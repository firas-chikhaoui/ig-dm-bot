"""Thin client for the Instagram API (Instagram Login / graph.instagram.com)."""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from .config import Settings

log = logging.getLogger("instagram")


# Meta error codes that usually mean something must be fixed by hand
_HINTS = {
    190: "access token invalid or expired: check /admin/token or put a new IG_ACCESS_TOKEN in .env",
    10: "permission missing or the 24h messaging window is closed",
    200: "permission missing: check the app permissions in the Meta dashboard",
    4: "rate limit reached",
    17: "rate limit reached",
    32: "rate limit reached",
    613: "rate limit reached",
}


def describe_error(r: httpx.Response) -> str:
    """One readable line from a Meta error response: status, code, message, trace id."""
    try:
        err = r.json().get("error") or {}
    except ValueError:
        err = {}
    if not isinstance(err, dict) or not err:
        return f"HTTP {r.status_code}: {r.text[:300]}"
    code = err.get("code")
    parts = [f"HTTP {r.status_code}"]
    if code is not None:
        sub = err.get("error_subcode")
        parts.append(f"code {code}" + (f"/{sub}" if sub else ""))
    if err.get("type"):
        parts.append(str(err["type"]))
    text = ", ".join(parts) + f": {err.get('message', '')}"
    if code in _HINTS:
        text += f" ({_HINTS[code]})"
    if err.get("fbtrace_id"):
        text += f" [fbtrace_id {err['fbtrace_id']}]"
    return text


class InstagramAPI:
    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 token_provider: Callable[[], str] | None = None,
                 account_id: str | None = None):
        self.s = settings
        # Instagram account this client acts for (IG_USER_ID by default; "" = "me")
        self.account_id = settings.ig_user_id if account_id is None else account_id
        # Where the access token comes from (the auto-refreshed one in the DB by default
        # in main.py); falls back to IG_ACCESS_TOKEN from .env.
        self.token = token_provider or (lambda: settings.ig_access_token)
        self.base = f"{settings.graph_base}/{settings.graph_version}"
        self.client = client or httpx.Client(timeout=15)
        # DRY_RUN only: everything the bot "sends" is kept here (used by /playground)
        self.outbox: list[dict[str, Any]] = []
        self.fake_follows = settings.dry_run_follows

    # --- low level -------------------------------------------------------
    def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        if self.s.dry_run:
            log.info("[DRY_RUN] POST %s %s", path, json)
            self.outbox.append({"path": path, "body": json})
            del self.outbox[:-500]
            return {"dry_run": True}
        return self._request("POST", path, json=json)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.s.dry_run:
            log.info("[DRY_RUN] GET %s %s", path, params)
            return {}
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        """Call the API. Failures are logged as one clear ERROR line (which also triggers
        a Telegram alert when configured) and re-raised for the caller."""
        try:
            r = self.client.request(method, f"{self.base}/{path}",
                                    headers={"Authorization": f"Bearer {self.token()}"}, **kw)
        except httpx.HTTPError as e:
            log.error("Instagram API %s %s failed: network error %s: %s",
                      method, path, type(e).__name__, e)
            raise
        if r.status_code >= 400:
            log.error("Instagram API %s %s failed: %s", method, path, describe_error(r))
        r.raise_for_status()
        return r.json()

    # --- comments --------------------------------------------------------
    def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        """Public reply under the comment."""
        return self._post(f"{comment_id}/replies", {"message": text})

    # --- messages --------------------------------------------------------
    def send(self, recipient: dict[str, str], message: dict[str, Any]) -> dict[str, Any]:
        """recipient = {"id": IGSID} for normal DMs, or {"comment_id": ...} for a
        private reply to a comment (only ONE private reply per comment, within 7 days)."""
        return self._post(f"{self.account_id or 'me'}/messages",
                          {"recipient": recipient, "message": message})

    @staticmethod
    def build_message(text: str, buttons: list[dict[str, Any]] | None = None,
                      payload_for: Any = None) -> dict[str, Any]:
        """Plain text, or a button template when buttons are given."""
        if not buttons:
            return {"text": text}
        ig_buttons = []
        for b in buttons:
            if "url" in b:
                ig_buttons.append({"type": "web_url", "url": b["url"], "title": b["title"]})
            else:
                ig_buttons.append({"type": "postback", "title": b["title"],
                                   "payload": payload_for(b["next"])})
        return {
            "attachment": {
                "type": "template",
                "payload": {"template_type": "button", "text": text, "buttons": ig_buttons},
            }
        }

    # --- users -----------------------------------------------------------
    def get_user_profile(self, igsid: str) -> dict[str, Any]:
        """Needs the user to have messaged you (or clicked a button) first."""
        return self._get(igsid, {"fields": "username,name,is_user_follow_business"})

    def is_following(self, igsid: str) -> bool:
        if self.s.dry_run:
            log.info("[DRY_RUN] follow check -> %s", self.fake_follows)
            return self.fake_follows
        try:
            return bool(self.get_user_profile(igsid).get("is_user_follow_business"))
        except httpx.HTTPError:
            # the failed call itself is already logged as an error by _request
            log.warning("follow check failed for %s, treating as not following", igsid)
            return False
