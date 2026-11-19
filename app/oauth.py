""""Connect Instagram": the OAuth flow of Instagram API with Instagram Login.

1. The admin opens /admin/connect. The bot sends the browser to Instagram's login and
   consent screen with a signed, single-use `state`.
2. Instagram redirects back to /connect/callback with a short-lived `code`.
3. The bot exchanges the code for a short-lived token, then for a 60-day long-lived token,
   reads the account id and username, subscribes the account to the webhook fields the bot
   needs, and stores the account. From then on the token is refreshed like any other.

Tokens never appear in logs, pages or exceptions raised from here.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from html import escape
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from .config import Settings
from .storage import Storage
from .tokens import DAY, env_account_id

log = logging.getLogger("oauth")

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
CODE_EXCHANGE_URL = "https://api.instagram.com/oauth/access_token"
CALLBACK_PATH = "/connect/callback"
SCOPES = ("instagram_business_basic", "instagram_business_manage_messages",
          "instagram_business_manage_comments")
WEBHOOK_FIELDS = ("comments", "messages", "messaging_postbacks")
STATE_TTL = 15 * 60  # seconds the admin has to finish the Instagram login


class OAuthError(Exception):
    """Something the person connecting the account should see (never contains a token)."""


@dataclass
class ConnectResult:
    ig_user_id: str
    username: str | None
    expires_at: float
    is_new: bool
    permissions: list[str] = field(default_factory=list)
    missing_permissions: list[str] = field(default_factory=list)
    webhooks_subscribed: bool = False
    warnings: list[str] = field(default_factory=list)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InstagramOAuth:
    def __init__(self, settings: Settings, db: Storage, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.s = settings
        self.db = db
        self.client = client or httpx.Client(timeout=15)
        self.clock = clock
        self._used: dict[str, float] = {}  # nonce -> time used (single-use states)
        self._lock = threading.Lock()

    # --- configuration ---------------------------------------------------
    def missing_config(self) -> list[str]:
        missing = [name for name, value in (("IG_APP_ID", self.s.ig_app_id),
                                            ("APP_SECRET", self.s.app_secret),
                                            ("ADMIN_TOKEN", self.s.admin_token)) if not value]
        return missing

    def redirect_uri(self, base_url: str = "") -> str:
        """OAUTH_REDIRECT_URI, else PUBLIC_URL + /connect/callback, else the request URL."""
        if self.s.oauth_redirect_uri:
            return self.s.oauth_redirect_uri
        base = self.s.public_url or base_url.rstrip("/")
        return f"{base}{CALLBACK_PATH}"

    # --- state (CSRF protection) ----------------------------------------
    def _key(self) -> bytes:
        return hashlib.sha256(
            f"oauth-state|{self.s.app_secret}|{self.s.admin_token}".encode()).digest()

    def _sign(self, msg: str) -> str:
        return hmac.new(self._key(), msg.encode(), hashlib.sha256).hexdigest()[:32]

    def make_state(self) -> str:
        msg = f"{int(self.clock())}.{secrets.token_urlsafe(12)}"
        return f"{msg}.{self._sign(msg)}"

    def check_state(self, state: str) -> None:
        """Valid signature, not older than STATE_TTL, not used before."""
        parts = (state or "").split(".")
        if len(parts) != 3:
            raise OAuthError("The login link is invalid. Start again from the dashboard.")
        ts, nonce, sig = parts
        if not hmac.compare_digest(sig, self._sign(f"{ts}.{nonce}")):
            raise OAuthError("The login link is invalid. Start again from the dashboard.")
        now = self.clock()
        try:
            age = now - int(ts)
        except ValueError:
            raise OAuthError("The login link is invalid. Start again from the dashboard.")
        if age > STATE_TTL or age < -60:
            raise OAuthError("The login link has expired. Start again from the dashboard.")
        with self._lock:
            for n, t in list(self._used.items()):
                if now - t > STATE_TTL:
                    del self._used[n]
            if nonce in self._used:
                raise OAuthError("This login link was already used. Start again from the "
                                 "dashboard.")
            self._used[nonce] = now

    # --- step 1 ----------------------------------------------------------
    def authorize_url(self, base_url: str = "") -> str:
        missing = self.missing_config()
        if missing:
            raise OAuthError("Set " + ", ".join(missing) + " in .env and restart.")
        params = {
            "client_id": self.s.ig_app_id,
            "redirect_uri": self.redirect_uri(base_url),
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": self.make_state(),
            "enable_fb_login": "0",
            "force_reauth": "true",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    # --- step 2 + 3 ------------------------------------------------------
    def complete(self, code: str, state: str, base_url: str = "") -> ConnectResult:
        """Handle the callback: check state, exchange the code, store the account."""
        self.check_state(state)
        code = (code or "").strip()
        if code.endswith("#_"):  # Instagram appends this to the redirect
            code = code[:-2]
        if not code:
            raise OAuthError("Instagram did not send a login code. Start again from the "
                             "dashboard.")
        short, user_id, permissions = self._exchange_code(code, self.redirect_uri(base_url))
        token, expires_in = self._long_lived(short)
        profile = self._profile(token)
        ig_user_id = str(profile.get("user_id") or user_id or "")
        if not ig_user_id:
            raise OAuthError("Instagram did not return the account id.")
        username = profile.get("username") or None

        now = self.clock()
        expires_at = now + expires_in
        is_new = self.db.get_account(ig_user_id) is None
        # For the .env account, remember the current .env token as "seen" so the next
        # restart does not overwrite the freshly connected token with the .env one.
        seed_hash = None
        if ig_user_id == env_account_id(self.s) and self.s.ig_access_token:
            seed_hash = _token_hash(self.s.ig_access_token)
        self.db.save_account(ig_user_id, token, expires_at=expires_at, username=username,
                             seed_hash=seed_hash, refreshed_at=now)

        result = ConnectResult(ig_user_id=ig_user_id, username=username,
                               expires_at=expires_at, is_new=is_new,
                               permissions=permissions,
                               missing_permissions=[p for p in SCOPES if permissions
                                                    and p not in permissions])
        if result.missing_permissions:
            result.warnings.append("Not all permissions were granted: "
                                   + ", ".join(result.missing_permissions)
                                   + ". Connect again and allow everything.")
        result.webhooks_subscribed = self._subscribe_webhooks(token)
        if not result.webhooks_subscribed:
            result.warnings.append("Could not subscribe the account to webhooks; check the "
                                   "logs and the webhook setup in the Meta dashboard.")
        account = self.db.get_account(ig_user_id)
        if account is not None and not account["active"]:
            result.warnings.append("This account is paused. Resume it to let the bot answer.")
        log.info("connected Instagram account %s (@%s), token valid until %s",
                 ig_user_id, username or "?",
                 time.strftime("%Y-%m-%d", time.gmtime(expires_at)))
        return result

    def _call(self, method: str, url: str, what: str, **kw: Any) -> dict[str, Any]:
        try:
            r = self.client.request(method, url, **kw)
        except httpx.HTTPError as e:  # the message could contain the URL with a token
            log.error("Instagram OAuth: %s failed: network error %s", what, type(e).__name__)
            raise OAuthError(f"Could not reach Instagram ({what}). Try again.")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or not isinstance(data, dict):
            err = data.get("error") if isinstance(data, dict) else None
            msg = (err.get("message") if isinstance(err, dict) else None) or \
                (data.get("error_message") if isinstance(data, dict) else None) or \
                r.text[:200]
            log.error("Instagram OAuth: %s failed: HTTP %s: %s", what, r.status_code, msg)
            raise OAuthError(f"Instagram refused the {what}: {msg}")
        return data

    def _exchange_code(self, code: str, redirect_uri: str) -> tuple[str, str, list[str]]:
        data = self._call("POST", CODE_EXCHANGE_URL, "login code exchange", data={
            "client_id": self.s.ig_app_id,
            "client_secret": self.s.app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        })
        # Newer responses wrap the result in {"data": [...]}
        if isinstance(data.get("data"), list) and data["data"]:
            data = data["data"][0]
        token = data.get("access_token")
        if not token:
            raise OAuthError("Instagram did not return an access token.")
        perms = data.get("permissions") or []
        if isinstance(perms, str):
            perms = [p.strip() for p in perms.split(",") if p.strip()]
        return token, str(data.get("user_id") or ""), list(perms)

    def _long_lived(self, short_token: str) -> tuple[str, int]:
        data = self._call("GET", f"{self.s.graph_base}/access_token", "long-lived token "
                          "exchange", params={"grant_type": "ig_exchange_token",
                                              "client_secret": self.s.app_secret,
                                              "access_token": short_token})
        token = data.get("access_token")
        if not token:
            raise OAuthError("Instagram did not return a long-lived token.")
        return token, int(data.get("expires_in") or 60 * DAY)

    def _profile(self, token: str) -> dict[str, Any]:
        return self._call("GET", f"{self.s.graph_base}/{self.s.graph_version}/me",
                          "profile lookup", params={"fields": "user_id,username"},
                          headers={"Authorization": f"Bearer {token}"})

    def _subscribe_webhooks(self, token: str) -> bool:
        try:
            data = self._call("POST", f"{self.s.graph_base}/{self.s.graph_version}/me/"
                              "subscribed_apps", "webhook subscription",
                              params={"subscribed_fields": ",".join(WEBHOOK_FIELDS)},
                              headers={"Authorization": f"Bearer {token}"})
        except OAuthError:
            return False
        return bool(data.get("success", True))


# --- result page ------------------------------------------------------------
_STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;
margin:60px auto;padding:0 16px;line-height:1.6;color:#1a1a1a;background:#fff}
h1{font-size:1.6em}.box{border:1px solid #ddd;border-radius:8px;padding:12px 16px;
background:#fafafa}.warn{color:#9a5b00}a.btn{display:inline-block;margin-top:16px;
padding:8px 14px;border-radius:6px;background:#1a1a1a;color:#fff;text-decoration:none}
"""


def result_page(result: ConnectResult | None = None, error: str | None = None) -> str:
    if error is not None:
        title = "Instagram not connected"
        body = (f"<h1>❌ {title}</h1><p>{escape(error)}</p>")
    else:
        assert result is not None
        title = "Instagram connected"
        name = f"@{escape(result.username)}" if result.username else \
            f"account {escape(result.ig_user_id)}"
        until = time.strftime("%Y-%m-%d", time.gmtime(result.expires_at))
        what = "is now connected" if result.is_new else "was reconnected with a new token"
        body = (f"<h1>✅ {title}</h1><div class='box'><p><b>{name}</b> {what}.</p>"
                f"<p>Account id: <code>{escape(result.ig_user_id)}</code><br>"
                f"Token valid until {until} (refreshed automatically).</p></div>")
        if result.is_new:
            body += ("<p>Until you give it its own rules, the account uses "
                     "<code>rules.yaml</code>.</p>")
        for w in result.warnings:
            body += f"<p class='warn'>⚠️ {escape(w)}</p>"
    body += "<a class='btn' href='/admin'>Back to the dashboard</a>"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<meta name='robots' content='noindex'>"
            f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
            f"<body>{body}</body></html>")
