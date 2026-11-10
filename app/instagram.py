"""Thin client for the Instagram API (Instagram Login / graph.instagram.com)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("instagram")


class InstagramAPI:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
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
        r = self.client.post(
            f"{self.base}/{path}",
            json=json,
            headers={"Authorization": f"Bearer {self.s.ig_access_token}"},
        )
        if r.status_code >= 400:
            log.error("POST %s failed %s: %s", path, r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.s.dry_run:
            log.info("[DRY_RUN] GET %s %s", path, params)
            return {}
        r = self.client.get(
            f"{self.base}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.s.ig_access_token}"},
        )
        if r.status_code >= 400:
            log.error("GET %s failed %s: %s", path, r.status_code, r.text)
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
        return self._post(f"{self.s.ig_user_id or 'me'}/messages",
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
            log.exception("follow check failed for %s", igsid)
            return False
