"""Tests for the dashboard's client list: account status, pause and resume."""
import time

from app.tokens import DAY, health
from tests.test_auth import PW, client
from tests.test_server import make_client

T = {"params": {"token": "admin"}}


def st(**kw):
    return {"configured": True, "active": True, "days_left": 40.0, "last_error": None, **kw}


def test_health_states():
    assert health(st()) == ("ok", "Active")
    assert health(st(days_left=None))[0] == "ok"
    assert health(st(configured=False, active=False))[0] == "missing"
    assert health(st(active=False, days_left=-1))[0] == "paused"  # paused wins
    assert health(st(days_left=-0.5, last_error="x"))[0] == "expired"
    assert health(st(last_error="HTTP 400"))[0] == "error"
    assert health(st(days_left=3.0)) == ("expiring", "Expires soon")


def setup_accounts(monkeypatch, tmp_path):
    monkeypatch.setenv("IG_ACCESS_TOKEN", "secret-ig-token")
    monkeypatch.setenv("IG_USER_ID", "42")
    c = make_client(monkeypatch, tmp_path)
    import app.main as m
    m.db.save_account("77", "client-token", expires_at=time.time() + 3 * DAY,
                      username="shop")
    m.db.save_account("88", "other-token", expires_at=time.time() + 50 * DAY,
                      username="cafe")
    m.db.set_account_error("88", "HTTP 400: bad")
    return c, m


def test_client_list(monkeypatch, tmp_path):
    c, _ = setup_accounts(monkeypatch, tmp_path)
    r = c.get("/admin/accounts", **T)
    data = r.json()
    by_id = {a["account_id"]: a for a in data["accounts"]}
    assert list(by_id) == ["42", "77", "88"]
    assert by_id["42"]["is_default"] and not by_id["77"]["is_default"]
    assert by_id["77"]["health"] == "expiring" and by_id["77"]["username"] == "shop"
    assert by_id["88"]["health"] == "error" and by_id["88"]["health_label"] == "Refresh failed"
    assert by_id["77"]["connected_at"] and by_id["77"]["rules"] == "file"
    assert data["summary"]["expiring"] == 1 and data["summary"]["error"] == 1
    for secret in ("secret-ig-token", "client-token", "other-token"):
        assert secret not in r.text


def test_pause_and_resume(monkeypatch, tmp_path):
    c, m = setup_accounts(monkeypatch, tmp_path)
    assert c.post("/admin/accounts/77/pause").status_code == 401
    r = c.post("/admin/accounts/77/pause", **T)
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["health"] == "paused"
    assert not m.db.get_account("77")["active"]
    assert m.router.resolve("77")[1] == "paused"  # webhook events are ignored now
    summary = c.get("/admin/accounts", **T).json()["summary"]
    assert summary["paused"] == 1

    r = c.post("/admin/accounts/77/resume", **T)
    assert r.json()["active"] is True and r.json()["health"] == "expiring"
    assert m.router.resolve("77")[1] == "ok"
    assert c.post("/admin/accounts/nope/pause", **T).status_code == 404


def test_pause_from_dashboard_session(monkeypatch, tmp_path):
    from tests.test_auth import hash_password
    c = client(monkeypatch, tmp_path, hash_password(PW, iterations=1000),
               IG_ACCESS_TOKEN="tok", IG_USER_ID="42")
    page = c.post("/admin/login", data={"password": PW})
    assert page.status_code == 200 and 'id="accounts"' in page.text
    # cross-site requests can't pause an account
    r = c.post("/admin/accounts/42/pause", headers={"Origin": "https://evil.test"})
    assert r.status_code == 403
    r = c.post("/admin/accounts/42/pause", headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["is_default"] and not r.json()["active"]
