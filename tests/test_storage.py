from app.storage import Storage
def test_daily_activity_fills_gaps(tmp_path):
    db = Storage(str(tmp_path / "x.db"))
    now = 20 * 86400 + 3600
    db._exec("INSERT INTO events(user_id, kind, detail, created_at) VALUES ('a','button','f/s',?)", (now - 2 * 86400,))
    db._exec("INSERT INTO events(user_id, kind, detail, created_at) VALUES ('a','button','f/s',?)", (now - 30 * 86400,))
    days = db.daily_activity(5, now=now)
    assert [d["counts"].get("button", 0) for d in days] == [0, 0, 1, 0, 0]
    assert days[-1]["date"] == "1970-01-21"


def test_accounts_crud():
    from app.storage import Storage
    db = Storage(":memory:")
    assert db.list_accounts() == [] and db.get_account("1") is None
    db.save_account("1", "t1", expires_at=100.0, username="one")
    db.save_account("2", "t2", expires_at=None)
    assert [r["ig_user_id"] for r in db.list_accounts()] == ["1", "2"]
    db.set_account_error("1", "HTTP 400")
    db.save_account("1", "t1b", expires_at=200.0)  # new token keeps username, clears error
    row = db.get_account("1")
    assert (row["access_token"], row["expires_at"], row["username"], row["last_error"]) == \
        ("t1b", 200.0, "one", None)
    assert db.set_account_active("2", False) is True
    assert db.set_account_active("nope", False) is False
    assert [r["ig_user_id"] for r in db.list_accounts(active_only=True)] == ["1"]
    db.save_account("2", "t2b", expires_at=None)  # a new token does not resume it
    assert db.get_account("2")["active"] == 0
    assert db.delete_account("2") and not db.delete_account("2")
    assert db.pop_legacy_token() is None
