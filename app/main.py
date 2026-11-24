"""FastAPI web server: Instagram webhook + small admin API."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import ipaddress
import asyncio
import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .alerts import setup_alerts
from .auth import COOKIE_NAME, AdminAuth, login_page, safe_next
from .bot import Bot
from .config import settings
from .oauth import CALLBACK_PATH, InstagramOAuth, OAuthError, result_page
from .privacy import (DEFAULT_DELETE_KEYWORDS, SignedRequestError, data_deletion_page,
                      delete_user, parse_signed_request, privacy_page)
from .router import AccountRouter
from .rules import AccountRulesStore, RulesError, RulesStore, parse_rules_yaml
from .storage import Storage
from .tokens import TokenManager
from .tokens import health as token_health

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
auth = AdminAuth(settings)  # dashboard login


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


_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
             "X-Frame-Options": "DENY"}


# --- Admin login -----------------------------------------------------------
# The dashboard uses a login (password hash + session cookie). Scripts and curl can
# still use ADMIN_TOKEN: ?token=... or "Authorization: Bearer ...".
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin(request: Request) -> bool:
    """CSRF check for cookie sessions: a browser sends Origin on POST/PUT/DELETE."""
    origin = request.headers.get("origin")
    if not origin:
        ref = request.headers.get("referer")
        if not ref:
            return True  # not a browser form/fetch from another site
        origin = ref
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc.lower() == host.lower()


def _has_session(request: Request) -> bool:
    return auth.verify_session(request.cookies.get(COOKIE_NAME, ""))


def _is_admin(request: Request, token: str) -> bool:
    bearer = request.headers.get("authorization", "")
    if bearer[:7].lower() == "bearer ":
        token = token or bearer[7:].strip()
    if token:
        return auth.check_token(token)
    if _has_session(request):
        if request.method in _UNSAFE and not _same_origin(request):
            raise HTTPException(status_code=403, detail="cross-site request blocked")
        return True
    return False


def require_admin(request: Request, token: str = Query("", include_in_schema=False)) -> None:
    if not _is_admin(request, token):
        raise HTTPException(status_code=401, detail="unauthorized")


ADMIN = Depends(require_admin)


def _client_ip(request: Request) -> str:
    """The visitor's IP for the login rate limit. X-Forwarded-For is only trusted when the
    request comes from a local/private address, i.e. from our Caddy reverse proxy."""
    peer = request.client.host if request.client else "?"
    try:
        trusted = ipaddress.ip_address(peer).is_private  # includes loopback
    except ValueError:
        trusted = False
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd if trusted and fwd else peer


def _cookie_secure(request: Request) -> bool:
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        return True
    pub = urlsplit(settings.public_url)
    return pub.scheme == "https" and pub.netloc == request.headers.get("host", "")


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/admin/login?next={quote(safe_next(target))}", status_code=303,
                            headers=_NO_STORE)


def _require_login_enabled() -> None:
    if not auth.enabled:
        raise HTTPException(status_code=404, detail="set ADMIN_PASSWORD_HASH in .env "
                                                    "(python -m app.auth) to use the dashboard")


def _login_html(error: str = "", next_path: str = "/admin", status: int = 200) -> HTMLResponse:
    page = login_page(error, next_path, using_token=not settings.admin_password_hash)
    return HTMLResponse(page, status_code=status, headers=_NO_STORE)


@app.get("/admin/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/admin"):  # noqa: A002
    _require_login_enabled()
    if _has_session(request):
        return RedirectResponse(safe_next(next), status_code=303, headers=_NO_STORE)
    return _login_html(next_path=safe_next(next))


@app.post("/admin/login")
async def login(request: Request):
    _require_login_enabled()
    raw = await request.body()
    form = parse_qs(raw[:8192].decode("utf-8", "replace"))
    password = form.get("password", [""])[0]
    next_path = safe_next(form.get("next", ["/admin"])[0])
    ip = _client_ip(request)
    if not _same_origin(request):
        return _login_html("Please try again.", next_path, 403)
    if auth.blocked(ip):
        log.warning("admin login blocked for %s: too many failed attempts", ip)
        return _login_html("Too many failed attempts. Try again in 15 minutes.", next_path, 429)
    if not auth.check_password(password):
        auth.record_failure(ip)
        log.warning("failed admin login from %s", ip)
        return _login_html("Wrong password.", next_path, 401)
    auth.reset_failures(ip)
    log.info("admin logged in from %s", ip)
    resp = RedirectResponse(next_path, status_code=303, headers=_NO_STORE)
    resp.set_cookie(COOKIE_NAME, auth.create_session(), max_age=auth.session_seconds,
                    path="/", httponly=True, samesite="strict", secure=_cookie_secure(request))
    return resp


@app.post("/admin/logout")
def logout(request: Request):
    if _same_origin(request):
        auth.revoke(request.cookies.get(COOKIE_NAME, ""))
    resp = RedirectResponse("/admin/login", status_code=303, headers=_NO_STORE)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- Admin -----------------------------------------------------------------
@app.get("/admin/stats", dependencies=[ADMIN])
def stats():
    r = bot.rules.get()
    return {"stats": db.stats(), "reels": len(r.reels), "flows": list(r.flows),
            "dry_run": settings.dry_run}


@app.get("/admin/activity", dependencies=[ADMIN])
def activity(days: int = Query(14, ge=1, le=90)):
    """Events per day and the most started flows, for the dashboard chart."""
    return {"days": db.daily_activity(days), "top_flows": db.top_flows()}


ADMIN_HTML = Path(__file__).with_name("admin.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    """Dashboard page. Without a login session the browser goes to /admin/login."""
    _require_login_enabled()
    if not _has_session(request):
        return _login_redirect(request)
    return HTMLResponse(ADMIN_HTML.read_text(encoding="utf-8"),
                        headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store",
                                 "Referrer-Policy": "same-origin"})


def _token_manager(account: str | None) -> TokenManager:
    """The .env account by default, or any account stored in the database."""
    if not account or account == tokens.account_id:
        return tokens
    if db.get_account(account) is None:
        raise HTTPException(status_code=404, detail="unknown account")
    return tokens.for_account(account)


@app.get("/admin/token", dependencies=[ADMIN])
def token_status(account: str | None = None):
    """Expiry and refresh status of an Instagram token (the token itself is never shown)."""
    return _token_manager(account).status()


@app.post("/admin/token/refresh", dependencies=[ADMIN])
def token_refresh(account: str | None = None):
    tm = _token_manager(account)
    if settings.dry_run:
        raise HTTPException(status_code=409, detail="not available in DRY_RUN")
    ok = tm.refresh()
    return {"refreshed": ok, **tm.status()}


def _account_info(account_id: str, row=None) -> dict:
    """One row of the dashboard's client list (the token itself is never included)."""
    st = tokens.for_account(account_id).status()
    code, label = token_health(st)
    return {**st, "rules": account_rules.source(account_id), "health": code,
            "health_label": label, "is_default": account_id == tokens.account_id,
            "connected_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()
            if row else None}


