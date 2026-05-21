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
from auth import create_jwt, get_current_user, get_optional_user, verify_google_token

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
        from models import init_phase4_tables
        init_phase4_tables()
        log.info("Aurelius API started — database ready")

        from tts_engine import detect_engine
        log.info(f"TTS engine: {detect_engine()}")

        # Migration: add user_id to books if not exists
        try:
            conn = get_db()
            execute(conn, "ALTER TABLE books ADD COLUMN user_id TEXT REFERENCES users(id)")
            conn.close()
            log.info("Migration: added user_id to books")
        except Exception:
            pass  # Column already exists

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


# =============================================================================
#  GUTENBERG IMPORT
# =============================================================================

from pydantic import BaseModel

class GutenbergImport(BaseModel):
    title: str
    author: str
    category: str
    text_url: str
    gutenberg_id: int

@app.post("/api/gutenberg/import", status_code=201)
async def gutenberg_import(data: GutenbergImport, background_tasks: BackgroundTasks):
    """Download a Gutenberg plain-text book and add it to the library."""
    import httpx

    book_id = str(uuid.uuid4())
    log.info(f"Importing Gutenberg book #{data.gutenberg_id}: {data.title}")

    # Download the plain text
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(data.text_url)
            r.raise_for_status()
            text_content = r.text
    except Exception as e:
        raise HTTPException(400, detail=f"Could not download book: {e}")

    if len(text_content) < 500:
        raise HTTPException(400, detail="Book text too short or empty")

    # Save as a text file (we'll handle .txt in worker)
    txt_path = UPLOAD_DIR / f"{book_id}.txt"
    txt_path.write_text(text_content, encoding="utf-8", errors="ignore")

    conn = get_db()
    execute(conn,
        """INSERT INTO books
           (id, title, author, category, filename, pdf_path, voice, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'af_bella', 'pending', ?, ?)""",
        (book_id, data.title, data.author, data.category,
         f"{data.title}.txt", str(txt_path), now(), now())
    )

    # Create job
    job_id = str(uuid.uuid4())
    execute(conn,
        """INSERT INTO jobs (id, book_id, status, progress, total, current_step, created_at)
           VALUES (?, ?, 'queued', 0, 0, 'Queued', ?)""",
        (job_id, book_id, now())
    )
    conn.close()

    from worker import process_gutenberg_book
    background_tasks.add_task(process_gutenberg_book, job_id, book_id, str(txt_path), 'af_bella')

    return {"book_id": book_id, "job_id": job_id, "message": "Book imported, narration starting"}


# =============================================================================
#  INTERNET ARCHIVE IMPORT
# =============================================================================

class ArchiveImport(BaseModel):
    title: str
    author: str
    category: str
    archive_id: str

@app.post("/api/archive/import", status_code=201)
async def archive_import(data: ArchiveImport, background_tasks: BackgroundTasks):
    """Fetch a plain-text file from Internet Archive and add to library."""
    import httpx

    log.info(f"Importing Archive item: {data.archive_id} — {data.title}")

    # Get item metadata to find best text file
    meta_url = f"https://archive.org/metadata/{data.archive_id}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            meta = await client.get(meta_url)
            meta.raise_for_status()
            meta_json = meta.json()
    except Exception as e:
        raise HTTPException(400, detail=f"Could not fetch Archive metadata: {e}")

    # Find best text file — prefer plain text over other formats
    files = meta_json.get("files", [])
    text_url = None
    formats_priority = ["DjVuTXT", "Plain Text", "Stripped Text"]

    for fmt in formats_priority:
        for f in files:
            if f.get("format") == fmt and f.get("name","").endswith(".txt"):
                text_url = f"https://archive.org/download/{data.archive_id}/{f['name']}"
                break
        if text_url:
            break

    # Fallback: any .txt file
    if not text_url:
        for f in files:
            if f.get("name","").endswith(".txt") and "meta" not in f.get("name","").lower():
                text_url = f"https://archive.org/download/{data.archive_id}/{f['name']}"
                break

    if not text_url:
        raise HTTPException(400, detail="No plain text file found for this item. Try a different edition.")

    # Download text
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(text_url)
            r.raise_for_status()
            text_content = r.text
    except Exception as e:
        raise HTTPException(400, detail=f"Could not download text: {e}")

    if len(text_content) < 500:
        raise HTTPException(400, detail="Text content too short or empty.")

    book_id  = str(uuid.uuid4())
    txt_path = UPLOAD_DIR / f"{book_id}.txt"
    txt_path.write_text(text_content, encoding="utf-8", errors="ignore")

    conn = get_db()
    execute(conn,
        """INSERT INTO books
           (id, title, author, category, filename, pdf_path, voice, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'af_bella', 'pending', ?, ?)""",
        (book_id, data.title, data.author, data.category,
         f"{data.title}.txt", str(txt_path), now(), now())
    )
    job_id = str(uuid.uuid4())
    execute(conn,
        """INSERT INTO jobs (id, book_id, status, progress, total, current_step, created_at)
           VALUES (?, ?, 'queued', 0, 0, 'Queued', ?)""",
        (job_id, book_id, now()))
    conn.close()

    from worker import process_gutenberg_book
    background_tasks.add_task(process_gutenberg_book, job_id, book_id, str(txt_path), "af_bella")

    log.info(f"Archive import queued: {data.archive_id} → book {book_id}")
    return {"book_id": book_id, "job_id": job_id, "message": "Item imported, narration starting"}


