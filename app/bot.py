"""The DM bot: turns webhook events into comment replies and DM flows."""
from __future__ import annotations

import logging
import re
from typing import Any

from .instagram import InstagramAPI
from .rules import RulesStore
from .storage import Storage

log = logging.getLogger("bot")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PAYLOAD_PREFIX = "igbot"
MAX_CHAIN = 10  # safety: max auto-chained steps per event
DEFAULT_FOLLOW_RETRIES = 3  # re-checks after the first "not following"


def make_payload(flow: str, step: str) -> str:
    return f"{PAYLOAD_PREFIX}|{flow}|{step}"


def parse_payload(payload: str | None) -> tuple[str, str] | None:
    if not payload:
        return None
    parts = payload.split("|")
    if len(parts) == 3 and parts[0] == PAYLOAD_PREFIX:
        return parts[1], parts[2]
    return None


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(text: str, **ctx: Any) -> str:
    return str(text).format_map(SafeDict({k: v or "" for k, v in ctx.items()}))


class Bot:
    def __init__(self, api: InstagramAPI, rules: RulesStore, db: Storage, own_id: str = ""):
        self.api = api
        self.rules = rules
        self.db = db
        self.own_id = own_id

    # ------------------------------------------------------------------
    # Webhook entry point
    # ------------------------------------------------------------------
    def handle_webhook(self, body: dict[str, Any]) -> None:
        if body.get("object") not in ("instagram", "page"):
            return
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") in ("comments", "live_comments"):
                    self._safe(self.handle_comment, change.get("value") or {})
            for event in entry.get("messaging", []):
                self._safe(self.handle_messaging, event)

    def _safe(self, fn, arg) -> None:
        try:
            fn(arg)
        except Exception:  # never let one bad event kill the webhook
            log.exception("error while handling %s", fn.__name__)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------
    def handle_comment(self, value: dict[str, Any]) -> None:
        comment_id = value.get("id")
        text = value.get("text", "")
        user = value.get("from") or {}
        user_id, username = user.get("id"), user.get("username")
        media_id = (value.get("media") or {}).get("id", "")

        if not comment_id or not user_id:
            return
        if self.own_id and user_id == self.own_id:
            return  # our own reply -> ignore, avoids loops
        if not self.db.mark_processed(f"c:{comment_id}"):
            return  # duplicate webhook delivery

        rules = self.rules.get()
        rule = rules.find_reel_rule(media_id, text)
        if not rule:
            return
        log.info("comment %s by @%s on %s matched '%s'", comment_id, username, media_id,
                 rule.name or rule.flow)

        if rule.once_per_user and not self.db.mark_triggered(user_id, media_id):
            log.info("@%s already triggered media %s, skipping", username, media_id)
            return

        reply = rule.pick_comment_reply()
        if reply:
            try:
                self.api.reply_to_comment(comment_id, render(reply, username=username))
                self.db.log_event(user_id, "comment_reply", comment_id)
            except Exception:
                log.exception("public comment reply failed")

        self.db.set_state(user_id, rule.flow, None, username=username)
        self.db.reset_follow_failures(user_id, rule.flow)
        self.db.log_event(user_id, "flow_started", rule.flow)
        # First DM must be a "private reply" to the comment.
        self.run_step(user_id, rule.flow, rules.flows[rule.flow]["start"], username,
                      via_comment=comment_id)

    # ------------------------------------------------------------------
    # DMs, button clicks
    # ------------------------------------------------------------------
    def handle_messaging(self, event: dict[str, Any]) -> None:
        sender = (event.get("sender") or {}).get("id")
        if not sender or (self.own_id and sender == self.own_id):
            return
        message = event.get("message") or {}
        if message.get("is_echo"):
            return

        postback = event.get("postback")
        key = (postback or {}).get("mid") or message.get("mid")
        if key and not self.db.mark_processed(f"m:{key}"):
            return

        state = self.db.get_state(sender)
        username = state["username"] if state else None

        # Button click (postback) or quick reply
        payload = (postback or {}).get("payload") or (message.get("quick_reply") or {}).get(
            "payload")
        target = parse_payload(payload)
        if target:
            flow, step = target
            self.db.log_event(sender, "button", f"{flow}/{step}")
            self.run_step(sender, flow, step, username)
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        # Waiting for an email?
        if state and state["waiting_step"] and state["flow"]:
            if self._handle_awaited_text(sender, state["flow"], state["waiting_step"], text,
                                         username):
                return

        # Keyword sent directly by DM
        trig = self.rules.get().find_dm_trigger(text)
        if trig:
            rules = self.rules.get()
            self.db.set_state(sender, trig.flow, None)
            self.db.reset_follow_failures(sender, trig.flow)
            self.db.log_event(sender, "flow_started", trig.flow)
            self.run_step(sender, trig.flow, rules.flows[trig.flow]["start"], username)

    def _handle_awaited_text(self, user_id: str, flow: str, step_name: str, text: str,
                             username: str | None) -> bool:
        step = self.rules.get().step(flow, step_name)
        if not step or "ask_email" not in step:
            self.db.set_state(user_id, flow, None)
            return False
        cfg = step["ask_email"]
        candidate = text.split()[0].strip(" .,;<>") if text else ""
        if not EMAIL_RE.match(candidate):
            self._send(user_id, cfg.get("invalid", "Hmm, that doesn't look like an email. "
                                                   "Can you send it again?"), username=username)
            return True
        self.db.save_email(user_id, username, candidate.lower(), cfg.get("list", "waitlist"), flow)
        self.db.set_state(user_id, flow, None)
        self.db.log_event(user_id, "email_collected", cfg.get("list", "waitlist"))
        if cfg.get("success"):
            self._send(user_id, cfg["success"], username=username, email=candidate)
        if cfg.get("next"):
            self.run_step(user_id, flow, cfg["next"], username)
        return True

    # ------------------------------------------------------------------
    # Flow engine
    # ------------------------------------------------------------------
    def run_step(self, user_id: str, flow: str, step_name: str, username: str | None,
                 via_comment: str | None = None, depth: int = 0) -> None:
        if depth > MAX_CHAIN:
            log.error("flow %s: too many chained steps, stopping (loop?)", flow)
            return
        rules = self.rules.get()
        step = rules.step(flow, step_name)
        if step is None:
            log.warning("unknown step %s/%s (rules changed?)", flow, step_name)
            return

        def recipient() -> dict[str, str]:
            return {"comment_id": via_comment} if via_comment else {"id": user_id}

        if "message" in step:
            buttons = step.get("buttons") or []
            text = render(step["message"], username=username)
            msg = self.api.build_message(text, buttons, lambda nxt: make_payload(flow, nxt))
            self.api.send(recipient(), msg)
            self.db.set_state(user_id, flow, None)
            nxt = step.get("next")
            if nxt and not buttons:
                if via_comment:
                    # Only ONE private reply is allowed per comment, so we can't
                    # keep talking until the user answers. Add a button instead.
                    log.warning("flow %s step %s: 'next' after the first DM is ignored; "
                                "use a button on the first message", flow, step_name)
                    return
                self.run_step(user_id, flow, nxt, username, depth=depth + 1)
            return

        if via_comment:
            # Follow-check / email steps need an open conversation first, so we send
            # a short opener with a button that jumps into this step.
            opener = rules.settings.get("opener", {})
            msg = self.api.build_message(
                render(opener.get("text", "Hey {username}! Tap below 👇"), username=username),
                [{"title": opener.get("button", "Continue"), "next": step_name}],
                lambda nxt: make_payload(flow, nxt),
            )
            self.api.send(recipient(), msg)
            return

        if "require_follow" in step:
            nxt = self._follow_gate(user_id, flow, step_name, step["require_follow"])
            if nxt:
                self.run_step(user_id, flow, nxt, username, depth=depth + 1)
            return

        if "ask_email" in step:
            self._send(user_id, step["ask_email"]["prompt"], username=username)
            self.db.set_state(user_id, flow, step_name)
            return

    def _follow_gate(self, user_id: str, flow: str, step_name: str,
                     rf: dict[str, Any]) -> str | None:
        """Return the next step of a follow check, or None to stay silent.

        1st failed check  -> not_following
        next failures     -> still_not_following (falls back to not_following)
        over max_retries  -> gave_up once (if set), then silence
        """
        following = self.api.is_following(user_id)
        self.db.log_event(user_id, "follow_check", "yes" if following else "no")
        if following:
            self.db.reset_follow_failures(user_id, flow)
            return rf["ok"]

        attempts = self.db.add_follow_failure(user_id, flow, step_name)
        if attempts == 1:
            return rf["not_following"]
        limit = rf.get("max_retries",
                       self.rules.get().settings.get("follow_max_retries", DEFAULT_FOLLOW_RETRIES))
        if limit is None or attempts - 1 <= int(limit):
            return rf.get("still_not_following") or rf["not_following"]
        if attempts - 1 == int(limit) + 1:
            log.info("user %s: follow check retry limit (%s) reached in flow %s",
                     user_id, limit, flow)
            self.db.log_event(user_id, "follow_gave_up", flow)
            return rf.get("gave_up")
        return None  # already told them; don't keep messaging

    def _send(self, user_id: str, text: str, **ctx: Any) -> None:
        self.api.send({"id": user_id}, {"text": render(text, **ctx)})
