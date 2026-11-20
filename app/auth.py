"""Admin login for the dashboard: hashed password, signed session cookie.

- The password is stored only as a PBKDF2-SHA256 hash in ADMIN_PASSWORD_HASH.
  Create one with:  python -m app.auth
- After a successful login the browser gets an HttpOnly, SameSite=Strict session cookie,
  signed with a key derived from the server's secrets. Changing the password (or
  ADMIN_TOKEN / APP_SECRET) logs every session out.
- Failed logins are rate limited per IP address.
- Without ADMIN_PASSWORD_HASH the login falls back to ADMIN_TOKEN as password, so an
  existing setup keeps working (a warning is logged).

ADMIN_TOKEN still works for scripts and curl (?token=... or "Authorization: Bearer ...").
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import logging
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from html import escape
from typing import Callable

from .config import Settings

log = logging.getLogger("auth")

COOKIE_NAME = "igdm_session"
HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 600_000
LOGIN_WINDOW = 15 * 60  # seconds in which LOGIN_MAX_ATTEMPTS failures are allowed


# --- password hashing (stdlib only) ---------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, iterations: int = HASH_ITERATIONS) -> str:
    """'pbkdf2_sha256:<iterations>:<salt>:<hash>'. Uses ':' rather than '$' so the value
    can be pasted into .env without Docker Compose trying to expand it."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{HASH_SCHEME}:{iterations}:{_b64(salt)}:{_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, expected = encoded.strip().split(":")
        if scheme != HASH_SCHEME:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(dk, _unb64(expected))
    except (ValueError, TypeError):
        return False


def valid_hash(encoded: str) -> bool:
    parts = encoded.strip().split(":")
    return len(parts) == 4 and parts[0] == HASH_SCHEME and parts[1].isdigit()


# --- sessions --------------------------------------------------------------
class AdminAuth:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.s = settings
        self.clock = clock
        self._revoked: dict[str, float] = {}  # nonce -> expiry (logged out sessions)
        self._failures: dict[str, deque] = defaultdict(deque)  # ip -> failure times
        self._lock = threading.Lock()
        if self.s.admin_password_hash and not valid_hash(self.s.admin_password_hash):
            log.error("ADMIN_PASSWORD_HASH is not a valid hash; create one with "
                      "`python -m app.auth`. Admin login is disabled.")
        elif not self.s.admin_password_hash and self.s.admin_token:
            log.warning("ADMIN_PASSWORD_HASH is empty: the dashboard login uses ADMIN_TOKEN as "
                        "password. Create a hash with `python -m app.auth`.")

    @property
    def enabled(self) -> bool:
        """True when someone can log in to the dashboard."""
        if self.s.admin_password_hash:
            return valid_hash(self.s.admin_password_hash)
        return bool(self.s.admin_token)

    @property
    def session_seconds(self) -> int:
        return int(self.s.admin_session_hours * 3600)

    def check_password(self, password: str) -> bool:
        if not password or not self.enabled:
            return False
        if self.s.admin_password_hash:
            return verify_password(password, self.s.admin_password_hash)
        return hmac.compare_digest(password.encode(), self.s.admin_token.encode())

    def check_token(self, token: str) -> bool:
        """The ADMIN_TOKEN for scripts and curl."""
        return bool(self.s.admin_token and token) and hmac.compare_digest(
            token.encode(), self.s.admin_token.encode())

    def _key(self) -> bytes:
        return hashlib.sha256(
            f"admin-session|{self.s.admin_password_hash}|{self.s.admin_token}|"
            f"{self.s.app_secret}".encode()).digest()

    def _sign(self, msg: str) -> str:
        return _b64(hmac.new(self._key(), msg.encode(), hashlib.sha256).digest())

    def create_session(self) -> str:
        msg = f"v1.{int(self.clock()) + self.session_seconds}.{secrets.token_urlsafe(16)}"
        return f"{msg}.{self._sign(msg)}"

    def _parse(self, value: str) -> tuple[int, str] | None:
        parts = (value or "").split(".")
        if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
            return None
        msg = ".".join(parts[:3])
        if not hmac.compare_digest(self._sign(msg), parts[3]):
            return None
        return int(parts[1]), parts[2]

    def verify_session(self, value: str) -> bool:
        if not self.enabled or not value:
            return False
        parsed = self._parse(value)
        if parsed is None:
            return False
        exp, nonce = parsed
        if exp <= self.clock():
            return False
        with self._lock:
            return nonce not in self._revoked

    def revoke(self, value: str) -> None:
        parsed = self._parse(value)
        if parsed is None:
            return
        now = self.clock()
        with self._lock:
            self._revoked = {n: e for n, e in self._revoked.items() if e > now}
            self._revoked[parsed[1]] = parsed[0]

    # --- brute-force protection -----------------------------------------
    def _recent(self, ip: str) -> deque:
        q = self._failures[ip]
        cutoff = self.clock() - LOGIN_WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip)) >= self.s.login_max_attempts

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._recent(ip).append(self.clock())

    def reset_failures(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


def safe_next(target: str | None) -> str:
    """Only local admin paths, so the login can't redirect to another site."""
    if target and target.startswith("/admin") and not target.startswith("//") \
            and "\\" not in target and "\r" not in target and "\n" not in target:
        return target
    return "/admin"


def login_page(error: str = "", next_path: str = "/admin", using_token: bool = False) -> str:
    hint = ("Use your <code>ADMIN_TOKEN</code> as password until you set "
            "<code>ADMIN_PASSWORD_HASH</code>." if using_token else "")
    err = f'<p class="err">{escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>ig-dm-bot · sign in</title>
<style>
  body {{ margin:0; background:#0b0b0f; color:#f2f2f5;
         font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  .card {{ max-width:380px; margin:12vh auto; background:#15151c; border:1px solid #26262f;
          border-radius:14px; padding:22px 24px; }}
  h1 {{ font-size:18px; margin:0 0 6px; }}
  p {{ color:#8b8b99; font-size:14px; }} .err {{ color:#ff4d4f; }}
  input {{ background:#0b0b0f; color:#f2f2f5; border:1px solid #26262f; border-radius:8px;
          padding:10px 12px; font:inherit; width:100%; box-sizing:border-box; }}
  button {{ margin-top:12px; background:#ff3d7f; border:0; color:#fff; border-radius:8px;
           padding:9px 14px; font:inherit; cursor:pointer; }}
</style></head><body>
<form class="card" method="post" action="/admin/login">
  <h1>📩 ig-dm-bot</h1>
  <p>Sign in to the dashboard.</p>{err}
  <input type="hidden" name="next" value="{escape(next_path)}">
  <input name="password" type="password" autocomplete="current-password"
         placeholder="Password" required autofocus>
  <button type="submit">Sign in</button>
  <p>{hint}</p>
</form></body></html>"""


def main() -> int:
    """Print an ADMIN_PASSWORD_HASH line for .env (the password is never stored)."""
    pw = getpass.getpass("New admin password: ")
    if len(pw) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != pw:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