@app.get("/admin/accounts", dependencies=[ADMIN])
def accounts():
    """All connected Instagram accounts with status and token health."""
    rows = db.list_accounts()
    items = [_account_info(r["ig_user_id"], r) for r in rows]
    if tokens.account_id not in {r["ig_user_id"] for r in rows} and tokens.current():
        items.insert(0, _account_info(tokens.account_id))  # .env token not stored yet
    counts: dict[str, int] = {}
    for a in items:
        counts[a["health"]] = counts.get(a["health"], 0) + 1
    return {"accounts": items, "default": tokens.account_id, "summary": counts}


def _set_active(account_id: str, active: bool) -> dict:
    if not db.set_account_active(account_id, active):
        raise HTTPException(status_code=404, detail="unknown account")
    log.info("account %s %s from the dashboard", account_id, "resumed" if active else "paused")
    return _account_info(account_id, db.get_account(account_id))


@app.get("/admin/accounts/{account_id}/stats", dependencies=[ADMIN])
def account_stats(account_id: str, days: int = Query(30, ge=1, le=90)):
    """Matches, DMs, button clicks and emails of one account, per day (for the chart).
    The .env account also counts what was stored before stats were kept per account."""
    acc = _rules_account(account_id)
    data = db.account_stats(acc, days, include_unassigned=acc == tokens.account_id)
    return {"account": acc, "range_days": days, **data}


