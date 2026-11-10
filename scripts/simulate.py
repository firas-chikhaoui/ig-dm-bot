"""Send fake Instagram webhook events to your local bot (no Instagram needed).

Start the bot first (with DRY_RUN=true in .env), then in a 2nd terminal:

  python scripts/simulate.py comment "send me the guide"          # comment on the reel
  python scripts/simulate.py click free_guide check_follow        # tap a button
  python scripts/simulate.py dm "me@example.com"                  # type a DM

Options: --media <reel id>  --user <user id>  --username <name>  --url <bot url>
Watch the bot's terminal: every message it WOULD send is logged as [DRY_RUN].
"""
import argparse
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import yaml  # noqa: E402

from app.bot import make_payload  # noqa: E402
from app.config import settings  # noqa: E402


def first_reel_id() -> str:
    data = yaml.safe_load(Path(settings.rules_path).read_text(encoding="utf-8")) or {}
    for r in data.get("reels") or []:
        if str(r.get("id")) != "*":
            return str(r["id"])
    return "123"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["comment", "click", "dm"])
    p.add_argument("args", nargs="+")
    p.add_argument("--media", default=None)
    p.add_argument("--user", default="111")
    p.add_argument("--username", default="test_user")
    p.add_argument("--url", default="http://127.0.0.1:8000/webhook")
    a = p.parse_args()
    uid = f"{a.kind}-{time.time_ns()}"  # unique id so dedup doesn't drop it

    if a.kind == "comment":
        body = {"object": "instagram", "entry": [{"changes": [{"field": "comments", "value": {
            "id": uid, "text": " ".join(a.args),
            "from": {"id": a.user, "username": a.username},
            "media": {"id": a.media or first_reel_id()}}}]}]}
    elif a.kind == "click":
        if len(a.args) != 2:
            sys.exit("usage: click <flow> <step>")
        body = {"object": "instagram", "entry": [{"messaging": [{
            "sender": {"id": a.user}, "recipient": {"id": settings.ig_user_id},
            "postback": {"mid": uid, "payload": make_payload(*a.args), "title": "btn"}}]}]}
    else:
        body = {"object": "instagram", "entry": [{"messaging": [{
            "sender": {"id": a.user}, "recipient": {"id": settings.ig_user_id},
            "message": {"mid": uid, "text": " ".join(a.args)}}]}]}

    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if settings.app_secret:
        sig = hmac.new(settings.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"
    r = httpx.post(a.url, content=raw, headers=headers, timeout=10)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
