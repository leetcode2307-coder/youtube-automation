import asyncio
import re
from pathlib import Path
from typing import Optional
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
    communicate = edge_tts.Communicate(text, selected_voice)

    subtitles = []
    current_offset = 0.0

    if output_srt_path:
        with open(output_audio_path, "wb") as f_audio:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f_audio.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # Offset is in 100-nanosecond units (1e-7 seconds)
                    start_s = chunk["offset"] / 10_000_000.0
                    dur_s = chunk["duration"] / 10_000_000.0
                    word = chunk["text"]
                    subtitles.append((start_s, start_s + dur_s, word))

        # Chunk subtitles into readable phrases (e.g. 4-6 words)
        output_srt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_srt_path, "w", encoding="utf-8") as f_srt:
            if subtitles:
                phrase_size = 5
                index = 1
                for i in range(0, len(subtitles), phrase_size):
                    group = subtitles[i:i + phrase_size]
                    g_start = group[0][0]
                    g_end = group[-1][1]
                    g_text = " ".join(item[2] for item in group)

                    f_srt.write(f"{index}\n")
                    f_srt.write(f"{_format_srt_timestamp(g_start)} --> {_format_srt_timestamp(g_end)}\n")
                    f_srt.write(f"{g_text}\n\n")
                    index += 1
            else:
                # Fallback simple single-cue SRT
                f_srt.write(f"1\n00:00:00,000 --> 00:00:30,000\n{text[:50]}...\n\n")
    else:
        await communicate.save(str(output_audio_path))

    logger.info("Speech generation complete: %s", output_audio_path)
    return output_audio_path


def generate_speech_sync(
    text: str,
    output_audio_path: Path,
    output_srt_path: Optional[Path] = None,
    voice: Optional[str] = None
) -> Path:
    """Synchronous wrapper for generate_speech."""
    return asyncio.run(generate_speech(text, output_audio_path, output_srt_path, voice))
