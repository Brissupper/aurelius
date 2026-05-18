"""
Aurelius — worker.py
Background processing pipeline with R2 storage support.
"""

import os
import re
import uuid
import logging
import traceback
from pathlib import Path

from models import get_db, now, execute, fetchone, fetchall
from tts_engine import synthesize_chapter, count_chunks, detect_engine
from storage import storage

log = logging.getLogger("aurelius.worker")
OUTPUT_DIR = Path("output")


def update_job(job_id: str, **kwargs) -> None:
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values     = list(kwargs.values()) + [job_id]
    conn = get_db()
    execute(conn, f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
    conn.close()


def update_book_status(book_id: str, status: str) -> None:
    conn = get_db()
    execute(conn,
        "UPDATE books SET status = ?, updated_at = ? WHERE id = ?",
        (status, now(), book_id))
    conn.close()


# ── Text extraction ────────────────────────────────────────────────────────────

def _clean_raw_text(text: str) -> str:
    import re
    text = re.sub(r"-\n(\w)", r"\1", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\b\d{1,3}\b(?=\s)", "", text)
    text = re.sub(r"[^\w\s.,!?;:'\"\-()\u2018\u2019\u201c\u201d]", " ", text)
    return text.strip()

_STOPWORDS = {
    "OF THE","BY THE","IN THE","TO THE","FROM THE","AND THE",
    "FOR THE","AT THE","ON THE","WITH THE","AS THE","IS THE",
}

def _is_chapter_heading(line: str) -> bool:
    s = line.strip()
    if len(s) < 5 or len(s) > 65: return False
    if not re.match(r'^[A-Z][A-Z\s]+$', s): return False
    if s in _STOPWORDS: return False
    if re.match(r'^[IVXLC]+$', s) and len(s) < 5: return False
    return True

def extract_chapters(pdf_path: str) -> tuple:
    import fitz
    doc        = fitz.open(pdf_path)
    pages_text = [page.get_text("text").strip() for page in doc]
    book_title = doc.metadata.get("title", Path(pdf_path).stem)
    doc.close()

    # Match chapter headings but NOT table-of-contents lines (which have dot fills)
    standard_patterns = [
        r"^(CHAPTER\s+[IVXLC\d]+[\.\:]?(?:\s+[^.…]{2,})?)$",
        r"^(Chapter\s+[IVXLC\d]+[\.\:]?(?:\s+[^.…]{2,})?)$",
        r"^(PART\s+[IVXLC\d]+[\.\:]?(?:\s+[^.…]{2,})?)$",
    ]
    full_text = "\n".join(pages_text)
    lines     = full_text.split("\n")

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
        if text: chapters.append({"title": current_title, "text": text})
    if found_any and chapters:
        return book_title, chapters

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
        if text: chapters.append({"title": current_title, "text": text})
    if found_any and len(chapters) > 1:
        return book_title, chapters

    return book_title, [{"title": book_title, "text": _clean_raw_text(full_text)}]


# ── Main pipeline ──────────────────────────────────────────────────────────────

def process_book(job_id: str, book_id: str, pdf_path: str, voice: str) -> None:
    log.info(f"Job {job_id} starting — engine: {detect_engine()}")

    try:
        engine = detect_engine()

        conn = get_db()
        execute(conn, """
            UPDATE jobs SET status = ?, current_step = ?, started_at = ?
            WHERE id = ?
        """, ("running", f"Starting ({engine} engine)", now(), job_id))
        conn.close()
        update_book_status(book_id, "processing")

        # ── Extract ──────────────────────────────────────────────────────────
        conn = get_db()
        execute(conn, "UPDATE jobs SET current_step = ? WHERE id = ?",
                ("Extracting text from PDF", job_id))
        conn.close()

        book_title, chapters = extract_chapters(pdf_path)
        log.info(f"Extracted {len(chapters)} chapter(s)")

        conn = get_db()
        execute(conn, "UPDATE books SET title = ?, updated_at = ? WHERE id = ?",
                (book_title, now(), book_id))
        conn.close()

        # ── Save chapters to DB ───────────────────────────────────────────────
        conn = get_db()
        execute(conn, "UPDATE jobs SET current_step = ? WHERE id = ?",
                ("Detecting chapters", job_id))
        for i, ch in enumerate(chapters):
            ch_id = str(uuid.uuid4())
            execute(conn,
                """INSERT INTO chapters
                   (id, book_id, number, title, char_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (book_id, number) DO NOTHING""",
                (ch_id, book_id, i + 1, ch["title"], len(ch["text"]), now())
            )
        rows = fetchall(conn,
            "SELECT id, number, title FROM chapters WHERE book_id = ? ORDER BY number",
            (book_id,))
        conn.close()

        # ── Count total chunks ────────────────────────────────────────────────
        total_chunks = sum(count_chunks(ch["text"], engine) for ch in chapters)
        conn = get_db()
        execute(conn,
            "UPDATE jobs SET total = ?, progress = ?, current_step = ? WHERE id = ?",
            (total_chunks, 0, f"Generating audio ({engine})", job_id))
        conn.close()

        chunks_done = 0
        book_slug   = re.sub(r"[^\w]", "_", Path(pdf_path).stem.lower())
        audio_dir   = OUTPUT_DIR / book_slug / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # ── TTS each chapter ──────────────────────────────────────────────────
        for db_row, chapter in zip(rows, chapters):
            ch_id    = db_row["id"]
            ch_num   = db_row["number"]
            ch_title = db_row["title"]

            safe       = re.sub(r"[^\w\s-]", "", ch_title)[:40].strip()
            audio_path = str(audio_dir / f"{ch_num:02d}_{safe}.mp3")

            log.info(f"  Chapter {ch_num}/{len(chapters)}: {ch_title}")
            conn = get_db()
            execute(conn, "UPDATE jobs SET current_step = ? WHERE id = ?",
                    (f"Narrating ch.{ch_num}/{len(chapters)}: {ch_title[:35]}", job_id))
            conn.close()

            ch_chunk_count = count_chunks(chapter["text"], engine)
            _chunks_done   = chunks_done  # capture for closure

            def progress_cb(done, total, _offset=_chunks_done):
                c = get_db()
                execute(c, "UPDATE jobs SET progress = ? WHERE id = ?",
                        (_offset + done, job_id))
                c.close()

            actual_path = synthesize_chapter(
                text         = chapter["text"],
                output_path  = audio_path,
                voice_id     = voice,
                engine       = engine,
                progress_cb  = progress_cb,
                chunk_offset = chunks_done,
                total_chunks = total_chunks,
            )

            chunks_done += ch_chunk_count

            if not actual_path or not Path(actual_path).exists():
                actual_path = audio_path.replace(".mp3", ".wav")

            conn = get_db()
            execute(conn, "UPDATE jobs SET current_step = ? WHERE id = ?",
                    (f"Saving chapter {ch_num}...", job_id))
            conn.close()

            if storage.is_cloud():
                ext         = Path(actual_path).suffix
                storage_key = f"audio/{book_id}/{ch_num:02d}_{safe}{ext}"
                saved_ref   = storage.upload_file(actual_path, storage_key)
            else:
                saved_ref = actual_path

            conn = get_db()
            execute(conn, "UPDATE chapters SET audio_path = ? WHERE id = ?",
                    (saved_ref, ch_id))
            conn.close()

        # ── Upload PDF to R2 ──────────────────────────────────────────────────
        if storage.is_cloud() and Path(pdf_path).exists():
            conn = get_db()
            execute(conn, "UPDATE jobs SET current_step = ? WHERE id = ?",
                    ("Saving book to cloud storage...", job_id))
            conn.close()
            pdf_key   = f"pdfs/{book_id}.pdf"
            cloud_pdf = storage.upload_file(pdf_path, pdf_key)
            conn = get_db()
            execute(conn, "UPDATE books SET pdf_path = ?, updated_at = ? WHERE id = ?",
                    (cloud_pdf, now(), book_id))
            conn.close()

        # ── Done ──────────────────────────────────────────────────────────────
        conn = get_db()
        execute(conn,
            "UPDATE jobs SET status = ?, progress = ?, current_step = ?, finished_at = ? WHERE id = ?",
            ("complete", total_chunks, "Done", now(), job_id))
        conn.close()
        update_book_status(book_id, "ready")
        log.info(f"Job {job_id} complete!")

    except Exception as e:
        err = traceback.format_exc()
        log.error(f"Job {job_id} failed: {e}\n{err}")
        conn = get_db()
        execute(conn,
            "UPDATE jobs SET status = ?, current_step = ?, error = ?, finished_at = ? WHERE id = ?",
            ("failed", "Failed", str(e), now(), job_id))
        conn.close()
        update_book_status(book_id, "failed")