@app.post("/admin/accounts/{account_id}/pause", dependencies=[ADMIN])
def account_pause(account_id: str):
    """Stop answering comments/DMs and refreshing the token of one account."""
    return _set_active(account_id, False)


@app.post("/admin/accounts/{account_id}/resume", dependencies=[ADMIN])
def account_resume(account_id: str):
    return _set_active(account_id, True)


# --- Connect Instagram (OAuth) ---------------------------------------------


def _connect_url(request: Request) -> str:
    if settings.dry_run:
        raise HTTPException(status_code=409, detail="not available in DRY_RUN")
    try:
        return oauth.authorize_url(str(request.base_url))
    except OAuthError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/admin/connect")
def connect_start(request: Request, token: str = Query("", include_in_schema=False)):
    """Send the browser to Instagram's login to connect (or reconnect) an account."""
    if not _is_admin(request, token):
        if auth.enabled:
            return _login_redirect(request)
        raise HTTPException(status_code=401, detail="unauthorized")
    return RedirectResponse(_connect_url(request), status_code=302, headers=_NO_STORE)


@app.post("/admin/connect", dependencies=[ADMIN])
def connect_url(request: Request):
    """Same as GET, but returns the Instagram login URL as JSON (used by the dashboard)."""
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


@app.get("/admin/rules", dependencies=[ADMIN])
def rules_export(account: str | None = None):
    """An account's rules as YAML: its stored rules, or rules.yaml when it has none.
    The X-Rules-Source header says which ("database" or "file")."""
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


@app.post("/admin/rules/validate", dependencies=[ADMIN])
async def rules_validate(request: Request):
    """Check YAML rules without saving them."""
    try:
        r = parse_rules_yaml(await _yaml_body(request))
    except RulesError as e:
        return {"valid": False, "error": str(e)}
    return {"valid": True, "reels": len(r.reels), "flows": list(r.flows)}


@app.put("/admin/rules", dependencies=[ADMIN])
async def rules_import(request: Request, account: str | None = None):
    """Validate YAML rules and store them for one account (default: the .env account).
    Invalid rules are rejected with 400 and the live rules stay unchanged."""
    acc = _rules_account(account)
    try:
        r = account_rules.import_yaml(acc, await _yaml_body(request))
    except RulesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"saved": True, "account": acc, "reels": len(r.reels), "flows": list(r.flows)}


@app.delete("/admin/rules", dependencies=[ADMIN])
def rules_reset(account: str | None = None):
    """Delete an account's stored rules; it uses rules.yaml again."""
    acc = _rules_account(account)
    return {"deleted": account_rules.reset(acc), "account": acc, "source": "file"}


