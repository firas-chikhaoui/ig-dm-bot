"""SQLite storage: dedup, per-user conversation state, collected emails."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    key TEXT PRIMARY KEY,          -- comment id / message mid
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS triggers (
    user_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, media_id)
);
CREATE TABLE IF NOT EXISTS user_state (
    user_id TEXT PRIMARY KEY,
    username TEXT,
    flow TEXT,
    waiting_step TEXT,             -- step waiting for free text (ask_email)
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    user_id TEXT NOT NULL,
    username TEXT,
    email TEXT NOT NULL,
    list TEXT NOT NULL,
    flow TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, list)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
"""


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    # --- dedup -----------------------------------------------------------
    def mark_processed(self, key: str) -> bool:
        """Return True the first time a key is seen, False on duplicates."""
        cur = self._exec(
            "INSERT OR IGNORE INTO processed(key, created_at) VALUES (?, ?)", (key, time.time())
        )
        return cur.rowcount == 1

    def mark_triggered(self, user_id: str, media_id: str) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO triggers(user_id, media_id, created_at) VALUES (?, ?, ?)",
            (user_id, media_id, time.time()),
        )
        return cur.rowcount == 1

    # --- conversation state ---------------------------------------------
    def set_state(self, user_id: str, flow: str | None, waiting_step: str | None,
                  username: str | None = None) -> None:
        self._exec(
            """INSERT INTO user_state(user_id, username, flow, waiting_step, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = COALESCE(excluded.username, user_state.username),
                 flow = excluded.flow,
                 waiting_step = excluded.waiting_step,
                 updated_at = excluded.updated_at""",
            (user_id, username, flow, waiting_step, time.time()),
        )

    def get_state(self, user_id: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM user_state WHERE user_id = ?", (user_id,)).fetchone()

    # --- emails ----------------------------------------------------------
    def save_email(self, user_id: str, username: str | None, email: str, list_name: str,
                   flow: str | None) -> None:
        self._exec(
            """INSERT INTO emails(user_id, username, email, list, flow, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, list) DO UPDATE SET email = excluded.email,
                 created_at = excluded.created_at""",
            (user_id, username, email, list_name, flow, time.time()),
        )

    def list_emails(self, list_name: str | None = None) -> list[sqlite3.Row]:
        if list_name:
            return self._exec(
                "SELECT * FROM emails WHERE list = ? ORDER BY created_at", (list_name,)
            ).fetchall()
        return self._exec("SELECT * FROM emails ORDER BY created_at").fetchall()

    # --- stats -----------------------------------------------------------
    def log_event(self, user_id: str | None, kind: str, detail: str = "") -> None:
        self._exec(
            "INSERT INTO events(user_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (user_id, kind, detail, time.time()),
        )

    def stats(self) -> dict[str, int]:
        rows = self._exec("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind").fetchall()
        out = {r["kind"]: r["n"] for r in rows}
        out["emails"] = self._exec("SELECT COUNT(*) FROM emails").fetchone()[0]
        return out
