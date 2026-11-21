"""Keeps the long-lived Instagram access tokens alive.

Every connected Instagram account is a row in the `accounts` table with its own token
and expiry. Long-lived Instagram tokens are valid for 60 days and can be refreshed (for
another 60 days) once they are at least 24 hours old; the bot does that in the background
before they expire and always uses the stored token for API calls.

IG_ACCESS_TOKEN / IG_USER_ID from .env seed the first account on start. If you put a
*different* token into .env later (e.g. after generating a new one in the Meta
dashboard), it replaces the stored one for that account on the next start.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .config import Settings
from .storage import Storage

log = logging.getLogger("tokens")

DAY = 86400
DEFAULT_ACCOUNT = "default"  # account key used when IG_USER_ID is not set
WARN_DAYS = 7  # log loudly when a token this close to expiry could not be refreshed


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


SOON_DAYS = 10  # the dashboard marks tokens with fewer days left as "expires soon"


def health(status: dict[str, Any]) -> tuple[str, str]:
    """Short state of an account for the dashboard, from TokenManager.status():
    (code, label) with code one of missing, paused, expired, error, expiring, ok."""
    left = status.get("days_left")
    if not status.get("configured"):
        return "missing", "Not configured"
    if not status.get("active"):
        return "paused", "Paused"
    if left is not None and left <= 0:
        return "expired", "Token expired"
    if status.get("last_error"):
        return "error", "Refresh failed"
    if left is not None and left < SOON_DAYS:
        return "expiring", "Expires soon"
    return "ok", "Active"


def env_account_id(settings: Settings) -> str:
    """Key of the account configured in .env (IG_USER_ID)."""
    return settings.ig_user_id or DEFAULT_ACCOUNT


class TokenManager:
    """Token of one account. `for_account()` gives the manager of another account and
    `check_all()` keeps every active account's token fresh."""

    def __init__(self, settings: Settings, db: Storage, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, account_id: str | None = None):
        self.s = settings
        self.db = db
        self.client = client or httpx.Client(timeout=15)
        self.clock = clock
        self.account_id = account_id or env_account_id(settings)

    def for_account(self, account_id: str) -> "TokenManager":
        return TokenManager(self.s, self.db, self.client, self.clock, account_id)

    @property
    def is_env_account(self) -> bool:
        return self.account_id == env_account_id(self.s)

    def _row(self):
        return self.db.get_account(self.account_id)

    @property
    def _label(self) -> str:
        row = self._row()
        name = row["username"] if row and row["username"] else None
        return f"Instagram token of @{name}" if name else \
            f"Instagram token of account {self.account_id}"

    # --- reading ---------------------------------------------------------
    def seed(self) -> None:
        """Create the .env account in the DB: on first start, when .env has a new token,
        and when upgrading from the single-token table of older versions."""
        if not self.is_env_account:
            return
        if self._row() is None:
            legacy = self.db.pop_legacy_token()
            if legacy is not None:
                self.db.save_account(self.account_id, legacy["access_token"],
                                     expires_at=legacy["expires_at"],
                                     username=self.s.ig_username or None,
                                     seed_hash=legacy["seed_hash"],
                                     refreshed_at=legacy["refreshed_at"])
                log.info("moved the stored Instagram token to the accounts table")
        env = self.s.ig_access_token
        if not env:
            return
        row = self._row()
        if row is None or row["seed_hash"] != _hash(env):
            self.db.save_account(self.account_id, env, expires_at=None,
                                 username=self.s.ig_username or None, seed_hash=_hash(env))
            log.info("stored IG_ACCESS_TOKEN from .env; it will be refreshed automatically")
        elif self.s.ig_username and not row["username"]:
            self.db.set_account_username(self.account_id, self.s.ig_username)

    def current(self) -> str:
        row = self._row()
        if row:
            return row["access_token"]
        # Not in the DB (yet): only the .env account has a fallback.
        return self.s.ig_access_token if self.is_env_account else ""

    def days_left(self) -> float | None:
        row = self._row()
        if not row or not row["expires_at"]:
            return None
        return (row["expires_at"] - self.clock()) / DAY

    def status(self) -> dict[str, Any]:
        """Token health for the admin endpoints. Never includes the token itself."""
        row = self._row()
        left = self.days_left()
        env_fallback = self.is_env_account and bool(self.s.ig_access_token)
        return {
            "account_id": self.account_id,
            "username": row["username"] if row else (self.s.ig_username or None
                                                     if self.is_env_account else None),
            "active": bool(row["active"]) if row else env_fallback,
            "configured": bool(row) or env_fallback,
            "source": "database" if row else ("env" if env_fallback else None),
            "auto_refresh": self.s.token_auto_refresh,
            "expires_at": _iso(row["expires_at"]) if row else None,
            "days_left": round(left, 1) if left is not None else None,
            "refreshed_at": _iso(row["refreshed_at"]) if row else None,
            "last_error": row["last_error"] if row else None,
        }

    # --- refreshing ------------------------------------------------------
    def needs_refresh(self) -> bool:
        row = self._row()
        if row is None:
            return False
        if not row["expires_at"]:
            return True  # expiry unknown (token just came from .env): refresh to learn it
        return row["expires_at"] - self.clock() < self.s.token_refresh_days_before * DAY

    def refresh(self) -> bool:
        """Exchange the current token for a fresh 60-day one. Returns True on success."""
        token = self.current()
        if not token:
            return False
        try:
            r = self.client.get(f"{self.s.graph_base}/refresh_access_token",
                                params={"grant_type": "ig_refresh_token", "access_token": token})
        except httpx.HTTPError as e:  # the message would contain the URL with the token
            return self._failed(f"network error: {type(e).__name__}")
        if r.status_code >= 400:
            return self._failed(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        new = data.get("access_token")
        if not new:
            return self._failed("response had no access_token")
        now = self.clock()
        expires_at = now + int(data.get("expires_in") or 60 * DAY)
        if self._row() is None:  # .env fallback that wasn't seeded yet
            self.db.save_account(self.account_id, new, expires_at=expires_at,
                                 username=self.s.ig_username or None,
                                 seed_hash=_hash(token), refreshed_at=now)
        else:
            self.db.save_account(self.account_id, new, expires_at=expires_at,
                                 refreshed_at=now)
        log.info("%s refreshed, valid until %s", self._label, _iso(expires_at))
        return True

    def _failed(self, error: str) -> bool:
        self.db.set_account_error(self.account_id, error)
        left = self.days_left()
        if left is not None and left <= 0:
            log.error("%s EXPIRED and refresh failed (%s). Generate a new token in the Meta "
                      "dashboard and put it in IG_ACCESS_TOKEN (or reconnect the account).",
                      self._label, error)
        elif left is not None and left < WARN_DAYS:
            log.error("%s expires in %.1f days and refresh failed (%s)",
                      self._label, left, error)
        else:
            # A token younger than 24h can't be refreshed yet; that's normal right after setup.
            log.warning("%s refresh failed, will retry later: %s", self._label, error)
        return False

    def check(self) -> None:
        """One maintenance pass: refresh if needed, warn if the token is about to expire."""
        if self.s.dry_run or not self.s.token_auto_refresh:
            return
        if self.needs_refresh():
            self.refresh()
            return
        left = self.days_left()
        if left is not None and left < WARN_DAYS:
            log.warning("%s expires in %.1f days", self._label, left)

    def check_all(self) -> None:
        """Maintenance pass over every active account (paused ones are skipped)."""
        for row in self.db.list_accounts(active_only=True):
            try:
                self.for_account(row["ig_user_id"]).check()
            except Exception:  # one broken account must not stop the others
                log.exception("token check crashed for account %s", row["ig_user_id"])

    async def run_forever(self) -> None:
        """Background loop started by the web server."""
        while True:
            try:
                await asyncio.to_thread(self.check_all)
            except Exception:  # never let the loop die
                log.exception("token check crashed")
            await asyncio.sleep(max(self.s.token_check_hours, 0.1) * 3600)