# =============================================================================
#  PHASE 4 — DMCA SAFE HARBOR + BOOK REQUESTS
# =============================================================================

from fastapi import Request as FastAPIRequest
from typing import Optional

# ── DMCA Takedown ─────────────────────────────────────────────────────────────

class DMCAReport(BaseModel):
    book_id: str
    reason: str

@app.post("/api/dmca/report")
async def dmca_report(data: DMCAReport, request: FastAPIRequest):
    """Flag a book for DMCA review. Immediately hides it pending review."""
    conn = get_db()
    book = fetchone(conn, "SELECT * FROM books WHERE id = ?", (data.book_id,))
    if not book:
        conn.close()
        raise HTTPException(404, detail="Book not found")

    reporter_ip = request.client.host or "unknown"
    report_id   = str(uuid.uuid4())

    # Check if already reported by this IP
    existing = fetchone(conn,
        "SELECT id FROM dmca_reports WHERE book_id = ? AND reporter_ip = ?",
        (data.book_id, reporter_ip))
    if existing:
        conn.close()
        return {"message": "Already reported"}

    execute(conn,
        """INSERT INTO dmca_reports (id, book_id, reporter_ip, reason, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (report_id, data.book_id, reporter_ip, data.reason[:1000], now()))

    # Hide book immediately pending review
    execute(conn,
        "UPDATE books SET status = 'dmca_flagged', updated_at = ? WHERE id = ?",
        (now(), data.book_id))
    conn.close()

    log.warning(f"DMCA report filed for book {data.book_id} by {reporter_ip}")
    return {"message": "Report received. The book has been hidden pending review. Thank you."}


@app.get("/api/dmca/reports")
def list_dmca_reports():
    """List all pending DMCA reports (admin view)."""
    conn = get_db()
    reports = fetchall(conn, """
        SELECT r.*, b.title as book_title, b.author as book_author
        FROM dmca_reports r
        JOIN books b ON b.id = r.book_id
        WHERE r.status = 'pending'
        ORDER BY r.created_at DESC
    """)
    conn.close()
    return {"reports": reports}


@app.post("/api/dmca/resolve/{report_id}")
async def resolve_dmca(report_id: str, action: str = "dismiss"):
    """
    Resolve a DMCA report.
    action='dismiss' → restore book, mark report dismissed
    action='remove'  → permanently delete book and audio
    """
    conn = get_db()
    report = fetchone(conn, "SELECT * FROM dmca_reports WHERE id = ?", (report_id,))
    if not report:
        conn.close()
        raise HTTPException(404, detail="Report not found")

    if action == "remove":
        # Permanently delete the book
        book_id = report["book_id"]
        chapters = fetchall(conn, "SELECT audio_path FROM chapters WHERE book_id = ?", (book_id,))
        for ch in chapters:
            if ch.get("audio_path") and Path(ch["audio_path"]).exists():
                Path(ch["audio_path"]).unlink(missing_ok=True)
        execute(conn, "DELETE FROM chapters WHERE book_id = ?", (book_id,))
        execute(conn, "DELETE FROM jobs     WHERE book_id = ?", (book_id,))
        execute(conn, "DELETE FROM books    WHERE id = ?",      (book_id,))
        execute(conn, "UPDATE dmca_reports SET status = 'removed' WHERE id = ?", (report_id,))
        log.info(f"DMCA: book {book_id} permanently removed")
    else:
        # Dismiss — restore book
        execute(conn,
            "UPDATE books SET status = 'ready', updated_at = ? WHERE id = ?",
            (now(), report["book_id"]))
        execute(conn,
            "UPDATE dmca_reports SET status = 'dismissed' WHERE id = ?", (report_id,))
        log.info(f"DMCA report {report_id} dismissed")

    conn.close()
    return {"message": f"Report {action}ed successfully"}


# ── Book Requests ─────────────────────────────────────────────────────────────

class BookRequestCreate(BaseModel):
    title: str
    author: str
    category: str = "Other"
    description: Optional[str] = None

@app.post("/api/requests", status_code=201)
async def create_request(data: BookRequestCreate, request: FastAPIRequest):
    """Submit a book request."""
    conn = get_db()

    # Check for duplicate (same title+author)
    existing = fetchone(conn,
        "SELECT id, votes FROM book_requests WHERE LOWER(title) = LOWER(?) AND LOWER(author) = LOWER(?)",
        (data.title.strip(), data.author.strip()))
    if existing:
        conn.close()
        return {"message": "This book has already been requested. Your vote has been counted.",
                "request_id": existing["id"], "already_existed": True}

    req_id = str(uuid.uuid4())
    execute(conn,
        """INSERT INTO book_requests
           (id, title, author, category, description, votes, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 1, 'requested', ?, ?)""",
        (req_id, data.title.strip(), data.author.strip(), data.category,
         data.description, now(), now()))

    # Auto-vote from requester
    voter_ip = request.client.host or "unknown"
    vote_id  = str(uuid.uuid4())
    try:
        execute(conn,
            "INSERT INTO request_votes (id, request_id, voter_ip, created_at) VALUES (?, ?, ?, ?)",
            (vote_id, req_id, voter_ip, now()))
    except Exception:
        pass

    conn.close()
    log.info(f"Book requested: '{data.title}' by {data.author}")

    # Auto-search Gutenberg in background
    from worker import auto_source_request
    background_tasks.add_task(
        lambda: __import__('asyncio').run(auto_source_request(req_id, data.title, data.author))
    )

    return {"message": "Request submitted!", "request_id": req_id}


@app.get("/api/requests")
def list_requests(status: str = "", sort: str = "votes"):
    """List book requests sorted by votes or date."""
    conn  = get_db()
    query = "SELECT * FROM book_requests"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    if sort == "votes":
        query += " ORDER BY votes DESC, created_at DESC"
    else:
        query += " ORDER BY created_at DESC"
    query += " LIMIT 100"
    rows = fetchall(conn, query, params)
    conn.close()
    return {"requests": rows}


@app.post("/api/requests/{req_id}/vote")
async def vote_request(req_id: str, request: FastAPIRequest):
    """Upvote a book request. One vote per IP per request."""
    voter_ip = request.client.host or "unknown"
    conn = get_db()

    req = fetchone(conn, "SELECT * FROM book_requests WHERE id = ?", (req_id,))
    if not req:
        conn.close()
        raise HTTPException(404, detail="Request not found")

    existing = fetchone(conn,
        "SELECT id FROM request_votes WHERE request_id = ? AND voter_ip = ?",
        (req_id, voter_ip))
    if existing:
        conn.close()
        return {"message": "Already voted", "votes": req["votes"]}

    vote_id = str(uuid.uuid4())
    execute(conn,
        "INSERT INTO request_votes (id, request_id, voter_ip, created_at) VALUES (?, ?, ?, ?)",
        (vote_id, req_id, voter_ip, now()))
    execute(conn,
        "UPDATE book_requests SET votes = votes + 1, updated_at = ? WHERE id = ?",
        (now(), req_id))

    updated = fetchone(conn, "SELECT votes FROM book_requests WHERE id = ?", (req_id,))
    conn.close()

    new_votes = updated["votes"] if updated else req["votes"] + 1
    log.info(f"Vote cast for request {req_id} — now {new_votes} votes")
    return {"message": "Vote counted!", "votes": new_votes}


@app.delete("/api/requests/{req_id}")
def delete_request(req_id: str):
    conn = get_db()
    execute(conn, "DELETE FROM request_votes WHERE request_id = ?", (req_id,))
    execute(conn, "DELETE FROM book_requests WHERE id = ?", (req_id,))
    conn.close()
    return {"message": "Deleted"}


# =============================================================================
#  AUTH ENDPOINTS
# =============================================================================

from pydantic import BaseModel as PydanticBase

class GoogleAuthRequest(PydanticBase):
    id_token: str

@app.post("/auth/google")
async def google_auth(data: GoogleAuthRequest):
    """Verify Google ID token, create/update user, return JWT."""
    user_info = await verify_google_token(data.id_token)

    conn = get_db()
    user = fetchone(conn,
        "SELECT * FROM users WHERE google_id = ?",
        (user_info["google_id"],))

    if user:
        # Update name/avatar if changed
        execute(conn,
            "UPDATE users SET name=?, avatar=?, updated_at=? WHERE google_id=?",
            (user_info["name"], user_info["avatar"], now(), user_info["google_id"]))
        user_id = user["id"]
    else:
        # Create new user
        user_id = str(uuid.uuid4())
        execute(conn,
            """INSERT INTO users (id, google_id, email, name, avatar, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, user_info["google_id"], user_info["email"],
             user_info["name"], user_info["avatar"], now(), now()))
        log.info(f"New user: {user_info['email']}")

    conn.close()

    token = create_jwt(
        user_id=user_id,
        email=user_info["email"],
        name=user_info["name"],
        avatar=user_info["avatar"],
    )
    return {
        "token": token,
        "user": {
            "id":     user_id,
            "email":  user_info["email"],
            "name":   user_info["name"],
            "avatar": user_info["avatar"],
        }
    }


