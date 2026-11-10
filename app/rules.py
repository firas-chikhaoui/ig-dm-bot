"""Load, validate and hot-reload rules.yaml."""
from __future__ import annotations

import logging
import os
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import yaml

log = logging.getLogger("rules")

MATCH_MODES = ("contains", "word", "exact")
STEP_KINDS = ("message", "require_follow", "ask_email")


class RulesError(ValueError):
    pass


@dataclass
class ReelRule:
    id: str  # media id, or "*" for every post/reel
    keywords: list[str]
    flow: str
    name: str = ""
    match: str = "word"
    comment_replies: list[str] = field(default_factory=list)
    once_per_user: bool = True

    def matches(self, media_id: str, text: str) -> bool:
        if self.id != "*" and str(self.id) != str(media_id):
            return False
        if not self.keywords:  # no keywords = any comment triggers
            return True
        t = (text or "").strip().lower()
        for kw in self.keywords:
            k = kw.strip().lower()
            if not k:
                continue
            if self.match == "exact" and t == k:
                return True
            if self.match == "contains" and k in t:
                return True
            if self.match == "word" and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", t):
                return True
        return False

    def pick_comment_reply(self) -> str | None:
        return random.choice(self.comment_replies) if self.comment_replies else None


@dataclass
class Rules:
    reels: list[ReelRule]
    flows: dict[str, dict[str, Any]]  # flow name -> {"start": str, "steps": {...}}
    settings: dict[str, Any]
    dm_triggers: list[ReelRule] = field(default_factory=list)

    def find_dm_trigger(self, text: str) -> ReelRule | None:
        for r in self.dm_triggers:
            if r.keywords and r.matches("*", text):
                return r
        return None

    def find_reel_rule(self, media_id: str, text: str) -> ReelRule | None:
        # Specific reel ids win over the "*" catch-all.
        specific = [r for r in self.reels if r.id != "*"]
        wildcard = [r for r in self.reels if r.id == "*"]
        for r in specific + wildcard:
            if r.matches(media_id, text):
                return r
        return None

    def step(self, flow: str, step: str) -> dict[str, Any] | None:
        return self.flows.get(flow, {}).get("steps", {}).get(step)


def parse_rules(data: dict[str, Any]) -> Rules:
    if not isinstance(data, dict):
        raise RulesError("rules.yaml must be a mapping")
    settings = data.get("settings") or {}
    default_match = settings.get("default_match", "word")

    flows = data.get("flows") or {}
    if not isinstance(flows, dict):
        raise RulesError("'flows' must be a mapping of flow_name -> flow")

    for fname, flow in flows.items():
        steps = (flow or {}).get("steps")
        if not isinstance(steps, dict) or not steps:
            raise RulesError(f"flow '{fname}' needs a non-empty 'steps' mapping")
        flow.setdefault("start", next(iter(steps)))
        if flow["start"] not in steps:
            raise RulesError(f"flow '{fname}': start step '{flow['start']}' not found")
        for sname, step in steps.items():
            _validate_step(fname, sname, step, steps)

    reels: list[ReelRule] = []
    for i, raw in enumerate(data.get("reels") or []):
        if "id" not in raw or "flow" not in raw:
            raise RulesError(f"reels[{i}] needs 'id' and 'flow'")
        kws = raw.get("keywords") or raw.get("keyword") or []
        if isinstance(kws, str):
            kws = [kws]
        replies = raw.get("comment_replies") or raw.get("comment_reply") or []
        if isinstance(replies, str):
            replies = [replies]
        match = raw.get("match", default_match)
        if match not in MATCH_MODES:
            raise RulesError(f"reels[{i}]: match must be one of {MATCH_MODES}")
        if raw["flow"] not in flows:
            raise RulesError(f"reels[{i}]: flow '{raw['flow']}' is not defined")
        reels.append(
            ReelRule(
                id=str(raw["id"]),
                name=raw.get("name", ""),
                keywords=[str(k) for k in kws],
                flow=raw["flow"],
                match=match,
                comment_replies=[str(r) for r in replies],
                once_per_user=bool(raw.get("once_per_user", True)),
            )
        )
    # Optional: keywords sent directly by DM (not in a comment) can start a flow too.
    dm_triggers: list[ReelRule] = []
    for i, raw in enumerate(data.get("dm_triggers") or []):
        kws = raw.get("keywords") or []
        if isinstance(kws, str):
            kws = [kws]
        if raw.get("flow") not in flows:
            raise RulesError(f"dm_triggers[{i}]: flow '{raw.get('flow')}' is not defined")
        dm_triggers.append(ReelRule(id="*", keywords=[str(k) for k in kws], flow=raw["flow"],
                                    match=raw.get("match", default_match), once_per_user=False))

    for r in reels:
        start = flows[r.flow]["start"]
        if "message" not in flows[r.flow]["steps"][start]:
            log.warning("flow '%s' is used by a reel but its start step '%s' is not a "
                        "'message' step; a short opener with a button will be sent first",
                        r.flow, start)

    return Rules(reels=reels, flows=flows, settings=settings, dm_triggers=dm_triggers)


def _validate_step(fname: str, sname: str, step: Any, steps: dict) -> None:
    where = f"flow '{fname}' step '{sname}'"
    if not isinstance(step, dict):
        raise RulesError(f"{where}: must be a mapping")
    kinds = [k for k in STEP_KINDS if k in step]
    if len(kinds) != 1:
        raise RulesError(f"{where}: needs exactly one of {STEP_KINDS}")

    def check_target(target: Any, label: str) -> None:
        if target is not None and target not in steps:
            raise RulesError(f"{where}: {label} -> unknown step '{target}'")

    kind = kinds[0]
    if kind == "message":
        buttons = step.get("buttons") or []
        if len(buttons) > 3:
            raise RulesError(f"{where}: Instagram allows max 3 buttons")
        for b in buttons:
            if "title" not in b or ("next" not in b and "url" not in b):
                raise RulesError(f"{where}: each button needs 'title' and 'next' or 'url'")
            if len(b["title"]) > 20:
                raise RulesError(f"{where}: button title '{b['title']}' > 20 chars")
            check_target(b.get("next"), "button")
        check_target(step.get("next"), "next")
    elif kind == "require_follow":
        rf = step["require_follow"] or {}
        if "ok" not in rf or "not_following" not in rf:
            raise RulesError(f"{where}: require_follow needs 'ok' and 'not_following'")
        check_target(rf["ok"], "ok")
        check_target(rf["not_following"], "not_following")
    elif kind == "ask_email":
        ae = step["ask_email"] or {}
        if "prompt" not in ae:
            raise RulesError(f"{where}: ask_email needs 'prompt'")
        check_target(ae.get("next"), "next")


class RulesStore:
    """Holds the current rules and reloads them when the file changes."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._rules: Rules | None = None

    def get(self) -> Rules:
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
            except FileNotFoundError:
                raise RulesError(f"rules file not found: {self.path}")
            if self._rules is None or mtime != self._mtime:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        new = parse_rules(yaml.safe_load(f) or {})
                except (RulesError, yaml.YAMLError) as e:
                    if self._rules is None:
                        raise
                    # Keep the last good rules instead of breaking the live bot.
                    log.error("rules.yaml is invalid, keeping previous rules: %s", e)
                    self._mtime = mtime
                    return self._rules
                if self._rules is not None:
                    log.info("rules.yaml changed -> reloaded")
                self._rules, self._mtime = new, mtime
            return self._rules
