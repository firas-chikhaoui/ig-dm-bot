"""Tests for the dashboard's rules editor and testing rules in the playground."""
import time

from tests.test_account_rules import SHOP_RULES
from tests.test_auth import PW, client, hash_password
from tests.test_server import make_client

T = {"token": "admin"}

DRAFT = SHOP_RULES.replace("Our prices: example.com/prices", "Draft prices: example.com/new")


def comment(c, text, user="5", **extra):
    r = c.post("/playground/event", json={"kind": "comment", "text": text, "user_id": user,
                                          **extra})
    return r


def test_editor_on_dashboard(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path, hash_password(PW, iterations=1000))
    page = c.post("/admin/login", data={"password": PW}).text
    for el in ('id="rulesText"', 'id="rulesSave"', 'id="rulesTest"', 'id="rulesAccount"',
               "/admin/rules/validate", "/playground?draft=1"):
        assert el in page
    assert c.get("/admin/stats").json()["dry_run"] is True


def test_save_from_session_needs_same_origin(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path, hash_password(PW, iterations=1000))
    c.post("/admin/login", data={"password": PW})
    r = c.put("/admin/rules", content=SHOP_RULES, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403
    r = c.put("/admin/rules", content=SHOP_RULES, headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["flows"] == ["prices"]
    assert c.get("/admin/rules").headers["X-Rules-Source"] == "database"


def test_playground_runs_unsaved_draft(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    info = c.post("/playground/info", json={"rules": DRAFT}).json()
    assert info["reels"][0]["keywords"] == ["price"]

    msgs = comment(c, "price", rules=DRAFT).json()["messages"]
    assert any("Draft prices" in m["text"] for m in msgs)
    # nothing was saved: the live rules are still rules.yaml
    assert c.get("/admin/rules", params=T).headers["X-Rules-Source"] == "file"
    assert not any("Draft prices" in m["text"]
                   for m in comment(c, "price", user="6").json()["messages"])


def test_playground_rejects_invalid_draft(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    r = comment(c, "price", rules="reels: [")
    assert r.status_code == 400 and "invalid" in r.json()["detail"]
    assert c.post("/playground/info", json={"rules": 5}).status_code == 400
    assert c.post("/playground/info", content="[]",
                  headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/playground/info", json={"rules": "x" * (513 * 1024)}).status_code == 400


def test_playground_runs_saved_rules_of_another_account(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    import app.main as m
    m.db.save_account("77", "client-token", expires_at=time.time() + 30 * 86400, username="shop")
    assert c.put("/admin/rules", params={**T, "account": "77"}, content=SHOP_RULES).status_code == 200

    info = c.get("/playground/info", params={"account": "77"}).json()
    assert info["reels"][0]["keywords"] == ["price"]
    msgs = comment(c, "price", account="77").json()["messages"]
    assert any("Our prices" in m["text"] for m in msgs)
    assert m.db.account_stats("77", 30)["totals"]["matches"] == 1  # counted for account 77
    # the .env account still uses rules.yaml
    assert c.get("/playground/info").json()["reels"][0]["keywords"] != ["price"]
    assert c.get("/playground/info", params={"account": "nope"}).status_code == 404


def test_playground_page_reads_draft(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    page = c.get("/playground").text
    assert "igdm.rulesDraft" in page and 'id="banner"' in page


def test_playground_draft_needs_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    c = client(monkeypatch, tmp_path, hash_password(PW, iterations=1000), DRY_RUN="false")
    assert c.post("/playground/info", json={"rules": DRAFT}).status_code == 404
    assert comment(c, "price", rules=DRAFT).status_code == 404
