from functools import lru_cache
from pathlib import Path
import subprocess
import edge_tts
import pytest

from app.config import settings


@lru_cache(maxsize=1)
def _synthetic_mp3_bytes() -> bytes:
    """Generate valid offline MP3 audio bytes once per test session using FFmpeg."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=mono",
            "-t", "2",
            "-c:a", "libmp3lame",
            "-b:a", "64k",
            "-f", "mp3",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


class _MockEdgeTTSCommunicate:
    """Offline Edge-TTS Communicate replacement yielding synthetic MP3 and WordBoundary events."""

    def __init__(self, text: str, voice: str = "", **kwargs):
        self.text = text
        self.voice = voice
        self.kwargs = kwargs

    async def stream(self):
        yield {"type": "audio", "data": _synthetic_mp3_bytes()}
        words = [w for w in self.text.split() if w]
        offset_100ns = 1_000_000  # 0.1s
        word_dur_100ns = 2_500_000  # 0.25s per word
        for word in words:
            yield {
                "type": "WordBoundary",
                "offset": offset_100ns,
                "duration": word_dur_100ns,
                "text": word,
            }
            offset_100ns += word_dur_100ns

    async def save(self, audio_fname: str) -> None:
        Path(audio_fname).write_bytes(_synthetic_mp3_bytes())


@pytest.fixture(autouse=True)
def _isolate_external_side_effects(monkeypatch, tmp_path):
    """Prevent real YouTube uploads, SMTP emails, Edge-TTS calls, and studio settings writes during pytest."""
    monkeypatch.setattr(settings, "YOUTUBE_MOCK_MODE", True)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "STUDIO_SETTINGS_FILE", tmp_path / "studio_settings.json")
    monkeypatch.setattr(edge_tts, "Communicate", _MockEdgeTTSCommunicate)
