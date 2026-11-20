"""Tests for the admin login: password hashing, sessions, rate limit and the web flow."""
import importlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.auth import AdminAuth, hash_password, safe_next, valid_hash, verify_password
from app.config import Settings

PW = "correct horse battery"


@pytest.fixture(scope="module")
def pw_hash():
    return hash_password(PW, iterations=1000)  # fast for tests


def make_auth(**kw):
    clock = {"t": 1_000_000.0}
    s = replace(Settings(), **{"admin_token": "", "admin_password_hash": "", "app_secret": "x",
                               "admin_session_hours": 1.0, "login_max_attempts": 3, **kw})
    return AdminAuth(s, clock=lambda: clock["t"]), clock


def test_hash_and_verify(pw_hash):
    assert valid_hash(pw_hash) and "$" not in pw_hash
    assert verify_password(PW, pw_hash)
    assert not verify_password("wrong", pw_hash)
    assert not verify_password(PW, "garbage")
    assert hash_password(PW, 1000) != pw_hash  # random salt
    with pytest.raises(ValueError):
        hash_password("")


def test_disabled_without_secrets():
    a, _ = make_auth()
    assert not a.enabled and not a.check_password("") and not a.check_password("x")
    assert not a.verify_session(a.create_session())
    a, _ = make_auth(admin_password_hash="not-a-hash", admin_token="tok")
    assert not a.enabled and not a.check_password("tok")


def test_token_fallback_and_check_token():
    a, _ = make_auth(admin_token="tok")
    assert a.enabled and a.check_password("tok") and not a.check_password("nope")
    assert a.check_token("tok") and not a.check_token("") and not a.check_token("x")
    b, _ = make_auth(admin_token="tok", admin_password_hash=hash_password(PW, 1000))
    assert not b.check_password("tok") and b.check_password(PW)  # hash wins over the token


def test_session_signed_expires_and_revokes(pw_hash):
    a, clock = make_auth(admin_password_hash=pw_hash)
    s = a.create_session()
    assert a.verify_session(s)
    v, exp, nonce, sig = s.split(".")
    assert not a.verify_session(f"{v}.{int(exp) + 9999}.{nonce}.{sig}")  # tampered expiry
    assert not a.verify_session("garbage") and not a.verify_session("")
    # another password hash = other key: old sessions are invalid
    other, _ = make_auth(admin_password_hash=hash_password(PW, 1000))
    assert not other.verify_session(s)
    clock["t"] += 3601
    assert not a.verify_session(s)
    s2 = a.create_session()
    a.revoke(s2)
    assert not a.verify_session(s2)


def test_rate_limit():
    a, clock = make_auth(admin_token="tok")
    for _ in range(3):
        assert not a.blocked("1.2.3.4")
        a.record_failure("1.2.3.4")
    assert a.blocked("1.2.3.4") and not a.blocked("5.6.7.8")
    clock["t"] += 15 * 60 + 1
    assert not a.blocked("1.2.3.4")


def test_safe_next():
    assert safe_next("/admin") == "/admin"
    assert safe_next("/admin/connect") == "/admin/connect"
    for bad in ("https://evil.test", "//evil.test", "/\\evil", "/playground", "", None):
        assert safe_next(bad) == "/admin"


def client(monkeypatch, tmp_path, pw_hash, **env):
    base = {"VERIFY_TOKEN": "abc", "APP_SECRET": "secret", "ADMIN_TOKEN": "admin",
            "ADMIN_PASSWORD_HASH": pw_hash, "DB_PATH": str(tmp_path / "bot.db"),
            "DRY_RUN": "true", "LOGIN_MAX_ATTEMPTS": "3", **env}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    import app.config
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app)


def test_login_flow(monkeypatch, tmp_path, pw_hash):
    c = client(monkeypatch, tmp_path, pw_hash)
    page = c.get("/admin/login")
    assert page.status_code == 200 and 'name="password"' in page.text
    assert page.headers["cache-control"] == "no-store"

    r = c.post("/admin/login", data={"password": "admin"})  # the token is no password now
    assert r.status_code == 401 and "Wrong password" in r.text
    assert "igdm_session" not in c.cookies

    r = c.post("/admin/login", data={"password": PW, "next": "https://evil.test"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/admin").status_code == 200
    assert c.get("/admin/stats").status_code == 200
    assert c.get("/admin/login", follow_redirects=False).status_code == 303  # already in

    # CSRF: a cookie request from another site is blocked
    r = c.post("/admin/alerts/test", headers={"Origin": "https://evil.test"})
    assert r.status_code == 403
    # same-origin requests pass the check (409 = alerts not configured)
    assert c.post("/admin/alerts/test", headers={"Origin": "http://testserver"}).status_code == 409

    r = c.post("/admin/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert c.get("/admin/stats").status_code == 401


def test_revoked_cookie_cannot_be_reused(monkeypatch, tmp_path, pw_hash):
    c = client(monkeypatch, tmp_path, pw_hash)
    c.post("/admin/login", data={"password": PW})
    session = c.cookies.get("igdm_session")
    c.post("/admin/logout")
    c.cookies.set("igdm_session", session)
    assert c.get("/admin/stats").status_code == 401


def test_token_still_works_for_scripts(monkeypatch, tmp_path, pw_hash):
    c = client(monkeypatch, tmp_path, pw_hash)
    assert c.get("/admin/stats").status_code == 401
    assert c.get("/admin/stats", params={"token": "admin"}).status_code == 200
    assert c.get("/admin/stats", headers={"Authorization": "Bearer admin"}).status_code == 200
    assert c.get("/admin/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
    # a wrong token is not rescued by a valid session
    c.post("/admin/login", data={"password": PW})
    assert c.get("/admin/stats", params={"token": "nope"}).status_code == 401


def test_login_rate_limited(monkeypatch, tmp_path, pw_hash):
    c = client(monkeypatch, tmp_path, pw_hash)
    for _ in range(3):
        assert c.post("/admin/login", data={"password": "x"}).status_code == 401
    r = c.post("/admin/login", data={"password": PW})
    assert r.status_code == 429 and "igdm_session" not in c.cookies


def test_secure_cookie_behind_https(monkeypatch, tmp_path, pw_hash):
    c = client(monkeypatch, tmp_path, pw_hash)
    r = c.post("/admin/login", data={"password": PW}, follow_redirects=False,
               headers={"X-Forwarded-Proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_dashboard_off_without_any_secret(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path, "", ADMIN_TOKEN="")
    assert c.get("/admin").status_code == 404
    assert c.get("/admin/login").status_code == 404
    assert c.post("/admin/login", data={"password": ""}).status_code == 404
    assert c.get("/admin/stats").status_code == 401
