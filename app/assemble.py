import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from app.config import resolve_tts_voice, settings
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


def _has_audio_stream(media_path: Path) -> bool:
    """Return True if media_path contains an audio stream."""
    try:
        info = get_media_info(media_path)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except Exception:
        return False


def _should_regenerate_tts(
    voiceover_path: Path,
    tts_meta_path: Path,
    custom_marker_path: Path,
    narration_text: str,
    active_voice: str,
) -> bool:
    """Generate Edge-TTS voiceover if missing or if script/voice changed for a previous auto-generated TTS file."""
    if not voiceover_path.exists():
        return True
    if custom_marker_path.exists() or not tts_meta_path.exists():
        # Preserve manually uploaded voiceover.mp3 files
        return False
    try:
        meta = json.loads(tts_meta_path.read_text(encoding="utf-8"))
        expected_hash = hashlib.sha256(narration_text.encode("utf-8")).hexdigest()
        return meta.get("narration_sha256") != expected_hash or meta.get("voice") != active_voice
    except Exception:
        return True


def assemble_video(run_id: str) -> Path:
    """
    Assemble exported Google Flow scene clips into a final 1080x1920 MP4
    with synchronized AI-generated voiceover narration (default) and subtitles.
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

    # 2. Resolve audio mode (AI-generated Edge-TTS voiceover is ON by default)
    studio_cfg = storage.get_studio_config()
    voiceover_path = run_export_dir / "voiceover.mp3"
    srt_path = run_export_dir / "captions.srt"
    tts_meta_path = run_export_dir / ".tts_meta.json"
    custom_marker_path = run_export_dir / ".custom_voiceover"

    audio_mode = studio_cfg.audio_mode if studio_cfg.use_ai_voiceover else "clip_audio_only"
    all_clips_have_audio = all(_has_audio_stream(clip) for clip in sorted_clips)
    use_clip_audio_only = audio_mode == "clip_audio_only" and all_clips_have_audio

    if not use_clip_audio_only:
        active_voice = resolve_tts_voice(package.language, studio_cfg.tts_voice)
        if _should_regenerate_tts(voiceover_path, tts_meta_path, custom_marker_path, package.narration_full, active_voice):
            logger.info(
                "Generating AI voiceover with Edge-TTS (%s, lang=%s) in %s...",
                active_voice, package.language, run_export_dir,
            )
            try:
                generate_speech_sync(package.narration_full, voiceover_path, srt_path, voice=active_voice)
                tts_meta_path.write_text(
                    json.dumps({
                        "voice": active_voice,
                        "narration_sha256": hashlib.sha256(package.narration_full.encode("utf-8")).hexdigest(),
                    }),
                    encoding="utf-8",
                )
            except Exception as e:
                error_msg = f"TTS voiceover generation failed: {e}"
                storage.update_package_status(run_id, "failed", error_msg)
                raise RuntimeError(error_msg) from e

    if srt_path.exists():
        shutil.copy2(srt_path, final_run_dir / "captions.srt")

    # 3. Probe clip and voiceover durations so audio never truncates or gets replaced by silent clip audio
    total_video_duration = sum(_probe_duration(clip) for clip in sorted_clips)
    audio_duration = 0.0 if use_clip_audio_only else _probe_duration(voiceover_path)
    max_dur_limit = float(studio_cfg.max_duration_seconds)
    pad_extra = 0.0
    if audio_duration > total_video_duration > 0 and total_video_duration < max_dur_limit:
        pad_extra = min(audio_duration - total_video_duration, max_dur_limit - total_video_duration)

    # 4. Build filter_complex to normalize heterogeneous clips to 1080x1920 30fps
    #    and apply AI-generated voiceover (or clip audio when explicitly selected).
    cmd = ["ffmpeg", "-y"]
    filter_parts = []
    mix_clip_audio = audio_mode == "ai_plus_clip_audio" and all_clips_have_audio

    for idx, clip in enumerate(sorted_clips):
        cmd.extend(["-i", str(clip)])
        filter_parts.append(
            f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[v{idx}]"
        )
        if use_clip_audio_only or mix_clip_audio:
            filter_parts.append(
                f"[{idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{idx}]"
            )

    if use_clip_audio_only:
        concat_streams = "".join(f"[v{i}][a{i}]" for i in range(len(sorted_clips)))
        filter_parts.append(f"{concat_streams}concat=n={len(sorted_clips)}:v=1:a=1[vout][aout]")
    else:
        audio_idx = len(sorted_clips)
        cmd.extend(["-i", str(voiceover_path)])

        concat_v = "".join(f"[v{i}]" for i in range(len(sorted_clips)))
        if pad_extra > 0.1:
            filter_parts.append(f"{concat_v}concat=n={len(sorted_clips)}:v=1:a=0[vconcat]")
            filter_parts.append(f"[vconcat]tpad=stop_mode=clone:stop_duration={pad_extra:.2f}[vout]")
        else:
            filter_parts.append(f"{concat_v}concat=n={len(sorted_clips)}:v=1:a=0[vout]")

        if mix_clip_audio:
            concat_a = "".join(f"[a{i}]" for i in range(len(sorted_clips)))
            filter_parts.append(f"{concat_a}concat=n={len(sorted_clips)}:v=0:a=1,volume=0.20[aclip]")
            filter_parts.append(
                f"[{audio_idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad[avoice]"
            )
            filter_parts.append("[avoice][aclip]amix=inputs=2:duration=first:dropout_transition=2[aout]")
        else:
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