@app.get("/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    """Return current user info from JWT."""
    return {"user": user}


@app.get("/auth/logout")
def logout():
    """Client just deletes token — nothing to do server-side."""
    return {"message": "Logged out"}


# =============================================================================
#  PATCH BOOKS ENDPOINTS TO BE USER-SCOPED
# =============================================================================

@app.get("/api/my/books")
def my_books(
    category: str = "",
    status: str = "",
    user: dict = Depends(get_current_user)
):
    """List books belonging to the current user only."""
    conn  = get_db()
    query = """
        SELECT b.*,
               COUNT(c.id) AS chapter_count,
               SUM(CASE WHEN c.audio_path IS NOT NULL THEN 1 ELSE 0 END) AS audio_ready
        FROM books b
        LEFT JOIN chapters c ON c.book_id = b.id
        WHERE b.user_id = ?
    """
    params = [user["sub"]]
    if category:
        query += " AND b.category = ?"
        params.append(category)
    if status:
        query += " AND b.status = ?"
        params.append(status)
    query += " GROUP BY b.id ORDER BY b.created_at DESC"
    rows = fetchall(conn, query, params)
    conn.close()
    return {"books": rows}


@app.post("/api/my/books/upload", status_code=201)
async def my_upload_book(
    file:     UploadFile = File(...),
    title:    str        = Form(default=""),
    author:   str        = Form(default="Unknown Author"),
    category: str        = Form(default="Other"),
    user:     dict       = Depends(get_current_user),
):
    """Upload a PDF to the current user's library."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Only PDF files are supported.")

    book_id  = str(uuid.uuid4())
    pdf_path = UPLOAD_DIR / f"{book_id}.pdf"
    contents = await file.read()
    pdf_path.write_bytes(contents)

    if not title.strip():
        title = file.filename.replace(".pdf","").replace("_"," ").replace("-"," ").title()

    conn = get_db()
    execute(conn,
        """INSERT INTO books
           (id, user_id, title, author, category, filename, pdf_path, voice, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'af_bella', 'stored', ?, ?)""",
        (book_id, user["sub"], title.strip(), author.strip(), category,
         file.filename, str(pdf_path), now(), now()))
    conn.close()

    log.info(f"Book uploaded by {user['email']}: '{title}'")
    return {"book_id": book_id, "title": title,
            "message": "Book saved. Click Narrate when ready."}


@app.post("/api/my/gutenberg/import", status_code=201)
async def my_gutenberg_import(
    data: GutenbergImport,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Import a Gutenberg book into the current user's library."""
    import httpx
    book_id = str(uuid.uuid4())
    log.info(f"Gutenberg import by {user['email']}: {data.title}")

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(data.text_url)
            r.raise_for_status()
            text_content = r.text
    except Exception as e:
        raise HTTPException(400, detail=f"Could not download book: {e}")

    if len(text_content) < 500:
        raise HTTPException(400, detail="Book text too short or empty")

    txt_path = UPLOAD_DIR / f"{book_id}.txt"
    txt_path.write_text(text_content, encoding="utf-8", errors="ignore")

    conn = get_db()
    execute(conn,
        """INSERT INTO books
           (id, user_id, title, author, category, filename, pdf_path, voice, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'af_bella', 'pending', ?, ?)""",
        (book_id, user["sub"], data.title, data.author, data.category,
         f"{data.title}.txt", str(txt_path), now(), now()))
    job_id = str(uuid.uuid4())
    execute(conn,
        """INSERT INTO jobs (id, book_id, status, progress, total, current_step, created_at)
           VALUES (?, ?, 'queued', 0, 0, 'Queued', ?)""",
        (job_id, book_id, now()))
    conn.close()

    from worker import process_gutenberg_book
    background_tasks.add_task(process_gutenberg_book, job_id, book_id, str(txt_path), "af_bella")
    return {"book_id": book_id, "job_id": job_id}


@app.post("/api/my/archive/import", status_code=201)
async def my_archive_import(
    data: ArchiveImport,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Import an Archive.org item into the current user's library."""
    import httpx
    log.info(f"Archive import by {user['email']}: {data.archive_id}")

    meta_url = f"https://archive.org/metadata/{data.archive_id}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            meta = await client.get(meta_url)
            meta_json = meta.json()
    except Exception as e:
        raise HTTPException(400, detail=f"Could not fetch Archive metadata: {e}")

    files = meta_json.get("files", [])
    text_url = None
    for fmt in ["DjVuTXT", "Plain Text", "Stripped Text"]:
        for f in files:
            if f.get("format") == fmt and f.get("name","").endswith(".txt"):
                text_url = f"https://archive.org/download/{data.archive_id}/{f['name']}"
                break
        if text_url:
            break
    if not text_url:
        for f in files:
            if f.get("name","").endswith(".txt") and "meta" not in f.get("name","").lower():
                text_url = f"https://archive.org/download/{data.archive_id}/{f['name']}"
                break
    if not text_url:
        raise HTTPException(400, detail="No plain text file found for this item.")

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(text_url)
            r.raise_for_status()
            text_content = r.text
    except Exception as e:
        raise HTTPException(400, detail=f"Could not download text: {e}")

    if len(text_content) < 500:
        raise HTTPException(400, detail="Text content too short or empty.")

    book_id  = str(uuid.uuid4())
    txt_path = UPLOAD_DIR / f"{book_id}.txt"
    txt_path.write_text(text_content, encoding="utf-8", errors="ignore")

    conn = get_db()
    execute(conn,
        """INSERT INTO books
           (id, user_id, title, author, category, filename, pdf_path, voice, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'af_bella', 'pending', ?, ?)""",
        (book_id, user["sub"], data.title, data.author, data.category,
         f"{data.title}.txt", str(txt_path), now(), now()))
    job_id = str(uuid.uuid4())
    execute(conn,
        """INSERT INTO jobs (id, book_id, status, progress, total, current_step, created_at)
           VALUES (?, ?, 'queued', 0, 0, 'Queued', ?)""",
        (job_id, book_id, now()))
    conn.close()

    from worker import process_gutenberg_book
    background_tasks.add_task(process_gutenberg_book, job_id, book_id, str(txt_path), "af_bella")
    return {"book_id": book_id, "job_id": job_id}
