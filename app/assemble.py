import hashlib
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.intake import validate_flow_assets
from app.logging_config import logger
from app.media_validator import get_media_info, validate_final_media
from app.storage import storage
from app.tts import generate_speech_sync


def calculate_sha256(file_path: Path) -> str:
    """Calculate the SHA-256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def _probe_duration(media_path: Path) -> float:
    """Return media duration in seconds via ffprobe, or 0.0 if probing fails."""
    try:
        info = get_media_info(media_path)
        return float(info.get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


def assemble_video(run_id: str) -> Path:
    """
    Assemble exported Google Flow scene clips into a final 1080x1920 MP4
    with synchronized voiceover narration and subtitles.
    """
    package = storage.load_package(run_id)
    if not package:
        raise FileNotFoundError(f"Package for run {run_id} not found.")

    storage.update_package_status(run_id, "assembling")
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    final_run_dir = settings.FINAL_OUTPUT_DIR / run_id
    final_run_dir.mkdir(parents=True, exist_ok=True)
    final_mp4_path = final_run_dir / "final_short.mp4"
    sha256_path = final_run_dir / "final_short.sha256"

    # 1. Validate flow asset intake
    is_valid, errors, sorted_clips = validate_flow_assets(run_export_dir, package)
    if not is_valid:
        error_msg = f"Asset intake validation failed: {'; '.join(errors)}"
        storage.update_package_status(run_id, "failed", error_msg)
        raise ValueError(error_msg)

    # 2. Check or generate voiceover audio and subtitles
    studio_cfg = storage.get_studio_config()
    voiceover_path = run_export_dir / "voiceover.mp3"
    srt_path = run_export_dir / "captions.srt"

    if not voiceover_path.exists():
        logger.info("Voiceover file not found in %s; generating with Edge-TTS (%s)...", run_export_dir, studio_cfg.tts_voice)
        try:
            generate_speech_sync(package.narration_full, voiceover_path, srt_path, voice=studio_cfg.tts_voice)
        except Exception as e:
            error_msg = f"TTS voiceover generation failed: {e}"
            storage.update_package_status(run_id, "failed", error_msg)
            raise RuntimeError(error_msg) from e

    if srt_path.exists():
        shutil.copy2(srt_path, final_run_dir / "captions.srt")

    # 3. Probe clip and voiceover durations so audio never truncates or gets replaced by silent clip audio
    total_video_duration = sum(_probe_duration(clip) for clip in sorted_clips)
    audio_duration = _probe_duration(voiceover_path)
    max_dur_limit = float(studio_cfg.max_duration_seconds)
    pad_extra = 0.0
    if audio_duration > total_video_duration > 0 and total_video_duration < max_dur_limit:
        pad_extra = min(audio_duration - total_video_duration, max_dur_limit - total_video_duration)

    # 4. Build filter_complex to normalize heterogeneous clips to 1080x1920 30fps
    #    and map the voiceover track explicitly with trailing silence padding.
    cmd = ["ffmpeg", "-y"]
    filter_parts = []
    for idx, clip in enumerate(sorted_clips):
        cmd.extend(["-i", str(clip)])
        filter_parts.append(
            f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[v{idx}]"
        )

    audio_idx = len(sorted_clips)
    cmd.extend(["-i", str(voiceover_path)])

    concat_inputs = "".join(f"[v{i}]" for i in range(len(sorted_clips)))
    if pad_extra > 0.1:
        filter_parts.append(f"{concat_inputs}concat=n={len(sorted_clips)}:v=1:a=0[vconcat]")
        filter_parts.append(f"[vconcat]tpad=stop_mode=clone:stop_duration={pad_extra:.2f}[vout]")
    else:
        filter_parts.append(f"{concat_inputs}concat=n={len(sorted_clips)}:v=1:a=0[vout]")

    filter_parts.append(
        f"[{audio_idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad[aout]"
    )

    cmd.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-threads", str(settings.FFMPEG_THREADS),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(final_mp4_path),
    ])

    logger.info("Running FFmpeg assembly command for run %s...", run_id)
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logger.error("FFmpeg assembly failed: %s\nStderr: %s", e, e.stderr)
        storage.update_package_status(run_id, "failed", f"FFmpeg failed: {e.stderr[:200]}")
        raise RuntimeError(f"FFmpeg assembly failed: {e.stderr}") from e

    # 5. Compute SHA-256 hash
    file_sha256 = calculate_sha256(final_mp4_path)
    with open(sha256_path, "w", encoding="utf-8") as f:
        f.write(file_sha256)
    logger.info("Saved checksum: %s (%s)", sha256_path, file_sha256)

    # 6. Validate final assembled media
    media_ok, message, info = validate_final_media(
        final_mp4_path,
        expected_min_duration=float(studio_cfg.min_duration_seconds) - 0.5,
        expected_max_duration=float(studio_cfg.max_duration_seconds) + 0.5,
    )
    if not media_ok:
        logger.error("Media validation failed for %s: %s", run_id, message)
        storage.update_package_status(run_id, "failed", message)
        raise ValueError(f"Assembled video failed media validation: {message}")

    logger.info("Assembly and validation successfully completed for %s -> %s", run_id, final_mp4_path)
    storage.update_package_status(run_id, "awaiting_approval")
    return final_mp4_path
