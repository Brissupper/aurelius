"""
Aurelius — worker.py
Background processing pipeline.
Runs in the same process as FastAPI using BackgroundTasks.
No Redis or Celery needed.

Pipeline per book:
  1. Extract text from PDF
  2. Detect chapters
  3. Save chapters to DB
  4. TTS each chapter with Kokoro
  5. Save audio paths to DB
  6. Mark job complete
"""

import os
import re
import uuid
import logging
import traceback
from pathlib import Path
from datetime import datetime

from models import get_db, now

log = logging.getLogger("aurelius.worker")

OUTPUT_DIR   = Path("output")
MODEL_PATH   = "kokoro-v1.0.onnx"
VOICES_PATH  = "voices-v1.0.bin"


# ── Job status helpers ────────────────────────────────────────────────────────

def update_job(job_id: str, **kwargs) -> None:
    """Update any fields on a job row."""
    if not kwargs:
        return
    kwargs["updated_at"] = now()
    # jobs table has no updated_at but we silently ignore extra keys
    kwargs.pop("updated_at", None)

    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values     = list(kwargs.values()) + [job_id]
    conn = get_db()
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def update_book_status(book_id: str, status: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE books SET status = ?, updated_at = ? WHERE id = ?",
        (status, now(), book_id)
    )
    conn.commit()
    conn.close()


# ── Text extraction (imported from engine logic inline) ───────────────────────

