import hashlib
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings
from app.intake import validate_flow_assets
from app.logging_config import logger
from app.media_validator import validate_final_media
from app.storage import storage
from app.tts import generate_speech_sync


def calculate_sha256(file_path: Path) -> str:
    """Calculate the SHA-256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


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
    voiceover_path = run_export_dir / "voiceover.mp3"
    srt_path = run_export_dir / "captions.srt"

    if not voiceover_path.exists():
        logger.info("Voiceover file not found in %s; generating with Edge-TTS...", run_export_dir)
        generate_speech_sync(package.narration_full, voiceover_path, srt_path)

    # 3. Create FFmpeg concat input file
    concat_file = run_export_dir / "concat_list.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for clip in sorted_clips:
            # Use forward slashes for FFmpeg compatibility on Windows
            clip_posix = clip.resolve().as_posix()
            f.write(f"file '{clip_posix}'\n")

    # 4. Build video filter for 1080x1920 9:16 portrait standardization
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        "fps=30"
    ]
    vf_string = ",".join(vf_filters)

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-i", str(voiceover_path),
        "-vf", vf_string,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(final_mp4_path)
    ]

    logger.info("Running FFmpeg assembly command for run %s...", run_id)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
    media_ok, message, info = validate_final_media(final_mp4_path)
    if not media_ok:
        logger.error("Media validation failed for %s: %s", run_id, message)
        storage.update_package_status(run_id, "failed", message)
        raise ValueError(f"Assembled video failed media validation: {message}")

    logger.info("Assembly and validation successfully completed for %s -> %s", run_id, final_mp4_path)
    storage.update_package_status(run_id, "awaiting_approval")
    return final_mp4_path
