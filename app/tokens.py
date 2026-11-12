"""Keeps the long-lived Instagram access token alive.

Long-lived Instagram tokens are valid for 60 days and can be refreshed (for another
60 days) once they are at least 24 hours old. The token from IG_ACCESS_TOKEN is copied
into the database on first start; from then on the bot refreshes it in the background
before it expires and uses the stored token for every API call.

If you put a *different* token into .env later (e.g. after generating a new one in the
Meta dashboard), it replaces the stored one on the next start.
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
WARN_DAYS = 7  # log loudly when a token this close to expiry could not be refreshed


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


class TokenManager:
    NAME = "instagram"

    def __init__(self, settings: Settings, db: Storage, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.s = settings
        self.db = db
        self.client = client or httpx.Client(timeout=15)
        self.clock = clock

    # --- reading ---------------------------------------------------------
    def seed(self) -> None:
        """Copy IG_ACCESS_TOKEN into the DB (first start, or when .env has a new token)."""
        env = self.s.ig_access_token
        if not env:
            return
        row = self.db.get_token(self.NAME)
        if row is None or row["seed_hash"] != _hash(env):
            self.db.save_token(self.NAME, env, expires_at=None, seed_hash=_hash(env))
            log.info("stored IG_ACCESS_TOKEN from .env; it will be refreshed automatically")

    def current(self) -> str:
        row = self.db.get_token(self.NAME)
        return row["access_token"] if row else self.s.ig_access_token

    def days_left(self) -> float | None:
        row = self.db.get_token(self.NAME)
        if not row or not row["expires_at"]:
            return None
        return (row["expires_at"] - self.clock()) / DAY

    def status(self) -> dict[str, Any]:
        """Token health for the admin endpoint. Never includes the token itself."""
        row = self.db.get_token(self.NAME)
        left = self.days_left()
        return {
            "configured": bool(row or self.s.ig_access_token),
            "source": "database" if row else ("env" if self.s.ig_access_token else None),
            "auto_refresh": self.s.token_auto_refresh,
            "expires_at": _iso(row["expires_at"]) if row else None,
            "days_left": round(left, 1) if left is not None else None,
            "refreshed_at": _iso(row["refreshed_at"]) if row else None,
            "last_error": row["last_error"] if row else None,
        }

    # --- refreshing ------------------------------------------------------
    def needs_refresh(self) -> bool:
        row = self.db.get_token(self.NAME)
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
        self.db.save_token(self.NAME, new, expires_at=expires_at, refreshed_at=now)
        log.info("Instagram token refreshed, valid until %s", _iso(expires_at))
        return True

    def _failed(self, error: str) -> bool:
        if self.db.get_token(self.NAME) is not None:
            self.db.set_token_error(self.NAME, error)
        left = self.days_left()
        if left is not None and left <= 0:
            log.error("Instagram token EXPIRED and refresh failed (%s). Generate a new token "
                      "in the Meta dashboard and put it in IG_ACCESS_TOKEN.", error)
        elif left is not None and left < WARN_DAYS:
            log.error("Instagram token expires in %.1f days and refresh failed (%s)", left, error)
        else:
            # A token younger than 24h can't be refreshed yet; that's normal right after setup.
            log.warning("Instagram token refresh failed, will retry later: %s", error)
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
            log.warning("Instagram token expires in %.1f days", left)

    async def run_forever(self) -> None:
        """Background loop started by the web server."""
        while True:
            try:
                await asyncio.to_thread(self.check)
            except Exception:  # never let the loop die
                log.exception("token check crashed")
            await asyncio.sleep(max(self.s.token_check_hours, 0.1) * 3600)
