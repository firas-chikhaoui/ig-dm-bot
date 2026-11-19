"""FastAPI web server: Instagram webhook + small admin API."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import asyncio
import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .alerts import setup_alerts
from .config import settings
from .oauth import CALLBACK_PATH, InstagramOAuth, OAuthError, result_page
from .privacy import (DEFAULT_DELETE_KEYWORDS, SignedRequestError, data_deletion_page,
                      delete_user, parse_signed_request, privacy_page)
from .router import AccountRouter
from .rules import AccountRulesStore, RulesError, RulesStore, parse_rules_yaml
from .storage import Storage
from .tokens import TokenManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
# httpx logs every request URL at INFO; the token refresh URL contains the access token.
logging.getLogger("httpx").setLevel(logging.WARNING)
alerts = setup_alerts(settings)  # Telegram alerts on errors, if configured

rules_store = RulesStore(settings.rules_path)  # shared rules file, the fallback
db = Storage(settings.db_path)
account_rules = AccountRulesStore(db, rules_store)  # rules per account, in the database
tokens = TokenManager(settings, db)
tokens.seed()
# One bot + API client per Instagram account; webhook events are routed by entry id.
router = AccountRouter(settings, db, account_rules, tokens)
bot = router.default  # the .env account (also drives the playground)
oauth = InstagramOAuth(settings, db)  # "Connect Instagram"


@asynccontextmanager
async def lifespan(_: FastAPI):
    r = rules_store.get()  # fail fast if rules.yaml is broken
    log.info("loaded %d reel rule(s), %d flow(s)%s", len(r.reels), len(r.flows),
             " [DRY_RUN]" if settings.dry_run else "")
    for row in db.list_account_rules():
        account_rules.get(row["account_id"])  # logs if stored rules are broken
        log.info("account %s uses its own rules from the database", row["account_id"])
    if not settings.app_secret:
        log.warning("APP_SECRET is empty: webhook signatures are NOT verified")
    refresher = None
    if settings.token_auto_refresh and not settings.dry_run and (
            tokens.current() or db.list_accounts(active_only=True)):
        refresher = asyncio.create_task(tokens.run_forever())
    yield
    if refresher:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher


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
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    routed = router.route(body)
    for account_id in routed.paused:
        log.info("account %s is paused, ignoring its webhook events", account_id)
    # Answer Meta fast (they retry on slow responses); do the work in the background,
    # each account with its own token.
    for account_id, part in routed.bodies.items():
        background.add_task(router.bot_for(account_id).handle_webhook, part)
    resp: dict = {"ok": True}
    if routed.paused:
        resp["paused"] = routed.paused
    if routed.unknown:
        resp["unknown"] = routed.unknown
    return resp


# --- Privacy policy + data deletion (both URLs go into the Meta app settings) --
def _delete_keywords() -> list[str]:
    cfg = bot.rules.get().settings.get("data_deletion") or {}
    return list(cfg.get("keywords", DEFAULT_DELETE_KEYWORDS) or [])


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return privacy_page(settings.privacy_operator, settings.privacy_contact,
                        settings.ig_username, _delete_keywords())


@app.get("/data-deletion", response_class=HTMLResponse)
def data_deletion(code: str | None = None):
    status = None
    if code:
        row = db.get_deletion(code)
        status = dict(row) if row else None
    return data_deletion_page(settings.privacy_contact, _delete_keywords(), status, code)


@app.post("/data-deletion")
async def data_deletion_callback(request: Request):
    """Meta's Data Deletion Request Callback: delete the user and return a status URL."""
    # Form-encoded body; parsed by hand so we don't need python-multipart.
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    signed_request = (form.get("signed_request") or [""])[0]
    try:
        data = parse_signed_request(signed_request, settings.app_secret)
    except SignedRequestError as e:
        log.warning("rejected data deletion callback: %s", e)
        raise HTTPException(status_code=400, detail="invalid signed_request")
    code, rows = delete_user(db, str(data["user_id"]), "meta_callback")
    log.info("data deletion callback: user %s, %d rows deleted, code %s",
             data["user_id"], rows, code)
    base = settings.public_url or str(request.base_url).rstrip("/")
    return {"url": f"{base}/data-deletion?code={code}", "confirmation_code": code}


# --- Admin ---------------------------------------------------------------
def _check_admin(token: str) -> None:
    if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/admin/stats")
def stats(token: str = ""):
    _check_admin(token)
    r = bot.rules.get()
    return {"stats": db.stats(), "reels": len(r.reels), "flows": list(r.flows)}


@app.get("/admin/activity")
def activity(token: str = "", days: int = Query(14, ge=1, le=90)):
    """Events per day and the most started flows, for the dashboard chart."""
    _check_admin(token)
    return {"days": db.daily_activity(days), "top_flows": db.top_flows()}


