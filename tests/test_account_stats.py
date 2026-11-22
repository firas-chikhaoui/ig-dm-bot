"""Tests for the per-account stats (matches, DMs, clicks, emails) and the 30-day chart."""
import sqlite3
import time

from app.storage import Storage
from tests.test_dashboard import T, setup_accounts

DAY = 86400
NOW = 1_700_000_000.0  # fixed clock for the day buckets


def test_account_stats_per_day():
    db = Storage(":memory:")
    db.log_event("u1", "flow_started", "f", account_id="77", at=NOW)
    db.log_event("u1", "button", "f/s", account_id="77", at=NOW)
    db.log_event(None, "dm_sent", account_id="77", at=NOW)
    db.log_event(None, "dm_sent", account_id="77", at=NOW - 2 * DAY)
    db.log_event("u1", "email_collected", "waitlist", account_id="77", at=NOW - 2 * DAY)
    db.log_event("u2", "flow_started", "f", account_id="77", at=NOW - 40 * DAY)  # too old
    db.log_event("u3", "flow_started", "f", account_id="88", at=NOW)  # other account
    db.log_event("u4", "follow_check", "yes", account_id="77", at=NOW)  # not a stat
    db.save_email("u1", "ann", "a@b.co", "waitlist", "f", account_id="77")

    s = db.account_stats("77", days=30, now=NOW)
    assert len(s["days"]) == 30
    assert s["days"][-1] == {"date": time.strftime("%Y-%m-%d", time.gmtime(NOW)),
                             "matches": 1, "dms": 1, "clicks": 1, "emails": 0}
    assert s["days"][-3]["dms"] == 1 and s["days"][-3]["emails"] == 1
    assert s["last_days"] == {"matches": 1, "dms": 2, "clicks": 1, "emails": 1}
    assert s["totals"] == {"matches": 2, "dms": 2, "clicks": 1, "emails": 1}
    assert db.account_stats("88", now=NOW)["totals"]["matches"] == 1
    assert db.account_stats("99", now=NOW)["totals"] == {
        "matches": 0, "dms": 0, "clicks": 0, "emails": 0}


def test_unassigned_rows_count_for_default_account():
    db = Storage(":memory:")
    db.log_event("u1", "flow_started", "f", at=NOW)  # stored without account
    db.save_email("u1", None, "a@b.co", "waitlist", "f")
    assert db.account_stats("42", now=NOW)["totals"]["matches"] == 0
    s = db.account_stats("42", now=NOW, include_unassigned=True)
    assert s["totals"]["matches"] == 1 and s["totals"]["emails"] == 1


def test_old_database_gets_account_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
            kind TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL);
        CREATE TABLE emails (user_id TEXT NOT NULL, username TEXT, email TEXT NOT NULL,
            list TEXT NOT NULL, flow TEXT, created_at REAL NOT NULL,
            PRIMARY KEY (user_id, list));
        INSERT INTO events(user_id, kind, detail, created_at) VALUES ('u', 'button', 'x', 1);
    """)
    conn.commit()
    conn.close()
    db = Storage(path)
    db.log_event("u", "button", "y", account_id="77")
    assert db.account_stats("77")["totals"]["clicks"] == 1
    assert db.account_stats("77", include_unassigned=True)["totals"]["clicks"] == 2
    Storage(path)  # migrating twice is fine


def test_bot_counts_per_account(monkeypatch, tmp_path):
    c, m = setup_accounts(monkeypatch, tmp_path)
    reel = m.rules_store.get().reels[0]
    body = {"object": "instagram", "entry": [{"id": "77", "changes": [{
        "field": "comments", "value": {
            "id": "c1", "text": reel.keywords[0] if reel.keywords else "hi",
            "from": {"id": "555", "username": "fan"}, "media": {"id": reel.id or "m1"}}}]}]}
    routed = m.router.route(body)
    assert list(routed.bodies) == ["77"]
    m.router.bot_for("77").handle_webhook(routed.bodies["77"])

    r = c.get("/admin/accounts/77/stats", **T)
    assert r.status_code == 200
    d = r.json()
    assert d["account"] == "77" and d["range_days"] == 30 and len(d["days"]) == 30
    assert d["totals"]["matches"] == 1 and d["totals"]["dms"] >= 1
    assert d["days"][-1]["matches"] == 1
    # the other accounts saw nothing
    assert c.get("/admin/accounts/88/stats", **T).json()["totals"]["matches"] == 0
    assert c.get("/admin/accounts/42/stats", **T).json()["totals"]["matches"] == 0


def test_stats_endpoint_auth_and_errors(monkeypatch, tmp_path):
    c, _ = setup_accounts(monkeypatch, tmp_path)
    assert c.get("/admin/accounts/77/stats").status_code == 401
    assert c.get("/admin/accounts/nope/stats", **T).status_code == 404
    assert c.get("/admin/accounts/77/stats", params={"token": "admin", "days": 0}
                 ).status_code == 422
    r = c.get("/admin/accounts/42/stats", params={"token": "admin", "days": 7})
    assert r.status_code == 200 and len(r.json()["days"]) == 7


def test_dashboard_has_stats_section(monkeypatch, tmp_path):
    from pathlib import Path
    html = Path("app/admin.html").read_text(encoding="utf-8")
    assert 'id="statsAccount"' in html and 'id="accChart"' in html
    assert "/admin/accounts/${encodeURIComponent(id)}/stats" in html
