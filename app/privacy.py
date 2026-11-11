"""Privacy policy page, data deletion page and Meta's data deletion callback.

Meta requires every app to publish a privacy policy URL and a way for people to
delete their data (either an instructions page or a callback URL). This module
provides both, plus the function that actually removes a person's data.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from html import escape

from .storage import Storage

DEFAULT_DELETE_KEYWORDS = ["delete my data"]
DEFAULT_DELETE_REPLY = ("Done ✅ Everything we stored about you has been deleted. "
                        "Confirmation code: {code}")


class SignedRequestError(ValueError):
    pass


def delete_user(db: Storage, user_id: str, source: str) -> tuple[str, int]:
    """Delete all stored data of one person; return (confirmation_code, rows_deleted)."""
    rows = db.delete_user_data(str(user_id))
    code = secrets.token_hex(8)
    db.record_deletion(code, source, rows)
    return code, rows


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parse_signed_request(signed_request: str, app_secret: str) -> dict:
    """Verify and decode a Meta `signed_request` (HMAC-SHA256 with the app secret)."""
    if not app_secret:
        raise SignedRequestError("APP_SECRET is not set")
    try:
        sig_b64, payload_b64 = signed_request.split(".", 1)
        sig = _b64url_decode(sig_b64)
        data = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as e:
        raise SignedRequestError("malformed signed_request") from e
    if str(data.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise SignedRequestError("unsupported algorithm")
    expected = hmac.new(app_secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise SignedRequestError("bad signature")
    if not data.get("user_id"):
        raise SignedRequestError("no user_id in signed_request")
    return data


def matches_delete_keyword(text: str, keywords: list[str]) -> bool:
    t = " ".join(text.lower().strip(" .!?").split())
    return any(t == " ".join(k.lower().split()) for k in keywords if k.strip())


# --- HTML pages -------------------------------------------------------------
_STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;
margin:40px auto;padding:0 16px;line-height:1.6;color:#1a1a1a;background:#fff}
h1{font-size:1.8em;margin-bottom:.2em}h2{font-size:1.2em;margin-top:1.8em}
code{background:#f2f2f2;padding:2px 6px;border-radius:4px}.muted{color:#666}
.box{border:1px solid #ddd;border-radius:8px;padding:12px 16px;background:#fafafa}
"""


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
            f"<body>{body}</body></html>")


def _contact_html(contact: str) -> str:
    if not contact:
        return "the account owner by direct message on Instagram"
    c = escape(contact)
    return f'<a href="mailto:{c}">{c}</a>' if "@" in contact else c


def privacy_page(operator: str, contact: str, account: str, keywords: list[str]) -> str:
    who = escape(operator or account or "The operator of this Instagram account")
    acct = f" (@{escape(account)})" if account else ""
    kw = escape(keywords[0]) if keywords else ""
    dm_line = (f"<li>Send <code>{kw}</code> to us in an Instagram direct message. "
               "Your data is deleted right away and you get a confirmation code.</li>"
               if kw else "")
    body = f"""
<h1>Privacy Policy</h1>
<p class="muted">Last updated: {datetime.now(timezone.utc):%Y-%m-%d}</p>
<p>{who}{acct} uses this service to reply automatically to Instagram comments and
direct messages. This page explains what data it processes and how you can have it deleted.</p>

<h2>What we collect</h2>
<ul>
<li>Your Instagram user ID and username, when you comment on one of our posts or
send us a direct message.</li>
<li>The keyword you commented and which buttons you tapped in our messages, so the
conversation can continue where you left off.</li>
<li>Whether you follow our account, checked only when a message asks you to follow first.
Only the yes/no result is kept, in a simple activity log.</li>
<li>Your email address, only if you type it in our messages yourself (for example
to join a waitlist).</li>
</ul>

<h2>How we use it</h2>
<p>Only to send you the messages you asked for, to avoid sending you the same message twice,
to send you the content or updates you signed up for, and for simple usage counts.
We do not sell your data, use it for advertising or share it with third parties,
except that messages are delivered through Meta's Instagram platform.</p>

<h2>Where it is stored</h2>
<p>On a server controlled by {who}. Data is kept until you ask for it to be deleted
or until it is no longer needed.</p>

<h2>Your rights and how to delete your data</h2>
<ul>
{dm_line}
<li>Remove this app in Instagram under <em>Settings → Website permissions → Apps and
websites</em>, then request deletion. We receive the request from Meta and delete your data
automatically.</li>
<li>Or contact {_contact_html(contact)}.</li>
</ul>
<p>See <a href="/data-deletion">data deletion instructions</a>.</p>

<h2>Contact</h2>
<p>Questions about this policy: {_contact_html(contact)}.</p>
"""
    return _page("Privacy Policy", body)


def data_deletion_page(contact: str, keywords: list[str], status: dict | None = None,
                       code: str | None = None) -> str:
    status_html = ""
    if code is not None:
        if status:
            when = datetime.fromtimestamp(status["created_at"], timezone.utc)
            status_html = (f'<div class="box"><strong>Request {escape(code)}: completed</strong>'
                           f"<br>Your data was deleted on {when:%Y-%m-%d %H:%M} UTC.</div>")
        else:
            status_html = (f'<div class="box"><strong>Request {escape(code)}: not found.</strong>'
                           "<br>Check the code, or contact us below.</div>")
    kw = escape(keywords[0]) if keywords else ""
    dm = (f"<li><strong>By direct message:</strong> send <code>{kw}</code> to our Instagram "
          "account. The bot deletes your data immediately and replies with a confirmation "
          "code.</li>" if kw else "")
    body = f"""
<h1>Data Deletion</h1>
{status_html}
<p>You can ask us to delete everything this service has stored about you
(your Instagram ID, username, email address and conversation history).</p>
<ol>
{dm}
<li><strong>Through Instagram:</strong> open Instagram → <em>Settings → Website permissions →
Apps and websites</em>, remove this app and choose to delete your data. Meta forwards
the request to us and it is processed automatically.</li>
<li><strong>By contacting us:</strong> {_contact_html(contact)}. Include your Instagram
username.</li>
</ol>
<p>Deletion is permanent. See also our <a href="/privacy">privacy policy</a>.</p>
"""
    return _page("Data Deletion", body)
