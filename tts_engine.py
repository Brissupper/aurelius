"""
Aurelius — tts_engine.py
TTS abstraction layer using Edge TTS (primary) with gTTS fallback.
"""

import re
import sys
import asyncio
import logging
from pathlib import Path

log = logging.getLogger("aurelius.tts")

CHUNK_SIZE = 500   # Edge TTS silently truncates long chunks — keep small


# =============================================================================
#  Engine detection
# =============================================================================

def detect_engine() -> str:
    try:
        import edge_tts  # noqa: F401
        return "edge_tts"
    except ImportError:
        return "gtts"


def engine_status() -> dict:
    return {
        "engine":         detect_engine(),
        "kokoro_model":   Path("kokoro-v1.0.onnx").exists(),
        "kokoro_voices":  Path("voices-v1.0.bin").exists(),
        "gtts_available": _gtts_available(),
        "edge_available": _edge_available(),
    }


def _gtts_available() -> bool:
    try:
        import gtts  # noqa: F401
        return True
    except Exception:
        return False


def _edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


# =============================================================================
#  Chunk helpers
# =============================================================================

def _split_chunks(text: str, size: int) -> list:
    sentences    = re.split(r'(?<=[.!?])\s+', text)
    chunks, curr = [], ""
    for s in sentences:
        if len(curr) + len(s) + 1 <= size:
            curr += (" " if curr else "") + s
        else:
            if curr:
                chunks.append(curr.strip())
            if len(s) > size:
                chunks += [s[i:i+size] for i in range(0, len(s), size)]
                curr = ""
            else:
                curr = s
    if curr:
        chunks.append(curr.strip())
    return [c for c in chunks if c.strip()]


def count_chunks(text: str, engine: str = None) -> int:
    return max(1, len(_split_chunks(text, CHUNK_SIZE)))


# =============================================================================
#  Voice mapping — Aurelius voice IDs → Edge TTS voice names
# =============================================================================

EDGE_VOICE_MAP = {
    "af_bella":   "en-US-AriaNeural",      # Warm, clear American female
    "af_sarah":   "en-US-JennyNeural",     # Bright American female
    "am_adam":    "en-US-GuyNeural",       # Deep American male
    "am_michael": "en-US-ChristopherNeural", # Narration American male
    "bf_emma":    "en-GB-SoniaNeural",     # Elegant British female
    "bm_george":  "en-GB-RyanNeural",      # Authoritative British male
}

DEFAULT_EDGE_VOICE = "en-US-AriaNeural"


# =============================================================================
#  Edge TTS
# =============================================================================

async def _edge_synthesize_chunk(text: str, voice: str, output_path: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def _synthesize_edge(text: str, output_path: str, voice_id: str,
                     progress_cb=None, chunk_offset: int = 0,
                     total_chunks: int = 0) -> str:
    edge_voice = EDGE_VOICE_MAP.get(voice_id, DEFAULT_EDGE_VOICE)
    chunks     = _split_chunks(text, CHUNK_SIZE)
    parts      = []

    log.info(f"  Edge TTS: {len(chunks)} chunk(s), voice: {edge_voice}")

    for i, chunk in enumerate(chunks):
        part_path = output_path.replace(".mp3", f"_part{i}.mp3")
        try:
            asyncio.run(_edge_synthesize_chunk(chunk, edge_voice, part_path))
            parts.append(part_path)
        except Exception as e:
            log.warning(f"  Chunk {i} failed: {e}")

        if progress_cb:
            progress_cb(chunk_offset + i + 1, total_chunks or len(chunks))

    if not parts:
        raise RuntimeError("Edge TTS produced no audio")

    if len(parts) == 1:
        Path(parts[0]).rename(output_path)
        log.info(f"  Saved MP3 (Edge TTS): {Path(output_path).name}")
        return output_path

    # Concatenate parts with pydub if available
    try:
        from pydub import AudioSegment
        combined = sum(AudioSegment.from_mp3(p) for p in parts)
        combined.export(output_path, format="mp3", bitrate="128k")
        for p in parts:
            Path(p).unlink(missing_ok=True)
    except Exception:
        # Just use first part if pydub not available
        Path(parts[0]).rename(output_path)
        for p in parts[1:]:
            Path(p).unlink(missing_ok=True)

    log.info(f"  Saved MP3 (Edge TTS): {Path(output_path).name}")
    return output_path


# =============================================================================
#  gTTS fallback
# =============================================================================

def _synthesize_gtts(text: str, output_path: str,
                     progress_cb=None, chunk_offset: int = 0,
                     total_chunks: int = 0) -> str:
    from gtts import gTTS
    chunks = _split_chunks(text, CHUNK_SIZE)
    parts  = []

    for i, chunk in enumerate(chunks):
        part_path = output_path.replace(".mp3", f"_part{i}.mp3")
        gTTS(text=chunk, lang="en", slow=False).save(part_path)
        parts.append(part_path)
        if progress_cb:
            progress_cb(chunk_offset + i + 1, total_chunks or len(chunks))

    if len(parts) == 1:
        Path(parts[0]).rename(output_path)
        return output_path

    try:
        from pydub import AudioSegment
        combined = sum(AudioSegment.from_mp3(p) for p in parts)
        combined.export(output_path, format="mp3", bitrate="128k")
        for p in parts:
            Path(p).unlink(missing_ok=True)
    except Exception:
        Path(parts[0]).rename(output_path)
        for p in parts[1:]:
            Path(p).unlink(missing_ok=True)

    log.info(f"  Saved MP3 (gTTS): {Path(output_path).name}")
    return output_path


# =============================================================================
#  Public API
# =============================================================================

def synthesize_chapter(text: str, output_path: str, voice_id: str,
                       engine: str = None, progress_cb=None,
                       chunk_offset: int = 0, total_chunks: int = 0) -> str:
    if engine is None:
        engine = detect_engine()

    log.info(f"  TTS engine: {engine}  |  voice: {voice_id}  |  "
             f"{len(text)} chars  ->  {Path(output_path).name}")

    if engine == "edge_tts":
        try:
            return _synthesize_edge(text, output_path, voice_id,
                                    progress_cb, chunk_offset, total_chunks)
        except Exception as e:
            log.warning(f"Edge TTS failed ({e}), falling back to gTTS")

    return _synthesize_gtts(text, output_path, progress_cb, chunk_offset, total_chunks)