def _clean_raw_text(text: str) -> str:
    import re
    text = re.sub(r"-\n(\w)", r"\1", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\b\d{1,3}\b(?=\s)", "", text)
    text = re.sub(r"[^\w\s.,!?;:'\"\-()\u2018\u2019\u201c\u201d]", " ", text)
    return text.strip()


_STOPWORDS = {
    "OF THE", "BY THE", "IN THE", "TO THE", "FROM THE", "AND THE",
    "FOR THE", "AT THE", "ON THE", "WITH THE", "AS THE", "IS THE",
}


def _is_chapter_heading(line: str) -> bool:
    s = line.strip()
    if len(s) < 5 or len(s) > 65:
        return False
    if not re.match(r'^[A-Z][A-Z\s]+$', s):
        return False
    if s in _STOPWORDS:
        return False
    if re.match(r'^[IVXLC]+$', s) and len(s) < 5:
        return False
    return True


def extract_chapters(pdf_path: str) -> tuple[str, list[dict]]:
    """
    Returns (book_title, [{"title": str, "text": str}, ...])
    """
    import fitz

    doc        = fitz.open(pdf_path)
    pages_text = [page.get_text("text").strip() for page in doc]
    book_title = doc.metadata.get("title", Path(pdf_path).stem)
    doc.close()

    standard_patterns = [
        r"^(CHAPTER\s+[IVXLC\d]+[\.\:]?\s*.*)$",
        r"^(Chapter\s+\d+[\.\:]?\s*.*)$",
        r"^(PART\s+[IVXLC\d]+[\.\:]?\s*.*)$",
    ]

    full_text = "\n".join(pages_text)
    lines     = full_text.split("\n")

    # Pass 1: standard patterns
    chapters, current_title, current_text, found_any = [], "Introduction", [], False
    for line in lines:
        stripped, is_heading = line.strip(), False
        for pat in standard_patterns:
            if re.match(pat, stripped, re.MULTILINE):
                if current_text:
                    text = _clean_raw_text(" ".join(current_text))
                    if len(text) > 200:
                        chapters.append({"title": current_title, "text": text})
                current_title, current_text, is_heading, found_any = (
                    stripped.title(), [], True, True)
                break
        if not is_heading and stripped:
            current_text.append(stripped)
    if current_text:
        text = _clean_raw_text(" ".join(current_text))
        if text:
            chapters.append({"title": current_title, "text": text})
    if found_any and chapters:
        return book_title, chapters

    # Pass 2: all-caps headings
    chapters, current_title, current_text, found_any = [], book_title, [], False
    for line in lines:
        stripped = line.strip()
        if _is_chapter_heading(stripped):
            if current_text:
                text = _clean_raw_text(" ".join(current_text))
                if len(text) > 300:
                    chapters.append({"title": current_title, "text": text})
            current_title, current_text, found_any = stripped.title(), [], True
        elif stripped:
            current_text.append(stripped)
    if current_text:
        text = _clean_raw_text(" ".join(current_text))
        if text:
            chapters.append({"title": current_title, "text": text})
    if found_any and len(chapters) > 1:
        return book_title, chapters

    # Fallback: single chapter
    return book_title, [{"title": book_title, "text": _clean_raw_text(full_text)}]


# ── TTS ───────────────────────────────────────────────────────────────────────

def _split_chunks(text: str, size: int = 150) -> list[str]:
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


def run_tts(text: str, output_path: str, voice: str, kokoro,
            progress_cb=None, chunk_offset: int = 0, total_chunks: int = 0) -> None:
    """
    Convert text to audio using Kokoro. Calls progress_cb(done, total) if provided.
    Saves as WAV (or MP3 if pydub+ffmpeg available).
    """
    import numpy as np
    import soundfile as sf
    import warnings
    import io as _io
    import sys

    warnings.filterwarnings("ignore")

    chunks      = _split_chunks(text)
    all_audio   = []
    sample_rate = 24000

    for i, chunk in enumerate(chunks):
        try:
            old_stderr = sys.stderr
            sys.stderr = _io.StringIO()
            samples, sr = kokoro.create(chunk, voice=voice, speed=1.0, lang="en-us")
            sys.stderr = old_stderr
            all_audio.append(samples)
            sample_rate = sr
        except Exception:
            if 'old_stderr' in dir():
                sys.stderr = old_stderr

        if progress_cb:
            progress_cb(chunk_offset + i + 1, total_chunks)

    if not all_audio:
        return

    combined = np.concatenate(all_audio)
    wav_path  = output_path.replace(".mp3", ".wav")
    sf.write(wav_path, combined, sample_rate)

    try:
        from pydub import AudioSegment
        AudioSegment.from_wav(wav_path).export(output_path, format="mp3", bitrate="128k")
        os.remove(wav_path)
        return output_path
    except Exception:
        # Return WAV path
        final = output_path.replace(".mp3", ".wav")
        if wav_path != final:
            try:
                os.rename(wav_path, final)
            except Exception:
                pass
        return final


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_book(job_id: str, book_id: str, pdf_path: str, voice: str) -> None:
    """
    Full pipeline: PDF -> chapters -> TTS -> saved audio.
    Updates job progress in DB throughout.
    Called by FastAPI BackgroundTasks.
    """
    log.info(f"Job {job_id} starting for book {book_id}")

    try:
        # ── Mark started ──────────────────────────────────────────────────────
        update_job(job_id,
                   status="running",
                   current_step="Extracting text from PDF",
                   started_at=now())
        update_book_status(book_id, "processing")

        # ── Step 1: Extract ───────────────────────────────────────────────────
        book_title, chapters = extract_chapters(pdf_path)
        log.info(f"Extracted {len(chapters)} chapter(s) from '{book_title}'")

        # Update book title (may have been detected from PDF metadata)
        conn = get_db()
        conn.execute(
            "UPDATE books SET title = ?, updated_at = ? WHERE id = ?",
            (book_title, now(), book_id)
        )
        conn.commit()
        conn.close()

        # ── Step 2: Save chapters to DB ───────────────────────────────────────
        update_job(job_id, current_step="Detecting chapters")

        conn = get_db()
        for i, ch in enumerate(chapters):
            ch_id = str(uuid.uuid4())
            conn.execute(
                """INSERT OR IGNORE INTO chapters
                   (id, book_id, number, title, char_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ch_id, book_id, i + 1, ch["title"], len(ch["text"]), now())
            )
        conn.commit()

        # Fetch chapter IDs in order
        rows = conn.execute(
            "SELECT id, number, title FROM chapters WHERE book_id = ? ORDER BY number",
            (book_id,)
        ).fetchall()
        conn.close()

        # ── Step 3: Load Kokoro ───────────────────────────────────────────────
        update_job(job_id, current_step="Loading voice model")

        if not Path(MODEL_PATH).exists() or not Path(VOICES_PATH).exists():
            raise FileNotFoundError(
                "Kokoro model files not found. Place kokoro-v1.0.onnx and "
                "voices-v1.0.bin in the aurelius/ folder."
            )

        from kokoro_onnx import Kokoro
        kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
        log.info("Kokoro model loaded")

        # ── Step 4: TTS per chapter ───────────────────────────────────────────
        # Count total chunks across all chapters for global progress
        total_chunks = sum(
            len(_split_chunks(ch["text"])) for ch in chapters
        )
        update_job(job_id, total=total_chunks, progress=0,
                   current_step="Generating audio")

        chunks_done = 0

        book_slug  = re.sub(r"[^\w]", "_", Path(pdf_path).stem.lower())
        audio_dir  = OUTPUT_DIR / book_slug / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        for db_row, chapter in zip(rows, chapters):
            ch_id    = db_row["id"]
            ch_num   = db_row["number"]
            ch_title = db_row["title"]

            safe       = re.sub(r"[^\w\s-]", "", ch_title)[:40].strip()
            audio_path = str(audio_dir / f"{ch_num:02d}_{safe}.mp3")

            log.info(f"  TTS chapter {ch_num}: {ch_title}")
            update_job(job_id,
                       current_step=f"Narrating chapter {ch_num}/{len(chapters)}: {ch_title[:40]}")

            ch_chunks = len(_split_chunks(chapter["text"]))

            def progress_cb(done, total, _offset=chunks_done, _total=total_chunks):
                update_job(job_id, progress=_offset + (done - _offset))

            run_tts(
                text         = chapter["text"],
                output_path  = audio_path,
                voice        = voice,
                kokoro       = kokoro,
                progress_cb  = progress_cb,
                chunk_offset = chunks_done,
                total_chunks = total_chunks,
            )

            chunks_done += ch_chunks

            # Determine actual saved path (may be .wav if pydub not installed)
            actual_path = audio_path if Path(audio_path).exists() else audio_path.replace(".mp3", ".wav")

            conn = get_db()
            conn.execute(
                "UPDATE chapters SET audio_path = ? WHERE id = ?",
                (actual_path, ch_id)
            )
            conn.commit()
            conn.close()

        # ── Step 5: Complete ──────────────────────────────────────────────────
        update_job(job_id,
                   status="complete",
                   progress=total_chunks,
                   current_step="Done",
                   finished_at=now())
        update_book_status(book_id, "ready")
        log.info(f"Job {job_id} complete!")

    except Exception as e:
        err = traceback.format_exc()
        log.error(f"Job {job_id} failed: {e}\n{err}")
        update_job(job_id,
                   status="failed",
                   current_step="Failed",
                   error=str(e),
                   finished_at=now())
        update_book_status(book_id, "failed")
