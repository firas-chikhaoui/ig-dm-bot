"""FastAPI web server: Instagram webhook + small admin API."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .bot import Bot
from .config import settings
from .instagram import InstagramAPI
from .rules import RulesStore
from .storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

rules_store = RulesStore(settings.rules_path)
db = Storage(settings.db_path)
bot = Bot(InstagramAPI(settings), rules_store, db, own_id=settings.ig_user_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    r = rules_store.get()  # fail fast if rules.yaml is broken
    log.info("loaded %d reel rule(s), %d flow(s)%s", len(r.reels), len(r.flows),
             " [DRY_RUN]" if settings.dry_run else "")
    if not settings.app_secret:
        log.warning("APP_SECRET is empty: webhook signatures are NOT verified")
    yield


app = FastAPI(title="ig-dm-bot", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# --- Webhook verification (Meta calls this once when you save the webhook URL)
@app.get("/webhook")
def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    if mode == "subscribe" and hmac.compare_digest(token, settings.verify_token):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verification failed")


def _valid_signature(raw: bytes, header: str | None) -> bool:
    if not settings.app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# --- Webhook events (comments, messages, button clicks)
@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    # Answer Meta fast (they retry on slow responses); do the work in the background.
    background.add_task(bot.handle_webhook, body)
    return {"ok": True}


# --- Admin ---------------------------------------------------------------
def _check_admin(token: str) -> None:
    if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/admin/stats")
def stats(token: str = ""):
    _check_admin(token)
    r = rules_store.get()
    return {"stats": db.stats(), "reels": len(r.reels), "flows": list(r.flows)}


@app.get("/admin/emails.csv")
def emails_csv(token: str = "", list: str | None = None):  # noqa: A002
    _check_admin(token)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "username", "user_id", "list", "flow", "created_at"])
    for row in db.list_emails(list):
        w.writerow([row["email"], row["username"], row["user_id"], row["list"], row["flow"],
                    datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=emails.csv"})


# --- Playground (only in DRY_RUN): see the bot like a follower would --------
PLAYGROUND_HTML = Path(__file__).with_name("playground.html")


def _require_dry_run() -> None:
    if not settings.dry_run:
        raise HTTPException(status_code=404, detail="playground only works with DRY_RUN=true")


@app.get("/playground")
def playground():
    _require_dry_run()
    return HTMLResponse(PLAYGROUND_HTML.read_text(encoding="utf-8"))


@app.get("/playground/info")
def playground_info():
    _require_dry_run()
    r = rules_store.get()
    return {"reels": [{"id": x.id, "name": x.name or x.flow, "keywords": x.keywords}
                      for x in r.reels],
            "dm_keywords": [k for t in r.dm_triggers for k in t.keywords]}


@app.post("/playground/event")
async def playground_event(request: Request):
    _require_dry_run()
    ev = await request.json()
    api = bot.api
    api.fake_follows = bool(ev.get("follows", False))
    uid, username = str(ev.get("user_id", "111")), ev.get("username") or "test_user"
    mid = f"pg-{time.time_ns()}"
    kind = ev.get("kind")
    if kind == "comment":
        body = {"object": "instagram", "entry": [{"changes": [{"field": "comments", "value": {
            "id": mid, "text": ev.get("text", ""), "from": {"id": uid, "username": username},
            "media": {"id": ev.get("media_id", "")}}}]}]}
    elif kind == "click":
        body = {"object": "instagram", "entry": [{"messaging": [{
            "sender": {"id": uid}, "recipient": {"id": settings.ig_user_id},
            "postback": {"mid": mid, "payload": ev.get("payload", ""), "title": "btn"}}]}]}
    elif kind == "dm":
        body = {"object": "instagram", "entry": [{"messaging": [{
            "sender": {"id": uid}, "recipient": {"id": settings.ig_user_id},
            "message": {"mid": mid, "text": ev.get("text", "")}}]}]}
    else:
        raise HTTPException(status_code=400, detail="kind must be comment, click or dm")

    start = len(api.outbox)
    bot.handle_webhook(body)
    out = []
    for item in api.outbox[start:]:
        b = item["body"]
        if item["path"].endswith("/replies"):
            out.append({"type": "public_reply", "text": b["message"]})
            continue
        m = b["message"]
        if "text" in m:
            out.append({"type": "dm", "text": m["text"], "buttons": []})
        else:
            p = m["attachment"]["payload"]
            out.append({"type": "dm", "text": p["text"], "buttons": [
                {"title": x["title"], "url": x.get("url"), "payload": x.get("payload")}
                for x in p["buttons"]]})
    return {"messages": out}
