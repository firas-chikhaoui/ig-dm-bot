"""Tests for the web server: webhook verification and signature check."""
import hashlib
import hmac
import importlib
import json

from fastapi.testclient import TestClient


def make_client(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIFY_TOKEN", "abc")
    monkeypatch.setenv("APP_SECRET", "secret")
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    import app.config
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app)


def test_verify_and_signature(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    r = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "abc",
                                  "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no",
                                     "hub.challenge": "42"}).status_code == 403

    body = json.dumps({"object": "instagram", "entry": []}).encode()
    assert c.post("/webhook", content=body,
                  headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert c.post("/webhook", content=body,
                  headers={"X-Hub-Signature-256": f"sha256={sig}"}).status_code == 200

    assert c.get("/admin/stats").status_code == 401
    assert c.get("/admin/stats", params={"token": "admin"}).status_code == 200
    assert c.get("/admin/emails.csv", params={"token": "admin"}).text.startswith("email,")
