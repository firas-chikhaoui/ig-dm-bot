"""Tests for rules stored per account in the database."""
import io
from dataclasses import replace

import pytest

from app.accounts_cli import run
from app.config import Settings
from app.rules import AccountRulesStore, RulesError, RulesStore, parse_rules_yaml
from app.storage import Storage
from tests.test_bot import ROOT, comment
from tests.test_router import entry, make_router
from tests.test_server import make_client

SHOP_RULES = """
# rules of the shop account
reels:
  - id: "*"
    keywords: [price]
    flow: prices
    comment_replies: ["Sent you a DM!"]
flows:
  prices:
    steps:
      hello:
        message: "Our prices: example.com/prices"
"""


def store():
    db = Storage(":memory:")
    return AccountRulesStore(db, RulesStore(str(ROOT / "rules.yaml"))), db


def test_falls_back_to_rules_file_until_rules_are_stored():
    s, _ = store()
    file_rules = s.file.get()
    assert s.get("77") is file_rules and s.source("77") == "file"
    assert s.export_yaml("77") == (ROOT / "rules.yaml").read_text(encoding="utf-8")

    s.import_yaml("77", SHOP_RULES)
    assert s.source("77") == "database"
    assert list(s.get("77").flows) == ["prices"]
    assert s.get("77") is s.get("77")  # parsed once, cached
    assert s.export_yaml("77") == SHOP_RULES  # comments kept
    assert s.get("42") is file_rules  # other accounts unaffected

    assert s.reset("77") and not s.reset("77")
    assert s.get("77") is file_rules


def test_invalid_rules_are_rejected_and_nothing_changes():
    s, db = store()
    s.import_yaml("77", SHOP_RULES)
    for bad in ("reels: [", "reels:\n  - id: '*'\n    flow: nope\n", "- just a list"):
        with pytest.raises(RulesError):
            s.import_yaml("77", bad)
    assert db.get_account_rules("77")["yaml"] == SHOP_RULES
    assert list(s.get("77").flows) == ["prices"]


def test_saving_again_updates_live_rules():
    s, _ = store()
    s.import_yaml("77", SHOP_RULES)
    assert s.get("77").reels[0].keywords == ["price"]
    s.import_yaml("77", SHOP_RULES.replace("[price]", "[cost]"))
    assert s.get("77").reels[0].keywords == ["cost"]


def test_broken_stored_row_keeps_bot_running():
    s, db = store()
    db.save_account_rules("77", "reels: [")  # e.g. edited by hand in the database
    assert s.get("77") is s.file.get()


def test_parse_rules_yaml_empty_is_valid():
    r = parse_rules_yaml("")
    assert r.reels == [] and r.flows == {}


def test_delete_account_removes_its_rules():
    s, db = store()
    db.save_account("77", "tok", expires_at=None)
    s.import_yaml("77", SHOP_RULES)
    db.delete_account("77")
    assert db.get_account_rules("77") is None


def test_router_gives_each_account_its_own_rules():
    router, db, calls = make_router()
    db.save_account("77", "client-tok", expires_at=None)
    router.rules.import_yaml("77", SHOP_RULES)
    assert router.bot_for("77").rules.source == "database"
    assert router.bot_for("42").rules.source == "file"

    def run_entries(*entries):
        calls.clear()
        for acc, part in router.route({"object": "instagram", "entry": list(entries)}).bodies.items():
            router.bot_for(acc).handle_webhook(part)
        return [c.url.path for c in calls if c.url.path.endswith("/messages")]

    # "guide" is a keyword of rules.yaml only, "price" of the shop rules only
    assert run_entries(entry("77", "c1", "201")) == []
    assert run_entries(entry("42", "c2", "202"))
    shop = comment("c3", "price?", user="203")["entry"][0]
    shop["id"] = "77"
    assert any(p.endswith("/77/messages") for p in run_entries(shop))


def cli(db, *argv):
    s = replace(Settings(), ig_access_token="env-tok", ig_user_id="42",
                rules_path=str(ROOT / "rules.yaml"))
    out = io.StringIO()
    return run(list(argv), s, db, out=out), out.getvalue()


def test_cli_rules_import_export_reset(tmp_path):
    db = Storage(":memory:")
    db.save_account("77", "tok", expires_at=None)
    good, bad = tmp_path / "shop.yaml", tmp_path / "bad.yaml"
    good.write_text(SHOP_RULES, encoding="utf-8")
    bad.write_text("reels: [", encoding="utf-8")

    assert cli(db, "rules-validate", str(good))[0] == 0
    code, out = cli(db, "rules-validate", str(bad))
    assert code == 1 and "Invalid" in out
    assert cli(db, "rules-import", "77", str(bad))[0] == 1
    assert db.get_account_rules("77") is None

    code, out = cli(db, "rules-import", "77", str(good))
    assert code == 0 and "1 flow(s)" in out
    assert cli(db, "rules-export", "77")[1] == SHOP_RULES
    dest = tmp_path / "out.yaml"
    assert cli(db, "rules-export", "77", "-o", str(dest))[0] == 0
    assert dest.read_text(encoding="utf-8") == SHOP_RULES
    # the .env account (42) can have its own rules too; unknown ones cannot
    assert cli(db, "rules-import", "42", str(good))[0] == 0
    assert cli(db, "rules-import", "99", str(good))[0] == 1

    code, out = cli(db, "rules-reset", "77")
    assert code == 0 and db.get_account_rules("77") is None


def test_admin_rules_endpoints(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    t = {"token": "admin"}
    assert c.get("/admin/rules").status_code == 401

    r = c.get("/admin/rules", params=t)
    assert r.status_code == 200 and r.headers["X-Rules-Source"] == "file"
    assert "flows:" in r.text

    r = c.post("/admin/rules/validate", params=t, content="reels: [")
    assert r.json()["valid"] is False and "YAML" in r.json()["error"]
    assert c.post("/admin/rules/validate", params=t, content=SHOP_RULES).json()["valid"]

    assert c.put("/admin/rules", params=t, content="flows: 3").status_code == 400
    r = c.put("/admin/rules", params=t, content=SHOP_RULES)
    assert r.status_code == 200 and r.json()["flows"] == ["prices"]
    r = c.get("/admin/rules", params=t)
    assert r.headers["X-Rules-Source"] == "database" and r.text == SHOP_RULES
    assert c.get("/admin/stats", params=t).json()["flows"] == ["prices"]

    # the playground runs the stored rules of the .env account
    info = c.get("/playground/info").json()
    assert info["reels"][0]["keywords"] == ["price"]
    msgs = c.post("/playground/event", json={"kind": "comment", "text": "price",
                                             "user_id": "5"}).json()["messages"]
    assert any("Our prices" in m["text"] for m in msgs)

    assert c.put("/admin/rules", params={**t, "account": "nope"},
                 content=SHOP_RULES).status_code == 404
    assert c.delete("/admin/rules", params=t).json()["deleted"] is True
    assert c.get("/admin/rules", params=t).headers["X-Rules-Source"] == "file"
