"""Tests for the dashboard's emails page: emails per account, lists, search, CSV, removal."""
import csv
import io
import sqlite3
from pathlib import Path

from app.storage import Storage
from tests.test_dashboard import T, setup_accounts


def fill(db):
    db.save_email("u1", "ann", "ann@x.co", "waitlist", "guide", account_id="77")
    db.save_email("u2", "bob", "bob@x.co", "waitlist", "guide", account_id="77")
    db.save_email("u2", "bob", "bob@x.co", "vip", "vip", account_id="77")
    db.save_email("u3", "cat", "cat@x.co", "waitlist", "guide", account_id="88")
    db.save_email("u4", None, "old@x.co", "waitlist", "guide")  # before per-account emails


def test_same_person_on_two_accounts_keeps_both():
    db = Storage(":memory:")
    db.save_email("u1", "ann", "a@x.co", "waitlist", "f", account_id="77")
    db.save_email("u1", "ann", "a@other.co", "waitlist", "f", account_id="88")
    db.save_email("u1", "ann", "new@x.co", "waitlist", "f", account_id="77")  # update
    assert [r["email"] for r in db.list_emails(account_id="77")] == ["new@x.co"]
    assert [r["email"] for r in db.list_emails(account_id="88")] == ["a@other.co"]
    assert db.count_emails() == 2


def test_unassigned_email_is_taken_over():
    db = Storage(":memory:")
    db.save_email("u1", None, "a@x.co", "waitlist", "f")
    db.save_email("u1", "ann", "b@x.co", "waitlist", "f", account_id="42")
    rows = db.list_emails()
    assert len(rows) == 1 and rows[0]["account_id"] == "42" and rows[0]["username"] == "ann"


def test_filters_and_lists():
    db = Storage(":memory:")
    fill(db)
    assert db.count_emails(account_id="77") == 3
    assert db.count_emails(account_id="77", list_name="vip") == 1
    assert db.count_emails(account_id="42", include_unassigned=True) == 1
    assert db.count_emails(account_id="77", search="BOB") == 2
    assert db.count_emails(search="%") == 0  # LIKE wildcards are literal
    assert db.email_lists(account_id="77")[0]["list"] == "vip"
    assert {x["list"]: x["count"] for x in db.email_lists("77")} == {"vip": 1, "waitlist": 2}
    page = db.list_emails(account_id="77", limit=2, offset=0, newest_first=True)
    assert len(page) == 2


def test_old_primary_key_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE emails (user_id TEXT NOT NULL, username TEXT, email TEXT NOT NULL,
            list TEXT NOT NULL, flow TEXT, created_at REAL NOT NULL,
            PRIMARY KEY (user_id, list));
        INSERT INTO emails VALUES ('u1', 'ann', 'a@x.co', 'waitlist', 'f', 1);
    """)
    conn.commit()
    conn.close()
    db = Storage(path)
    db.save_email("u1", "ann", "a@shop.co", "waitlist", "f", account_id="77")  # takes it over
    db.save_email("u1", "ann", "a@cafe.co", "waitlist", "f", account_id="88")  # new row
    assert db.count_emails() == 2
    Storage(path)  # migrating twice is fine
    assert Storage(path).count_emails() == 2


def test_account_emails_endpoint(monkeypatch, tmp_path):
    c, m = setup_accounts(monkeypatch, tmp_path)
    fill(m.db)
    assert c.get("/admin/accounts/77/emails").status_code == 401
    d = c.get("/admin/accounts/77/emails", **T).json()
    assert d["account"] == "77" and d["total"] == 3
    assert {x["list"] for x in d["lists"]} == {"waitlist", "vip"}
    assert {e["email"] for e in d["emails"]} == {"ann@x.co", "bob@x.co"}
    d = c.get("/admin/accounts/77/emails", params={"token": "admin", "list": "vip"}).json()
    assert d["total"] == 1 and d["emails"][0]["list"] == "vip"
    d = c.get("/admin/accounts/77/emails", params={"token": "admin", "q": "ann"}).json()
    assert [e["username"] for e in d["emails"]] == ["ann"]
    d = c.get("/admin/accounts/77/emails", params={"token": "admin", "limit": 1,
                                                   "offset": 1}).json()
    assert d["total"] == 3 and len(d["emails"]) == 1
    # the .env account also sees the addresses stored before emails were per account
    d = c.get("/admin/accounts/42/emails", **T).json()
    assert [e["email"] for e in d["emails"]] == ["old@x.co"]
    assert c.get("/admin/accounts/999/emails", **T).status_code == 404


def test_csv_per_account(monkeypatch, tmp_path):
    c, m = setup_accounts(monkeypatch, tmp_path)
    fill(m.db)
    m.db.save_email("u9", "evil", "=cmd@x.co", "waitlist", "guide", account_id="88")
    r = c.get("/admin/emails.csv", params={"token": "admin", "account": "77",
                                           "list": "waitlist"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="emails-77-waitlist.csv"' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert sorted(x["email"] for x in rows) == ["ann@x.co", "bob@x.co"]
    assert {x["account_id"] for x in rows} == {"77"}

    every = list(csv.DictReader(io.StringIO(c.get("/admin/emails.csv", **T).text)))
    assert len(every) == 6
    assert "'=cmd@x.co" in {x["email"] for x in every}  # no formula in spreadsheets
    legacy = list(csv.DictReader(io.StringIO(
        c.get("/admin/emails.csv", params={"token": "admin", "account": "42"}).text)))
    assert [(x["email"], x["account_id"]) for x in legacy] == [("old@x.co", "42")]


def test_remove_one_email(monkeypatch, tmp_path):
    c, m = setup_accounts(monkeypatch, tmp_path)
    fill(m.db)
    q = {"token": "admin", "user_id": "u2", "list": "vip"}
    assert c.delete("/admin/accounts/88/emails", params=q).status_code == 404  # other account
    r = c.delete("/admin/accounts/77/emails", params=q)
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    assert m.db.count_emails(account_id="77") == 2  # bob stays on the waitlist
    assert c.delete("/admin/accounts/77/emails", params=q).status_code == 404


def test_dashboard_has_emails_section():
    html = Path("app/admin.html").read_text(encoding="utf-8")
    for needle in ('id="emailsTable"', 'id="emailsCsv"', "/emails?", "emails.csv?"):
        assert needle in html
