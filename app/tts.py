import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple
import edge_tts
from app.config import settings
from app.logging_config import logger


def _format_srt_timestamp(seconds: float) -> str:
    """Format seconds into HH:MM:SS,mmm for SubRip SRT."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def _write_srt_cues(output_srt_path: Path, cues: List[Tuple[float, float, str]]) -> None:
    """Write a list of (start_s, end_s, text) cues to an SRT file."""
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_srt_path, "w", encoding="utf-8") as f_srt:
        for index, (start_s, end_s, cue_text) in enumerate(cues, start=1):
            f_srt.write(f"{index}\n")
            f_srt.write(f"{_format_srt_timestamp(start_s)} --> {_format_srt_timestamp(end_s)}\n")
            f_srt.write(f"{cue_text}\n\n")


async def generate_speech(
    text: str,
    output_audio_path: Path,
    output_srt_path: Optional[Path] = None,
    voice: Optional[str] = None
) -> Path:
    """Generate speech audio file and optional SRT subtitles using Edge-TTS."""
    selected_voice = voice or settings.TTS_VOICE
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Generating TTS speech (%s) to %s", selected_voice, output_audio_path)
    communicate = edge_tts.Communicate(
        text,
        selected_voice,
        boundary="WordBoundary",
    )

    word_subtitles: List[Tuple[float, float, str]] = []
    sentence_subtitles: List[Tuple[float, float, str]] = []

    if output_srt_path:
        with open(output_audio_path, "wb") as f_audio:
            async for chunk in communicate.stream():
                chunk_type = chunk.get("type")
                if chunk_type == "audio":
                    f_audio.write(chunk["data"])
                elif chunk_type in ("WordBoundary", "SentenceBoundary"):
                    # Offset is in 100-nanosecond units (1e-7 seconds)
                    start_s = chunk["offset"] / 10_000_000.0
                    dur_s = chunk["duration"] / 10_000_000.0
                    cue_text = chunk["text"]
                    if chunk_type == "WordBoundary":
                        word_subtitles.append((start_s, start_s + dur_s, cue_text))
                    else:
                        sentence_subtitles.append((start_s, start_s + dur_s, cue_text))

        cues: List[Tuple[float, float, str]] = []
        if word_subtitles:
            phrase_size = 5
            for i in range(0, len(word_subtitles), phrase_size):
                group = word_subtitles[i:i + phrase_size]
                cues.append((group[0][0], group[-1][1], " ".join(item[2] for item in group)))
        elif sentence_subtitles:
            cues = sentence_subtitles
        else:
            cues = [(0.0, 30.0, f"{text[:80]}...")]

        _write_srt_cues(output_srt_path, cues)
    else:
        await communicate.save(str(output_audio_path))

    if not output_audio_path.exists() or output_audio_path.stat().st_size == 0:
        raise RuntimeError(f"Edge-TTS produced an empty audio file: {output_audio_path}")

    logger.info("Speech generation complete: %s", output_audio_path)
    return output_audio_path


def generate_speech_sync(
    text: str,
    output_audio_path: Path,
    output_srt_path: Optional[Path] = None,
    voice: Optional[str] = None
) -> Path:
    """Synchronous wrapper for generate_speech (safe inside or outside running event loops)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(generate_speech(text, output_audio_path, output_srt_path, voice))

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(generate_speech(text, output_audio_path, output_srt_path, voice))
        ).result()
