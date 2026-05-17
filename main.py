"""
Aurelius — main.py
FastAPI backend. Run with: uvicorn main:app --reload --port 8000
"""

import os
import uuid
import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from models import get_db, init_db, now, execute, fetchone, fetchall

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("aurelius.api")

app = FastAPI(title="Aurelius API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

VALID_VOICES = ["af_bella", "af_sarah", "am_adam", "am_michael", "bf_emma", "bm_george"]
CATEGORIES   = ["Fiction", "Non-Fiction", "Philosophy", "Science", "History",
                 "Biography", "Self-Help", "Spirituality", "Technology", "Other"]


# =============================================================================
#  STARTUP
# =============================================================================

@app.on_event("startup")
async def startup():
    try:
        init_db()
        log.info("Aurelius API started — database ready")

        model_file  = Path("kokoro-v1.0.onnx")
        voices_file = Path("voices-v1.0.bin")
        r2_vars = ["CLOUDFLARE_R2_ENDPOINT", "CLOUDFLARE_R2_ACCESS_KEY",
                   "CLOUDFLARE_R2_SECRET_KEY", "CLOUDFLARE_R2_BUCKET"]
        has_r2 = all(os.environ.get(v) for v in r2_vars)

        if not model_file.exists() or not voices_file.exists():
            if has_r2:
                log.info("Downloading Kokoro model files from R2...")
                import boto3
                s3 = boto3.client(
                    "s3",
                    endpoint_url          = os.environ["CLOUDFLARE_R2_ENDPOINT"],
                    aws_access_key_id     = os.environ["CLOUDFLARE_R2_ACCESS_KEY"],
                    aws_secret_access_key = os.environ["CLOUDFLARE_R2_SECRET_KEY"],
                    region_name           = "auto",
                )
                bucket = os.environ["CLOUDFLARE_R2_BUCKET"]
                if not model_file.exists():
                    log.info("  Downloading kokoro-v1.0.onnx (~310 MB)...")
                    s3.download_file(bucket, "models/kokoro-v1.0.onnx", "kokoro-v1.0.onnx")
                    log.info("  kokoro-v1.0.onnx ✓")
                if not voices_file.exists():
                    log.info("  Downloading voices-v1.0.bin (~27 MB)...")
                    s3.download_file(bucket, "models/voices-v1.0.bin", "voices-v1.0.bin")
                    log.info("  voices-v1.0.bin ✓")
                log.info("Kokoro model files ready.")
            else:
                log.warning("Kokoro model files not found — falling back to gTTS.")
        else:
            log.info("Kokoro model files already present.")

        from storage import storage
        mode = "Cloudflare R2" if storage.is_cloud() else "Local disk"
        log.info(f"Storage mode: {mode}")

    except Exception as e:
        log.error(f"Startup error: {e}")
        import traceback
        log.error(traceback.format_exc())
        raise


# =============================================================================
#  FRONTEND
# =============================================================================

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return open("app.html", encoding="utf-8").read()


# =============================================================================
#  HEALTH
# =============================================================================

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# =============================================================================
#  BOOKS
# =============================================================================

@app.post("/api/books/upload", status_code=201)
async def upload_book(
    file:     UploadFile = File(...),
    title:    str        = Form(default=""),
    author:   str        = Form(default="Unknown Author"),
    category: str        = Form(default="Other"),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Only PDF files are supported.")

    book_id  = str(uuid.uuid4())
    pdf_path = UPLOAD_DIR / f"{book_id}.pdf"
    contents = await file.read()
    pdf_path.write_bytes(contents)

    if not title.strip():
        title = file.filename.replace(".pdf", "").replace("_", " ").replace("-", " ").title()

    conn = get_db()
    execute(conn,
        """INSERT INTO books
           (id, title, author, category, filename, pdf_path, voice, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'af_bella', 'stored', ?, ?)""",
        (book_id, title.strip(), author.strip(), category,
         file.filename, str(pdf_path), now(), now())
    )
    conn.close()

    log.info(f"Book stored: '{title}' by {author} [{category}]")
    return {"book_id": book_id, "title": title,
            "message": "Book saved to library. Select it and click Narrate when ready."}


@app.get("/api/books")
def list_books(category: str = "", status: str = ""):
    conn  = get_db()
    query = """
        SELECT b.*,
               COUNT(c.id) AS chapter_count,
               SUM(CASE WHEN c.audio_path IS NOT NULL THEN 1 ELSE 0 END) AS audio_ready
        FROM books b
        LEFT JOIN chapters c ON c.book_id = b.id
    """
    conditions, params = [], []
    if category:
        conditions.append("b.category = ?")
        params.append(category)
    if status:
        conditions.append("b.status = ?")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY b.id ORDER BY b.created_at DESC"

    rows = fetchall(conn, query, params)
    conn.close()
    return {"books": rows}


@app.get("/api/books/{book_id}")
def get_book(book_id: str):
    conn     = get_db()
    book     = fetchone(conn, "SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        conn.close()
        raise HTTPException(404, detail="Book not found")
    chapters = fetchall(conn,
        "SELECT * FROM chapters WHERE book_id = ? ORDER BY number", (book_id,))
    job = fetchone(conn,
        "SELECT * FROM jobs WHERE book_id = ? ORDER BY created_at DESC LIMIT 1", (book_id,))
    conn.close()
    return {"book": book, "chapters": chapters, "job": job}


@app.patch("/api/books/{book_id}")
async def update_book(
    book_id:  str,
    title:    str = Form(default=None),
    author:   str = Form(default=None),
    category: str = Form(default=None),
    voice:    str = Form(default=None),
):
    conn = get_db()
    book = fetchone(conn, "SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        conn.close()
        raise HTTPException(404, detail="Book not found")

    updates, params = [], []
    if title    is not None: updates.append("title = ?");    params.append(title)
    if author   is not None: updates.append("author = ?");   params.append(author)
    if category is not None: updates.append("category = ?"); params.append(category)
    if voice    is not None: updates.append("voice = ?");    params.append(voice)

    if updates:
        updates.append("updated_at = ?")
        params.append(now())
        params.append(book_id)
        execute(conn, f"UPDATE books SET {', '.join(updates)} WHERE id = ?", params)
    conn.close()
    return {"message": "Updated"}


@app.delete("/api/books/{book_id}")
def delete_book(book_id: str):
    conn = get_db()
    book = fetchone(conn, "SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        conn.close()
        raise HTTPException(404, detail="Book not found")

    chapters = fetchall(conn,
        "SELECT audio_path FROM chapters WHERE book_id = ?", (book_id,))
    for ch in chapters:
        if ch.get("audio_path") and Path(ch["audio_path"]).exists():
            Path(ch["audio_path"]).unlink(missing_ok=True)

    pdf = Path(book["pdf_path"])
    if pdf.exists():
        pdf.unlink(missing_ok=True)

    execute(conn, "DELETE FROM chapters WHERE book_id = ?", (book_id,))
    execute(conn, "DELETE FROM jobs     WHERE book_id = ?", (book_id,))
    execute(conn, "DELETE FROM books    WHERE id = ?",      (book_id,))
    conn.close()
    return {"message": "Deleted"}


# =============================================================================
#  NARRATION
# =============================================================================

@app.post("/api/books/{book_id}/narrate", status_code=202)
def narrate_book(
    book_id:          str,
    background_tasks: BackgroundTasks,
    voice:            str = Form(default="af_bella"),
):
    if voice not in VALID_VOICES:
        raise HTTPException(400, detail=f"Invalid voice. Choose: {VALID_VOICES}")

    conn = get_db()
    book = fetchone(conn, "SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        conn.close()
        raise HTTPException(404, detail="Book not found")

    execute(conn, "DELETE FROM chapters WHERE book_id = ?", (book_id,))
    execute(conn, "DELETE FROM jobs     WHERE book_id = ?", (book_id,))
    execute(conn,
        "UPDATE books SET status = 'pending', voice = ?, updated_at = ? WHERE id = ?",
        (voice, now(), book_id))

    job_id = str(uuid.uuid4())
    execute(conn,
        """INSERT INTO jobs (id, book_id, status, progress, total, current_step, created_at)
           VALUES (?, ?, 'queued', 0, 0, 'Queued', ?)""",
        (job_id, book_id, now()))
    conn.close()

    from worker import process_book
    background_tasks.add_task(process_book, job_id, book_id, book["pdf_path"], voice)

    log.info(f"Narration started for '{book['title']}' with voice {voice}")
    return {"job_id": job_id, "book_id": book_id, "message": "Narration started"}


# =============================================================================
#  JOBS
# =============================================================================

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    conn = get_db()
    job  = fetchone(conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
    conn.close()
    if not job:
        raise HTTPException(404, detail="Job not found")
    total    = job.get("total", 0)
    progress = job.get("progress", 0)
    job["percent"] = round((progress / total * 100) if total > 0 else 0, 1)
    return job


# =============================================================================
#  AUDIO
# =============================================================================

@app.get("/api/audio/{chapter_id}")
def stream_audio(chapter_id: str):
    from storage import storage
    from fastapi.responses import RedirectResponse

    conn    = get_db()
    chapter = fetchone(conn, "SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    conn.close()
    if not chapter:
        raise HTTPException(404, detail="Chapter not found")

    audio_ref = chapter.get("audio_path")
    if not audio_ref:
        raise HTTPException(404, detail="Audio not yet generated")

    if storage.is_cloud() or audio_ref.startswith("http"):
        url = storage.get_url(audio_ref) if not audio_ref.startswith("http") else audio_ref
        return RedirectResponse(url=url)

    if not Path(audio_ref).exists():
        raise HTTPException(404, detail="Audio file not found on disk")
    mime = "audio/mpeg" if str(audio_ref).endswith(".mp3") else "audio/wav"
    return FileResponse(path=audio_ref, media_type=mime,
                        headers={"Accept-Ranges": "bytes"})


# =============================================================================
#  METADATA
# =============================================================================

@app.get("/api/voices")
def list_voices():
    return {"voices": [
        {"id": "af_bella",   "name": "Bella",   "accent": "American", "gender": "Female", "style": "Warm, clear"},
        {"id": "af_sarah",   "name": "Sarah",   "accent": "American", "gender": "Female", "style": "Bright"},
        {"id": "am_adam",    "name": "Adam",    "accent": "American", "gender": "Male",   "style": "Deep, steady"},
        {"id": "am_michael", "name": "Michael", "accent": "American", "gender": "Male",   "style": "Narration"},
        {"id": "bf_emma",    "name": "Emma",    "accent": "British",  "gender": "Female", "style": "Elegant"},
        {"id": "bm_george",  "name": "George",  "accent": "British",  "gender": "Male",   "style": "Authoritative"},
    ]}

@app.get("/api/categories")
def list_categories():
    return {"categories": CATEGORIES}

@app.get("/api/tts-status")
def tts_status():
    from tts_engine import engine_status
    return engine_status()
