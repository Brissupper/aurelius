"""
Aurelius — models.py
Database layer. Uses PostgreSQL in production (DATABASE_URL env var),
falls back to SQLite for local development.
"""

import os
import logging
from datetime import datetime

log = logging.getLogger("aurelius.models")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)


def get_db():
    if USE_POSTGRES:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        import sqlite3
        conn = sqlite3.connect("aurelius.db")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def init_db() -> None:
    conn = get_db()
    if USE_POSTGRES:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                author      TEXT NOT NULL DEFAULT 'Unknown Author',
                category    TEXT NOT NULL DEFAULT 'Other',
                filename    TEXT NOT NULL,
                pdf_path    TEXT NOT NULL,
                voice       TEXT NOT NULL DEFAULT 'af_bella',
                status      TEXT NOT NULL DEFAULT 'stored',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chapters (
                id          TEXT PRIMARY KEY,
                book_id     TEXT NOT NULL REFERENCES books(id),
                number      INTEGER NOT NULL,
                title       TEXT NOT NULL,
                char_count  INTEGER NOT NULL DEFAULT 0,
                audio_path  TEXT,
                duration_s  REAL,
                created_at  TEXT NOT NULL,
                UNIQUE(book_id, number)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,
                book_id      TEXT NOT NULL REFERENCES books(id),
                status       TEXT NOT NULL DEFAULT 'queued',
                progress     INTEGER NOT NULL DEFAULT 0,
                total        INTEGER NOT NULL DEFAULT 0,
                current_step TEXT NOT NULL DEFAULT 'Queued',
                error        TEXT,
                started_at   TEXT,
                finished_at  TEXT,
                created_at   TEXT NOT NULL
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        log.info("PostgreSQL schema ready.")
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Unknown Author',
                category TEXT NOT NULL DEFAULT 'Other',
                filename TEXT NOT NULL, pdf_path TEXT NOT NULL,
                voice TEXT NOT NULL DEFAULT 'af_bella',
                status TEXT NOT NULL DEFAULT 'stored',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chapters (
                id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
                number INTEGER NOT NULL, title TEXT NOT NULL,
                char_count INTEGER NOT NULL DEFAULT 0, audio_path TEXT,
                duration_s REAL, created_at TEXT NOT NULL, UNIQUE(book_id, number)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                current_step TEXT NOT NULL DEFAULT 'Queued', error TEXT,
                started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()
        log.info("SQLite schema ready.")


def now() -> str:
    return datetime.utcnow().isoformat()


def execute(conn, sql: str, params=None):
    """Run a write query."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params or [])
        conn.commit()
        cur.close()
    else:
        conn.execute(sql, params or [])
        conn.commit()


def fetchone(conn, sql: str, params=None):
    """Fetch one row as dict."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params or [])
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    else:
        row = conn.execute(sql, params or []).fetchone()
        return dict(row) if row else None


def fetchall(conn, sql: str, params=None):
    """Fetch all rows as list of dicts."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params or [])
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    else:
        rows = conn.execute(sql, params or []).fetchall()
        return [dict(r) for r in rows]
