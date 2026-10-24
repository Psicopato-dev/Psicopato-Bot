"""Хранилище: SQLite (только стандартная библиотека)."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL DEFAULT '',
    info       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS passwords (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service      TEXT NOT NULL,
    login_enc    BLOB NOT NULL,
    password_enc BLOB NOT NULL,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(sql, args).fetchall()


def one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with closing(_connect()) as conn, conn:
        return conn.execute(sql, args).lastrowid


def get_meta(key: str) -> bytes | None:
    row = one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else None


def set_meta(key: str, value: bytes) -> None:
    execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
