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
CREATE TABLE IF NOT EXISTS follow_checks (
    user_id TEXT NOT NULL,
    flow TEXT NOT NULL,
    step TEXT NOT NULL,
    attempts INTEGER NOT NULL,     -- failed follow checks in a row
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, flow, step)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deletion_requests (
    code TEXT PRIMARY KEY,         -- confirmation code shown to the person
    source TEXT NOT NULL,          -- meta_callback / dm / admin
    deleted_rows INTEGER NOT NULL,
    created_at REAL NOT NULL       -- no user id is kept on purpose
);
CREATE TABLE IF NOT EXISTS accounts (
    ig_user_id TEXT PRIMARY KEY,   -- Instagram professional account id
    username TEXT,
    access_token TEXT NOT NULL,
    expires_at REAL,               -- unix time; NULL = unknown
    active INTEGER NOT NULL DEFAULT 1,
    seed_hash TEXT,                -- hash of the .env token this row was seeded from
    refreshed_at REAL,             -- last successful token refresh
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- Single-token table of older versions; moved into accounts on start, kept for old DBs.
CREATE TABLE IF NOT EXISTS tokens (
    name TEXT PRIMARY KEY,         -- "instagram"
    access_token TEXT NOT NULL,
    expires_at REAL,               -- unix time; NULL = unknown
    seed_hash TEXT,                -- hash of the .env token this row was seeded from
    refreshed_at REAL,             -- last successful refresh
    last_error TEXT,
    updated_at REAL NOT NULL
);
"""

# Tables holding data about a person, keyed by their Instagram user id.
USER_TABLES = ("triggers", "user_state", "emails", "follow_checks", "events")


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

    # --- follow gate -----------------------------------------------------
    def add_follow_failure(self, user_id: str, flow: str, step: str) -> int:
        """Count one more failed follow check and return the new total."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO follow_checks(user_id, flow, step, attempts, updated_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(user_id, flow, step) DO UPDATE SET
                     attempts = follow_checks.attempts + 1,
                     updated_at = excluded.updated_at""",
                (user_id, flow, step, time.time()),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT attempts FROM follow_checks WHERE user_id = ? AND flow = ? AND step = ?",
                (user_id, flow, step),
            ).fetchone()[0]

    def reset_follow_failures(self, user_id: str, flow: str | None = None) -> None:
        if flow is None:
            self._exec("DELETE FROM follow_checks WHERE user_id = ?", (user_id,))
        else:
            self._exec("DELETE FROM follow_checks WHERE user_id = ? AND flow = ?",
                       (user_id, flow))

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

    # --- data deletion ---------------------------------------------------
    def delete_user_data(self, user_id: str) -> int:
        """Delete everything stored about one person. Returns the number of rows removed."""
        total = 0
        with self._lock:
            for table in USER_TABLES:
                cur = self._conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                total += cur.rowcount
            self._conn.commit()
        return total

    def record_deletion(self, code: str, source: str, deleted_rows: int) -> None:
        self._exec(
            "INSERT OR REPLACE INTO deletion_requests(code, source, deleted_rows, created_at) "
            "VALUES (?, ?, ?, ?)",
            (code, source, deleted_rows, time.time()),
        )

    def get_deletion(self, code: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM deletion_requests WHERE code = ?", (code,)).fetchone()

    # --- accounts (connected Instagram accounts and their tokens) ----------
    def get_account(self, ig_user_id: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM accounts WHERE ig_user_id = ?",
                          (ig_user_id,)).fetchone()

    def list_accounts(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM accounts"
        if active_only:
            sql += " WHERE active = 1"
        return self._exec(sql + " ORDER BY created_at, ig_user_id").fetchall()

    def save_account(self, ig_user_id: str, access_token: str, expires_at: float | None,
                     username: str | None = None, seed_hash: str | None = None,
                     refreshed_at: float | None = None) -> None:
        """Add an account or store a (new) token for it. Clears the last error.
        username/seed_hash/refreshed_at are kept when passed as None; `active` is kept."""
        now = time.time()
        self._exec(
            """INSERT INTO accounts(ig_user_id, username, access_token, expires_at, active,
                                    seed_hash, refreshed_at, last_error, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
               ON CONFLICT(ig_user_id) DO UPDATE SET
                 username = COALESCE(excluded.username, accounts.username),
                 access_token = excluded.access_token,
                 expires_at = excluded.expires_at,
                 seed_hash = COALESCE(excluded.seed_hash, accounts.seed_hash),
                 refreshed_at = COALESCE(excluded.refreshed_at, accounts.refreshed_at),
                 last_error = NULL,
                 updated_at = excluded.updated_at""",
            (ig_user_id, username, access_token, expires_at, seed_hash, refreshed_at, now, now),
        )

    def set_account_username(self, ig_user_id: str, username: str) -> None:
        self._exec("UPDATE accounts SET username = ?, updated_at = ? WHERE ig_user_id = ?",
                   (username, time.time(), ig_user_id))

    def set_account_active(self, ig_user_id: str, active: bool) -> bool:
        """Pause (False) or resume (True) an account. Returns False if it doesn't exist."""
        cur = self._exec("UPDATE accounts SET active = ?, updated_at = ? WHERE ig_user_id = ?",
                         (1 if active else 0, time.time(), ig_user_id))
        return cur.rowcount == 1

    def set_account_error(self, ig_user_id: str, error: str | None) -> None:
        self._exec("UPDATE accounts SET last_error = ?, updated_at = ? WHERE ig_user_id = ?",
                   (error, time.time(), ig_user_id))

    def delete_account(self, ig_user_id: str) -> bool:
        return self._exec("DELETE FROM accounts WHERE ig_user_id = ?",
                          (ig_user_id,)).rowcount == 1

    def pop_legacy_token(self) -> sqlite3.Row | None:
        """Return and remove the token stored by older versions (tokens table), if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE name = 'instagram'").fetchone()
            if row is not None:
                self._conn.execute("DELETE FROM tokens WHERE name = 'instagram'")
                self._conn.commit()
            return row

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

    def daily_activity(self, days: int = 14, now: float | None = None) -> list[dict]:
        """Events per UTC day for the last `days` days, oldest first (missing days = 0)."""
        days = max(1, min(int(days), 90))
        now = time.time() if now is None else now
        today = int(now // 86400)
        first = today - days + 1
        rows = self._exec(
            "SELECT CAST(created_at / 86400 AS INTEGER) AS day, kind, COUNT(*) AS n "
            "FROM events WHERE created_at >= ? GROUP BY day, kind",
            (first * 86400,),
        ).fetchall()
        by_day: dict[int, dict[str, int]] = {d: {} for d in range(first, today + 1)}
        for r in rows:
            if r["day"] in by_day:
                by_day[r["day"]][r["kind"]] = r["n"]
        return [{"date": time.strftime("%Y-%m-%d", time.gmtime(d * 86400)), "counts": c}
                for d, c in by_day.items()]

    def top_flows(self, limit: int = 10) -> list[dict]:
        """How often each flow was started, most popular first."""
        rows = self._exec(
            "SELECT detail AS flow, COUNT(*) AS n FROM events WHERE kind = 'flow_started' "
            "GROUP BY detail ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
        return [{"flow": r["flow"], "started": r["n"]} for r in rows]
