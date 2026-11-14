from app.storage import Storage
def test_daily_activity_fills_gaps(tmp_path):
    db = Storage(str(tmp_path / "x.db"))
    now = 20 * 86400 + 3600
    db._exec("INSERT INTO events(user_id, kind, detail, created_at) VALUES ('a','button','f/s',?)", (now - 2 * 86400,))
    db._exec("INSERT INTO events(user_id, kind, detail, created_at) VALUES ('a','button','f/s',?)", (now - 30 * 86400,))
    days = db.daily_activity(5, now=now)
    assert [d["counts"].get("button", 0) for d in days] == [0, 0, 1, 0, 0]
    assert days[-1]["date"] == "1970-01-21"
