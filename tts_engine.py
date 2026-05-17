"""
Aurelius — tts_engine.py
TTS abstraction layer. Tries Kokoro first, falls back to gTTS.
kokoro-onnx is NOT in requirements.txt — it is installed at runtime
after the Render build, because it needs libespeak-ng which is not
available during the build step.
"""

import re
import sys
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("aurelius.tts")

CHUNK_SIZE = 150   # chars per Kokoro chunk
GTTS_CHUNK = 2000  # chars per gTTS chunk

_kokoro_instance = None   # lazy singleton


# =============================================================================
#  Engine detection
# =============================================================================

def _try_install_kokoro() -> bool:
    """Attempt to pip-install kokoro-onnx at runtime. Returns True if success."""
    try:
        log.info("Installing kokoro-onnx at runtime...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "kokoro-onnx>=0.3.0", "--quiet"],
            timeout=120,
        )
        log.info("kokoro-onnx installed successfully.")
        return True
    except Exception as e:
        log.warning(f"Could not install kokoro-onnx: {e}")
        return False


def detect_engine() -> str:
    """Return 'kokoro' if model files exist and library loads, else 'gtts'."""
    model  = Path("kokoro-v1.0.onnx")
    voices = Path("voices-v1.0.bin")
    if not (model.exists() and voices.exists()):
        return "gtts"
    try:
        import kokoro_onnx  # noqa: F401
        return "kokoro"
    except ImportError:
        if _try_install_kokoro():
            try:
                import kokoro_onnx  # noqa: F401
                return "kokoro"
            except Exception:
                pass
        return "gtts"
    except Exception:
        return "gtts"


def engine_status() -> dict:
    engine    = detect_engine()
    model_ok  = Path("kokoro-v1.0.onnx").exists()
    voices_ok = Path("voices-v1.0.bin").exists()
    return {
        "engine":         engine,
        "kokoro_model":   model_ok,
        "kokoro_voices":  voices_ok,
        "gtts_available": _gtts_available(),
    }


def _gtts_available() -> bool:
    try:
        import gtts  # noqa: F401
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
    if engine is None:
        engine = detect_engine()
    size = CHUNK_SIZE if engine == "kokoro" else GTTS_CHUNK
    return max(1, len(_split_chunks(text, size)))


# =============================================================================
#  Kokoro TTS
# =============================================================================

def _get_kokoro():
    global _kokoro_instance
    if _kokoro_instance is None:
        from kokoro_onnx import Kokoro
        log.info("Loading Kokoro model...")
        _kokoro_instance = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
        log.info("Kokoro ready.")
    return _kokoro_instance


def _synthesize_kokoro(text: str, output_path: str, voice_id: str,
                        progress_cb=None, chunk_offset: int = 0,
                        total_chunks: int = 0) -> str:
    import io as _io
    import numpy as np
    import soundfile as sf

    kokoro = _get_kokoro()
    chunks = _split_chunks(text, CHUNK_SIZE)
    audio  = []
    sr     = 24000

    for i, chunk in enumerate(chunks):
        try:
            old_err    = sys.stderr
            sys.stderr = _io.StringIO()
            samples, sr = kokoro.create(chunk, voice=voice_id, speed=1.0, lang="en-us")
            sys.stderr = old_err
            audio.append(samples)
        except Exception as e:
            try:
                sys.stderr = old_err
            except Exception:
                pass
            log.warning(f"Chunk {i} failed: {e}")

        if progress_cb:
            progress_cb(chunk_offset + i + 1, total_chunks or len(chunks))

    if not audio:
        raise RuntimeError("Kokoro produced no audio")

    combined = np.concatenate(audio)
    wav_path = output_path.replace(".mp3", ".wav")
    sf.write(wav_path, combined, sr)

    try:
        from pydub import AudioSegment
        AudioSegment.from_wav(wav_path).export(output_path, format="mp3", bitrate="128k")
        Path(wav_path).unlink(missing_ok=True)
        log.info(f"  Saved MP3: {Path(output_path).name}")
        return output_path
    except Exception:
        final = output_path.replace(".mp3", ".wav")
        if wav_path != final:
            try:
                Path(wav_path).rename(final)
            except Exception:
                pass
        log.info(f"  Saved WAV: {Path(final).name}")
        return final


# =============================================================================
#  gTTS fallback
# =============================================================================

def _synthesize_gtts(text: str, output_path: str,
                      progress_cb=None, chunk_offset: int = 0,
                      total_chunks: int = 0) -> str:
    from gtts import gTTS

    chunks = _split_chunks(text, GTTS_CHUNK)
    parts  = []

    for i, chunk in enumerate(chunks):
        part_path = output_path.replace(".mp3", f"_part{i}.mp3")
        tts = gTTS(text=chunk, lang="en", slow=False)
        tts.save(part_path)
        parts.append(part_path)

        if progress_cb:
            progress_cb(chunk_offset + i + 1, total_chunks or len(chunks))

    if len(parts) == 1:
        Path(parts[0]).rename(output_path)
        log.info(f"  Saved MP3 (gTTS): {Path(output_path).name}")
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

    if engine == "kokoro":
        try:
            return _synthesize_kokoro(text, output_path, voice_id,
                                       progress_cb, chunk_offset, total_chunks)
        except Exception as e:
            log.warning(f"Kokoro failed ({e}), falling back to gTTS")

    return _synthesize_gtts(text, output_path, progress_cb, chunk_offset, total_chunks)
