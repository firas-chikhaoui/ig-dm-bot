"""Tests for the "Connect Instagram" OAuth flow."""
import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.oauth import STATE_TTL, InstagramOAuth, OAuthError, result_page
from app.storage import Storage
from app.tokens import DAY, TokenManager

NOW = 1_800_000_000.0


def settings(**kw):
    base = dict(ig_app_id="12345", app_secret="shh", admin_token="admin",
                public_url="https://bot.example.com", oauth_redirect_uri="",
                ig_access_token="", ig_user_id="", dry_run=False)
    base.update(kw)
    return replace(Settings(), **base)


def instagram(calls, *, code_response=None, me=None, subscribe_status=200):
    """Fake Instagram: code exchange, long-lived exchange, /me, subscribed_apps."""
    def handler(request: httpx.Request):
        calls.append(request)
        path = request.url.path
        if request.url.host == "api.instagram.com" and path == "/oauth/access_token":
            form = parse_qs(request.content.decode())
            if form["code"] != ["good-code"]:
                return httpx.Response(400, json={"error_type": "OAuthException",
                                                 "code": 400,
                                                 "error_message": "Invalid code"})
            return httpx.Response(200, json=code_response or {"data": [{
                "access_token": "short-token", "user_id": "999",
                "permissions": ",".join(["instagram_business_basic",
                                         "instagram_business_manage_messages",
                                         "instagram_business_manage_comments"])}]})
        if path == "/access_token":
            assert request.url.params["grant_type"] == "ig_exchange_token"
            assert request.url.params["access_token"] == "short-token"
            return httpx.Response(200, json={"access_token": "long-token",
                                             "token_type": "bearer",
                                             "expires_in": 60 * DAY})
        if path.endswith("/me/subscribed_apps"):
            return httpx.Response(subscribe_status, json={"success": True})
        if path.endswith("/me"):
            assert request.headers["Authorization"] == "Bearer long-token"
            return httpx.Response(200, json=me or {"user_id": "17841400000000001",
                                                   "username": "shop", "id": "999"})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def make(s=None, **kw):
    calls = []
    db = Storage(":memory:")
    clock = {"t": NOW}
    oa = InstagramOAuth(s or settings(), db, client=instagram(calls, **kw),
                        clock=lambda: clock["t"])
    return oa, db, calls, clock


def state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def test_authorize_url():
    oa, *_ = make()
    url = oa.authorize_url()
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert q["client_id"] == ["12345"] and q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["https://bot.example.com/connect/callback"]
    assert set(q["scope"][0].split(",")) == {"instagram_business_basic",
                                             "instagram_business_manage_messages",
                                             "instagram_business_manage_comments"}
    assert "shh" not in url  # the app secret never leaves the server


def test_redirect_uri_override_and_fallback():
    oa, *_ = make(settings(oauth_redirect_uri="https://x.test/cb"))
    assert oa.redirect_uri("http://ignored/") == "https://x.test/cb"
    oa, *_ = make(settings(public_url=""))
    assert oa.redirect_uri("http://localhost:8000/") == "http://localhost:8000/connect/callback"


def test_missing_config():
    oa, *_ = make(settings(ig_app_id="", admin_token=""))
    with pytest.raises(OAuthError, match="IG_APP_ID, ADMIN_TOKEN"):
        oa.authorize_url()


def test_state_is_signed_single_use_and_expires():
    oa, _, _, clock = make()
    s1 = oa.make_state()
    oa.check_state(s1)
    with pytest.raises(OAuthError, match="already used"):
        oa.check_state(s1)
    ts, nonce, sig = oa.make_state().split(".")
    with pytest.raises(OAuthError, match="invalid"):
        oa.check_state(f"{ts}.{nonce}.{'0' * len(sig)}")
    with pytest.raises(OAuthError, match="invalid"):
        oa.check_state("garbage")
    s2 = oa.make_state()
    clock["t"] += STATE_TTL + 1
    with pytest.raises(OAuthError, match="expired"):
        oa.check_state(s2)
    # a state signed with other secrets is rejected
    other, *_ = make(settings(admin_token="different"))
    with pytest.raises(OAuthError, match="invalid"):
        oa.check_state(other.make_state())


def test_complete_saves_account_and_subscribes():
    oa, db, calls, _ = make()
    state = state_of(oa.authorize_url())
    res = oa.complete("good-code#_", state)
    assert res.ig_user_id == "17841400000000001" and res.username == "shop"
    assert res.is_new and res.webhooks_subscribed and not res.warnings
    row = db.get_account("17841400000000001")
    assert row["access_token"] == "long-token" and row["active"] == 1
    assert row["expires_at"] == NOW + 60 * DAY and row["username"] == "shop"

    code_call = calls[0]
    form = parse_qs(code_call.content.decode())
    assert form["code"] == ["good-code"]  # "#_" stripped
    assert form["redirect_uri"] == ["https://bot.example.com/connect/callback"]
    assert form["grant_type"] == ["authorization_code"]
    sub = [c for c in calls if c.url.path.endswith("/subscribed_apps")][0]
    assert sub.url.params["subscribed_fields"] == "comments,messages,messaging_postbacks"

    # the stored token is what the bot uses for this account from now on
    tm = TokenManager(oa.s, db, clock=lambda: NOW).for_account("17841400000000001")
    assert tm.current() == "long-token" and round(tm.days_left()) == 60
    assert not tm.needs_refresh()

    # reconnecting the same account replaces the token and keeps it paused/active as is
    db.set_account_active("17841400000000001", False)
    res2 = oa.complete("good-code", state_of(oa.authorize_url()))
    assert not res2.is_new and any("paused" in w for w in res2.warnings)


def test_complete_flat_response_and_missing_permissions():
    oa, db, _, _ = make(code_response={"access_token": "short-token", "user_id": 5,
                                       "permissions": ["instagram_business_basic"]},
                        subscribe_status=400)
    res = oa.complete("good-code", oa.make_state())
    assert res.missing_permissions == ["instagram_business_manage_messages",
                                       "instagram_business_manage_comments"]
    assert not res.webhooks_subscribed and len(res.warnings) == 2
    assert db.get_account("17841400000000001") is not None


def test_bad_code_is_an_error_without_saving():
    oa, db, _, _ = make()
    with pytest.raises(OAuthError, match="Invalid code"):
        oa.complete("bad-code", oa.make_state())
    assert db.list_accounts() == []
    with pytest.raises(OAuthError, match="invalid"):
        oa.complete("good-code", "forged.state.sig")
    assert db.list_accounts() == []


def test_connecting_env_account_survives_restart():
    """A token from the OAuth flow must not be replaced by the old .env token on restart."""
    s = settings(ig_access_token="env-token", ig_user_id="17841400000000001")
    oa, db, _, _ = make(s)
    TokenManager(s, db).seed()
    oa.complete("good-code", oa.make_state())
    tm = TokenManager(s, db)
    tm.seed()  # what happens on the next start
    assert tm.current() == "long-token"
    assert db.get_account("17841400000000001")["seed_hash"] == \
        hashlib.sha256(b"env-token").hexdigest()


def test_result_page_escapes():
    html = result_page(error="<script>x</script>")
    assert "<script>x" not in html and "&lt;script&gt;" in html
