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
-- One address per person, list and account (unique index emails_owner, see _migrate).
CREATE TABLE IF NOT EXISTS emails (
    user_id TEXT NOT NULL,
    username TEXT,
    email TEXT NOT NULL,
    list TEXT NOT NULL,
    flow TEXT,
    created_at REAL NOT NULL,
    account_id TEXT                -- NULL = stored before emails were kept per account
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
CREATE TABLE IF NOT EXISTS account_rules (
    account_id TEXT PRIMARY KEY,   -- Instagram account id (or "default")
    yaml TEXT NOT NULL,            -- validated rules, same format as rules.yaml
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

# Event kinds shown in the per-account stats -> metric name.
STAT_KINDS = {"flow_started": "matches", "dm_sent": "dms", "button": "clicks",
              "email_collected": "emails"}

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns that older databases don't have yet."""
        for table in ("events", "emails"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if "account_id" not in cols:
                # NULL = stored before stats were kept per account (the .env account)
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS events_account_time "
                           "ON events(account_id, created_at)")
        # Older versions kept one address per person and list (PRIMARY KEY (user_id, list)),
        # so a person on the "waitlist" of two accounts overwrote the first one. Rebuild
        # the table without that key; uniqueness now includes the account.
        if any(r[5] for r in self._conn.execute("PRAGMA table_info(emails)")):
            self._conn.executescript("""
                ALTER TABLE emails RENAME TO emails_old;
                CREATE TABLE emails (user_id TEXT NOT NULL, username TEXT, email TEXT NOT NULL,
                    list TEXT NOT NULL, flow TEXT, created_at REAL NOT NULL, account_id TEXT);
                INSERT INTO emails(user_id, username, email, list, flow, created_at, account_id)
                    SELECT user_id, username, email, list, flow, created_at, account_id
                    FROM emails_old;
                DROP TABLE emails_old;
            """)
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS emails_owner "
                           "ON emails(IFNULL(account_id, ''), user_id, list)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS emails_account_time "
                           "ON emails(account_id, created_at)")

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
                   flow: str | None, account_id: str | None = None) -> None:
        """Store (or update) the address of one person on one list of one account.
        A row stored before emails were kept per account is taken over by the account."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE emails SET email = ?, created_at = ?, flow = COALESCE(?, flow), "
                "username = COALESCE(?, username) "
                "WHERE IFNULL(account_id, '') = IFNULL(?, '') AND user_id = ? AND list = ?",
                (email, now, flow, username, account_id, user_id, list_name))
            if cur.rowcount == 0 and account_id is not None:
                cur = self._conn.execute(
                    "UPDATE emails SET email = ?, created_at = ?, flow = COALESCE(?, flow), "
                    "username = COALESCE(?, username), account_id = ? "
                    "WHERE account_id IS NULL AND user_id = ? AND list = ?",
                    (email, now, flow, username, account_id, user_id, list_name))
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO emails(user_id, username, email, list, flow, created_at, "
                    "account_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, username, email, list_name, flow, now, account_id))
            self._conn.commit()

    @staticmethod
    def _email_filter(list_name: str | None, account_id: str | None,
                      include_unassigned: bool, search: str | None) -> tuple[str, list]:
        where, args = [], []
        if account_id is not None:
            where.append("(account_id = ? OR account_id IS NULL)" if include_unassigned
                         else "account_id = ?")
            args.append(account_id)
        if list_name:
            where.append("list = ?")
            args.append(list_name)
        if search:
            like = "%" + search.lower().replace("\\", "\\\\").replace("%", "\\%") \
                .replace("_", "\\_") + "%"
            where.append("(LOWER(email) LIKE ? ESCAPE '\\' OR LOWER(IFNULL(username, '')) "
                         "LIKE ? ESCAPE '\\')")
            args += [like, like]
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list_emails(self, list_name: str | None = None, account_id: str | None = None,
                    include_unassigned: bool = False, search: str | None = None,
                    limit: int | None = None, offset: int = 0,
                    newest_first: bool = False) -> list[sqlite3.Row]:
        """Collected addresses, optionally of one account and/or list.
        include_unassigned also returns rows stored before emails were kept per account
        (pass it for the .env account); search matches email and username."""
        where, args = self._email_filter(list_name, account_id, include_unassigned, search)
        sql = f"SELECT * FROM emails{where} ORDER BY created_at" + (" DESC" if newest_first
                                                                    else "")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [int(limit), int(offset)]
        return self._exec(sql, tuple(args)).fetchall()

    def count_emails(self, list_name: str | None = None, account_id: str | None = None,
                     include_unassigned: bool = False, search: str | None = None) -> int:
        where, args = self._email_filter(list_name, account_id, include_unassigned, search)
        return self._exec(f"SELECT COUNT(*) FROM emails{where}", tuple(args)).fetchone()[0]

    def email_lists(self, account_id: str | None = None,
                    include_unassigned: bool = False) -> list[dict]:
        """The lists of one account (or of all) with their number of addresses."""
        where, args = self._email_filter(None, account_id, include_unassigned, None)
        rows = self._exec(f"SELECT list, COUNT(*) AS n, MAX(created_at) AS last FROM emails"
                          f"{where} GROUP BY list ORDER BY list", tuple(args)).fetchall()
        return [{"list": r["list"], "count": r["n"], "last_at": r["last"]} for r in rows]

    def delete_email(self, user_id: str, list_name: str, account_id: str | None = None,
                     include_unassigned: bool = False) -> int:
        """Remove one person's address from one list (e.g. they unsubscribed)."""
        where, args = self._email_filter(list_name, account_id, include_unassigned, None)
        return self._exec(f"DELETE FROM emails{where} AND user_id = ?",
                          (*args, user_id)).rowcount

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
        """Delete an account, its token and its stored rules."""
        with self._lock:
            n = self._conn.execute("DELETE FROM accounts WHERE ig_user_id = ?",
                                   (ig_user_id,)).rowcount
            self._conn.execute("DELETE FROM account_rules WHERE account_id = ?", (ig_user_id,))
            self._conn.commit()
        return n == 1

    # --- rules per account -------------------------------------------------
    def get_account_rules(self, account_id: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM account_rules WHERE account_id = ?",
                          (account_id,)).fetchone()

    def save_account_rules(self, account_id: str, yaml_text: str) -> None:
        """Store (already validated) rules for one account."""
        with self._lock:
            # updated_at must change on every save: it is the cache key of the parsed rules.
            prev = self._conn.execute("SELECT updated_at FROM account_rules WHERE account_id = ?",
                                      (account_id,)).fetchone()
            now = time.time()
            if prev is not None and now <= prev[0]:
                now = prev[0] + 1e-6
            self._conn.execute(
                """INSERT INTO account_rules(account_id, yaml, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET yaml = excluded.yaml,
                     updated_at = excluded.updated_at""",
                (account_id, yaml_text, now))
            self._conn.commit()

    def delete_account_rules(self, account_id: str) -> bool:
        return self._exec("DELETE FROM account_rules WHERE account_id = ?",
                          (account_id,)).rowcount == 1

    def list_account_rules(self) -> list[sqlite3.Row]:
        return self._exec("SELECT account_id, updated_at FROM account_rules "
                          "ORDER BY account_id").fetchall()

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
    def log_event(self, user_id: str | None, kind: str, detail: str = "",
                  account_id: str | None = None, at: float | None = None) -> None:
        self._exec(
            "INSERT INTO events(user_id, kind, detail, created_at, account_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, detail, time.time() if at is None else at, account_id),
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

    def account_stats(self, account_id: str, days: int = 30, include_unassigned: bool = False,
                      now: float | None = None) -> dict:
        """Matches, DMs, button clicks and emails of one account: totals (all time and for
        the last `days` UTC days) and one row per day, oldest first (missing days = 0).

        include_unassigned also counts rows stored before stats were kept per account
        (account_id NULL); pass it for the .env account, which handled all of them."""
        days = max(1, min(int(days), 90))
        now = time.time() if now is None else now
        today = int(now // 86400)
        first = today - days + 1
        where = "(account_id = ? OR account_id IS NULL)" if include_unassigned else \
            "account_id = ?"
        kinds = tuple(STAT_KINDS)
        marks = ",".join("?" * len(kinds))
        totals = {m: 0 for m in STAT_KINDS.values()}
        window = dict(totals)
        for r in self._exec(
                f"SELECT kind, COUNT(*) AS n, SUM(created_at >= ?) AS recent FROM events "
                f"WHERE {where} AND kind IN ({marks}) GROUP BY kind",
                (first * 86400, account_id, *kinds)).fetchall():
            totals[STAT_KINDS[r["kind"]]] = r["n"]
            window[STAT_KINDS[r["kind"]]] = r["recent"] or 0
        # emails: the stored addresses (one per person and list), not the events
        totals["emails"] = self._exec(f"SELECT COUNT(*) FROM emails WHERE {where}",
                                      (account_id,)).fetchone()[0]
        by_day = {d: {m: 0 for m in STAT_KINDS.values()} for d in range(first, today + 1)}
        for r in self._exec(
                f"SELECT CAST(created_at / 86400 AS INTEGER) AS day, kind, COUNT(*) AS n "
                f"FROM events WHERE {where} AND kind IN ({marks}) AND created_at >= ? "
                f"GROUP BY day, kind", (account_id, *kinds, first * 86400)).fetchall():
            if r["day"] in by_day:
                by_day[r["day"]][STAT_KINDS[r["kind"]]] = r["n"]
        return {"totals": totals, "last_days": window, "days": [
            {"date": time.strftime("%Y-%m-%d", time.gmtime(d * 86400)), **c}
            for d, c in by_day.items()]}
