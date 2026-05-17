"""Aurelius — models.py — SQLite schema with author + category fields."""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("aurelius.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db() -> None:
    conn = get_db()
    conn.executescript("""
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
        );

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
        );

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
        );

        -- Add missing columns to existing installs (safe no-ops if already exist)
        ALTER TABLE books ADD COLUMN author   TEXT NOT NULL DEFAULT 'Unknown Author';
        ALTER TABLE books ADD COLUMN category TEXT NOT NULL DEFAULT 'Other';
    """)
    conn.commit()
    conn.close()

def now() -> str:
    return datetime.utcnow().isoformat()