ADMIN_HTML = Path(__file__).with_name("admin.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """Dashboard page. It asks for the admin token in the browser; no data without it."""
    if not settings.admin_token:
        raise HTTPException(status_code=404, detail="set ADMIN_TOKEN in .env to use the dashboard")
    return HTMLResponse(ADMIN_HTML.read_text(encoding="utf-8"),
                        headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store",
                                 "Referrer-Policy": "no-referrer"})


def _token_manager(account: str | None) -> TokenManager:
    """The .env account by default, or any account stored in the database."""
    if not account or account == tokens.account_id:
        return tokens
    if db.get_account(account) is None:
        raise HTTPException(status_code=404, detail="unknown account")
    return tokens.for_account(account)


@app.get("/admin/token")
def token_status(token: str = "", account: str | None = None):
    """Expiry and refresh status of an Instagram token (the token itself is never shown)."""
    _check_admin(token)
    return _token_manager(account).status()


@app.post("/admin/token/refresh")
def token_refresh(token: str = "", account: str | None = None):
    _check_admin(token)
    tm = _token_manager(account)
    if settings.dry_run:
        raise HTTPException(status_code=409, detail="not available in DRY_RUN")
    ok = tm.refresh()
    return {"refreshed": ok, **tm.status()}


@app.get("/admin/accounts")
def accounts(token: str = ""):
    """All connected Instagram accounts with token health (tokens are never shown)."""
    _check_admin(token)
    ids = [r["ig_user_id"] for r in db.list_accounts()]
    if tokens.account_id not in ids and tokens.current():
        ids.insert(0, tokens.account_id)  # .env token not stored yet
    return {"accounts": [{**tokens.for_account(i).status(), "rules": account_rules.source(i)}
                         for i in ids],
            "default": tokens.account_id}


# --- Connect Instagram (OAuth) ---------------------------------------------
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
             "X-Frame-Options": "DENY"}


def _connect_url(request: Request) -> str:
    if settings.dry_run:
        raise HTTPException(status_code=409, detail="not available in DRY_RUN")
    try:
        return oauth.authorize_url(str(request.base_url))
    except OAuthError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/admin/connect")
def connect_start(request: Request, token: str = ""):
    """Send the browser to Instagram's login to connect (or reconnect) an account."""
    _check_admin(token)
    return RedirectResponse(_connect_url(request), status_code=302, headers=_NO_STORE)


@app.post("/admin/connect")
def connect_url(request: Request, token: str = ""):
    """Same as GET, but returns the Instagram login URL as JSON (used by the dashboard)."""
    _check_admin(token)
    return {"url": _connect_url(request)}


@app.get(CALLBACK_PATH, response_class=HTMLResponse)
def connect_callback(request: Request, code: str = "", state: str = "",
                     error: str = "", error_description: str = ""):
    """Instagram sends the browser back here after the login. Protected by the signed,
    single-use `state` created in /admin/connect."""
    if error:
        try:
            oauth.check_state(state)  # don't let anyone show text on our domain
            msg = error_description or error
        except OAuthError:
            msg = "The login was cancelled."
        return HTMLResponse(result_page(error=f"Instagram login cancelled: {msg}"),
                            status_code=400, headers=_NO_STORE)
    try:
        result = oauth.complete(code, state, str(request.base_url))
    except OAuthError as e:
        return HTMLResponse(result_page(error=str(e)), status_code=400, headers=_NO_STORE)
    return HTMLResponse(result_page(result), headers=_NO_STORE)


def _rules_account(account: str | None) -> str:
    """The .env account by default, or any account stored in the database."""
    if not account or account == tokens.account_id:
        return tokens.account_id
    if db.get_account(account) is None:
        raise HTTPException(status_code=404, detail="unknown account")
    return account


@app.get("/admin/rules")
def rules_export(token: str = "", account: str | None = None):
    """An account's rules as YAML: its stored rules, or rules.yaml when it has none.
    The X-Rules-Source header says which ("database" or "file")."""
    _check_admin(token)
    acc = _rules_account(account)
    return PlainTextResponse(account_rules.export_yaml(acc), media_type="text/yaml",
                             headers={"X-Rules-Source": account_rules.source(acc),
                                      "Content-Disposition":
                                          f'inline; filename="rules-{acc}.yaml"'})


async def _yaml_body(request: Request) -> str:
    raw = await request.body()
    if len(raw) > 512 * 1024:
        raise HTTPException(status_code=413, detail="rules too large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="rules must be UTF-8 text")


@app.post("/admin/rules/validate")
async def rules_validate(request: Request, token: str = ""):
    """Check YAML rules without saving them."""
    _check_admin(token)
    try:
        r = parse_rules_yaml(await _yaml_body(request))
    except RulesError as e:
        return {"valid": False, "error": str(e)}
    return {"valid": True, "reels": len(r.reels), "flows": list(r.flows)}


@app.put("/admin/rules")
async def rules_import(request: Request, token: str = "", account: str | None = None):
    """Validate YAML rules and store them for one account (default: the .env account).
    Invalid rules are rejected with 400 and the live rules stay unchanged."""
    _check_admin(token)
    acc = _rules_account(account)
    try:
        r = account_rules.import_yaml(acc, await _yaml_body(request))
    except RulesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"saved": True, "account": acc, "reels": len(r.reels), "flows": list(r.flows)}


@app.delete("/admin/rules")
def rules_reset(token: str = "", account: str | None = None):
    """Delete an account's stored rules; it uses rules.yaml again."""
    _check_admin(token)
    acc = _rules_account(account)
    return {"deleted": account_rules.reset(acc), "account": acc, "source": "file"}


@app.post("/admin/alerts/test")
def alerts_test(token: str = ""):
    """Send a test message to the Telegram chat to check the alert setup."""
    _check_admin(token)
    if alerts is None:
        raise HTTPException(status_code=409,
                            detail="Telegram alerts are off: set TELEGRAM_BOT_TOKEN and "
                                   "TELEGRAM_CHAT_ID in .env and restart")
    ok = alerts.send(f"✅ {alerts.label}: test alert. Errors will show up here.")
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram rejected the message, see logs")
    return {"sent": True}


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


@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: str, token: str = ""):
    """Delete everything stored about one Instagram user (e.g. after an email request)."""
    _check_admin(token)
    code, rows = delete_user(db, user_id, "admin")
    log.info("admin deleted data of user %s (%d rows, code %s)", user_id, rows, code)
    return {"deleted_rows": rows, "confirmation_code": code}


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
    r = bot.rules.get()
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
