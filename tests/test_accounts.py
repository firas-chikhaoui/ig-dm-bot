"""Tests for the accounts command line tool and the /admin/accounts endpoint."""
import hashlib
import hmac
import io
import json
from dataclasses import replace

from app.accounts_cli import run
from app.config import Settings
from app.storage import Storage


def cli(db, *argv, token="", settings=None):
    s = settings or replace(Settings(), ig_access_token="", ig_user_id="")
    out = io.StringIO()
    code = run(list(argv), s, db, out=out, read_token=lambda: token)
    return code, out.getvalue()


def test_cli_add_list_pause_remove():
    db = Storage(":memory:")
    code, text = cli(db, "add", "1784", "--username", "shop", token="secret-tok")
    assert code == 0 and "secret-tok" not in text
    assert db.get_account("1784")["access_token"] == "secret-tok"
    code, text = cli(db, "list")
    rows = json.loads(text)
    assert rows[0]["account_id"] == "1784" and rows[0]["username"] == "shop"
    assert "secret-tok" not in text
    assert cli(db, "pause", "1784")[0] == 0 and db.get_account("1784")["active"] == 0
    assert cli(db, "resume", "1784")[0] == 0 and db.get_account("1784")["active"] == 1
    assert cli(db, "pause", "nope")[0] == 1
    assert cli(db, "add", "5", token="  ")[0] == 1 and db.get_account("5") is None
    assert cli(db, "remove", "1784")[0] == 0 and db.get_account("1784") is None
    assert cli(db, "remove", "1784")[0] == 1


def test_cli_seeds_env_account():
    db = Storage(":memory:")
    s = replace(Settings(), ig_access_token="env-tok", ig_user_id="42", ig_username="me")
    code, text = cli(db, "list", settings=s)
    assert json.loads(text)[0]["account_id"] == "42"
    code, text = cli(db, "remove", "42", settings=s)
    assert "comes back on the next start" in text


def test_admin_accounts_endpoint_and_pause(monkeypatch, tmp_path):
    from tests.test_server import make_client
    monkeypatch.setenv("IG_ACCESS_TOKEN", "secret-ig-token")
    monkeypatch.setenv("IG_USER_ID", "42")
    c = make_client(monkeypatch, tmp_path)
    import app.main as m
    m.db.save_account("77", "other-token", expires_at=None, username="client")

    assert c.get("/admin/accounts").status_code == 401
    r = c.get("/admin/accounts", params={"token": "admin"})
    data = r.json()
    assert data["default"] == "42"
    assert [a["account_id"] for a in data["accounts"]] == ["42", "77"]
    assert "secret-ig-token" not in r.text and "other-token" not in r.text

    r = c.get("/admin/token", params={"token": "admin", "account": "77"})
    assert r.json()["username"] == "client"
    assert c.get("/admin/token", params={"token": "admin", "account": "x"}).status_code == 404

    # A paused default account ignores webhook events
    m.db.set_account_active("42", False)
    calls = []
    monkeypatch.setattr(m.bot, "handle_webhook", lambda body: calls.append(body))
    body = {"object": "instagram", "entry": []}
    raw = json.dumps(body).encode()
    headers = {"X-Hub-Signature-256":
               "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()}
    r = c.post("/webhook", content=raw, headers=headers)
    assert r.json().get("paused") is True and calls == []
    m.db.set_account_active("42", True)
    r = c.post("/webhook", content=raw, headers=headers)
    assert "paused" not in r.json() and calls == [body]
