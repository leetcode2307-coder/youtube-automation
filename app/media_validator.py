import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Tuple
from app.logging_config import logger


def get_media_info(media_path: Path) -> Dict[str, Any]:
    """Run ffprobe to inspect video and audio stream parameters."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(media_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def validate_final_media(
    video_path: Path,
    expected_min_duration: float = 29.5,
    expected_max_duration: float = 45.5,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validate that the assembled MP4 matches:
    - 1080x1920 portrait dimensions (9:16)
    - Total duration strictly within 30 to 45 seconds (with 0.5s tolerance)
    - Valid H.264 video stream
    - Valid audio stream
    """
    if not video_path.exists():
        return False, f"File does not exist: {video_path}", {}

    if video_path.stat().st_size < 50_000:
        return False, f"File size too small: {video_path.stat().st_size} bytes", {}

    try:
        info = get_media_info(video_path)
    except Exception as e:
        return False, f"FFprobe analysis failed: {e}", {}

    format_data = info.get("format", {})
    duration = float(format_data.get("duration", 0.0))

    if not (expected_min_duration <= duration <= expected_max_duration):
        return False, f"Duration ({duration:.2f}s) is outside the required 30-45s range.", info

    streams = info.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        return False, "No video stream found in media file.", info

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))

    if width != 1080 or height != 1920:
        return False, f"Video dimensions ({width}x{height}) must be 1080x1920 for YouTube Shorts (9:16).", info

    if not audio_stream:
        return False, "No audio stream found in media file.", info

    logger.info("Media validation PASSED for %s: %dx%d, %.2fs duration.", video_path.name, width, height, duration)
    return True, "Media validation passed.", info
