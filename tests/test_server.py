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


def _signed_request(payload: dict, secret: str) -> str:
    import base64

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    body = b64(json.dumps(payload).encode())
    sig = b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{sig}.{body}"


def test_privacy_and_data_deletion_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIVACY_CONTACT", "me@example.com")
    c = make_client(monkeypatch, tmp_path)
    r = c.get("/privacy")
    assert r.status_code == 200 and "Privacy Policy" in r.text
    assert "me@example.com" in r.text and "delete my data" in r.text
    r = c.get("/data-deletion")
    assert r.status_code == 200 and "Data Deletion" in r.text
    assert "not found" in c.get("/data-deletion", params={"code": "nope"}).text


def test_data_deletion_callback(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com/")
    c = make_client(monkeypatch, tmp_path)
    import app.main
    db = app.main.db
    db.save_email("555", "bob", "bob@x.io", "waitlist", "f")

    sr = _signed_request({"algorithm": "HMAC-SHA256", "user_id": "555"}, "secret")
    r = c.post("/data-deletion", data={"signed_request": sr})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == f"https://bot.example.com/data-deletion?code={body['confirmation_code']}"
    assert not db.list_emails()
    assert "completed" in c.get("/data-deletion",
                                params={"code": body["confirmation_code"]}).text

    bad = _signed_request({"algorithm": "HMAC-SHA256", "user_id": "555"}, "wrong")
    assert c.post("/data-deletion", data={"signed_request": bad}).status_code == 400
    assert c.post("/data-deletion", data={"signed_request": "garbage"}).status_code == 400


def test_admin_delete_user(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    import app.main
    app.main.db.save_email("777", "eve", "eve@x.io", "waitlist", "f")
    assert c.delete("/admin/users/777").status_code == 401
    r = c.delete("/admin/users/777", params={"token": "admin"})
    assert r.status_code == 200 and r.json()["deleted_rows"] == 1
    assert not app.main.db.list_emails()