@app.post("/admin/alerts/test", dependencies=[ADMIN])
def alerts_test():
    """Send a test message to the Telegram chat to check the alert setup."""
    if alerts is None:
        raise HTTPException(status_code=409,
                            detail="Telegram alerts are off: set TELEGRAM_BOT_TOKEN and "
                                   "TELEGRAM_CHAT_ID in .env and restart")
    ok = alerts.send(f"✅ {alerts.label}: test alert. Errors will show up here.")
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram rejected the message, see logs")
    return {"sent": True}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _csv_cell(value) -> str:
    """Spreadsheet apps run cells starting with = + - @ as formulas; neutralise them."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _email_scope(account: str | None) -> dict:
    """Filter arguments for one account's emails; none = every account.
    The .env account also gets the addresses stored before emails were kept per account."""
    if not account:
        return {}
    acc = _rules_account(account)
    return {"account_id": acc, "include_unassigned": acc == tokens.account_id}


@app.get("/admin/emails.csv", dependencies=[ADMIN])
def emails_csv(list: str | None = None, account: str | None = None,  # noqa: A002
               q: str | None = None):
    """Download collected emails as CSV: all, or of one `account` and/or `list`."""
    scope = _email_scope(account)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "username", "user_id", "list", "flow", "created_at", "account_id"])
    for row in db.list_emails(list, search=q, **scope):
        w.writerow([_csv_cell(v) for v in (
            row["email"], row["username"], row["user_id"], row["list"], row["flow"],
            _iso(row["created_at"]), row["account_id"] or scope.get("account_id", ""))])
    parts = ["emails", scope.get("account_id"), list]
    name = "-".join(p for p in parts if p)
    name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) + ".csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})


@app.get("/admin/accounts/{account_id}/emails", dependencies=[ADMIN])
def account_emails(account_id: str, list: str | None = None,  # noqa: A002
                   q: str | None = Query(None, max_length=200),
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    """Emails collected by one account, newest first, with its lists and their sizes."""
    scope = _email_scope(account_id)
    rows = db.list_emails(list, search=q, limit=limit, offset=offset, newest_first=True,
                          **scope)
    return {
        "account": scope["account_id"],
        "lists": [{**x, "last_at": _iso(x["last_at"])} for x in db.email_lists(**scope)],
        "total": db.count_emails(list, search=q, **scope),
        "limit": limit, "offset": offset,
        "emails": [{"email": r["email"], "username": r["username"], "user_id": r["user_id"],
                    "list": r["list"], "flow": r["flow"], "created_at": _iso(r["created_at"])}
                   for r in rows],
    }


@app.delete("/admin/accounts/{account_id}/emails", dependencies=[ADMIN])
def account_email_delete(account_id: str, user_id: str = Query(..., min_length=1),
                         list: str = Query(..., min_length=1)):  # noqa: A002
    """Remove one person's address from one list of an account (e.g. an unsubscribe).
    Their other data stays; DELETE /admin/users/<id> removes everything."""
    n = db.delete_email(user_id, list, **_email_scope(account_id))
    if not n:
        raise HTTPException(status_code=404, detail="email not found")
    log.info("admin removed the %s address of user %s (account %s)", list, user_id, account_id)
    return {"deleted": n}


@app.delete("/admin/users/{user_id}", dependencies=[ADMIN])
def admin_delete_user(user_id: str):
    """Delete everything stored about one Instagram user (e.g. after an email request)."""
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


class _DraftRules:
    """Rules that are not saved anywhere, with the same `get()` as RulesStore."""

    def __init__(self, rules):
        self.rules = rules

    def get(self):
        return self.rules


def _playground_bot(ev: dict) -> Bot:
    """The bot the playground talks to: the .env account by default, another account
    (`account`), and/or rules that are not saved yet (`rules`, YAML from the editor).
    Every variant sends through the same dry-run client, so nothing reaches Instagram."""
    account, draft = ev.get("account"), ev.get("rules")
    if not account and draft is None:
        return bot
    acc = _rules_account(str(account) if account else None)
    if draft is None:
        rules = account_rules.for_account(acc)
    else:
        if not isinstance(draft, str) or len(draft.encode("utf-8")) > 512 * 1024:
            raise HTTPException(status_code=400, detail="rules must be YAML text up to 512 KB")
        try:
            rules = _DraftRules(parse_rules_yaml(draft))
        except RulesError as e:
            raise HTTPException(status_code=400, detail=f"draft rules are invalid: {e}")
    return Bot(bot.api, rules, db, own_id=bot.own_id, account_id=acc)


def _playground_info(ev: dict) -> dict:
    r = _playground_bot(ev).rules.get()
    return {"reels": [{"id": x.id, "name": x.name or x.flow, "keywords": x.keywords}
                      for x in r.reels],
            "dm_keywords": [k for t in r.dm_triggers for k in t.keywords]}


@app.get("/playground/info")
def playground_info(account: str | None = None):
    _require_dry_run()
    return _playground_info({"account": account})


@app.post("/playground/info")
async def playground_info_draft(request: Request):
    """Like GET /playground/info, for rules sent in the body (the editor's draft)."""
    _require_dry_run()
    return _playground_info(await _json_object(request))


async def _json_object(request: Request) -> dict:
    try:
        ev = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(ev, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return ev


@app.post("/playground/event")
async def playground_event(request: Request):
    _require_dry_run()
    ev = await _json_object(request)
    pbot = _playground_bot(ev)
    api = pbot.api
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
    pbot.handle_webhook(body)
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
