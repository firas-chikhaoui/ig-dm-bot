"""Tests for routing webhook events to the right Instagram account."""
import hashlib
import hmac
import json
from dataclasses import replace

import httpx

from app.config import Settings
from app.router import AccountRouter
from app.rules import RulesStore
from app.storage import Storage
from app.tokens import TokenManager
from tests.test_bot import ROOT, comment


def make_router(env_token="env-tok", env_id="42", handler=None):
    s = replace(Settings(), ig_access_token=env_token, ig_user_id=env_id, dry_run=False,
                app_secret="")
    db = Storage(":memory:")
    calls = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler or default_handler))
    tokens = TokenManager(s, db, client=client)
    tokens.seed()
    router = AccountRouter(s, db, RulesStore(str(ROOT / "rules.yaml")), tokens, client=client)
    return router, db, calls


def entry(account, cid, user="111"):
    body = comment(cid, "guide", user=user)
    body["entry"][0]["id"] = account
    return body["entry"][0]


def test_splits_body_per_account_and_skips_paused_and_unknown():
    router, db, _ = make_router()
    db.save_account("77", "client-tok", expires_at=None, username="client")
    db.save_account("88", "paused-tok", expires_at=None)
    db.set_account_active("88", False)

    body = {"object": "instagram",
            "entry": [entry("42", "a"), entry("77", "b"), entry("77", "c"),
                      entry("88", "d"), entry("99", "e"), {"changes": []}]}
    r = router.route(body)
    assert set(r.bodies) == {"42", "77"}
    assert len(r.bodies["77"]["entry"]) == 2 and r.bodies["77"]["object"] == "instagram"
    # an entry without id goes to the .env account
    assert len(r.bodies["42"]["entry"]) == 2
    assert r.paused == ["88"] and r.unknown == ["99"]


def test_each_account_uses_its_own_token_and_id():
    router, db, calls = make_router()
    db.save_account("77", "client-tok", expires_at=None)

    a, b = router.bot_for("42"), router.bot_for("77")
    assert router.bot_for("77") is b and router.default is a
    assert (a.own_id, b.own_id) == ("42", "77")

    # (Instagram-scoped user ids differ per account, so two different people here)
    for account, cid, user in (("42", "c1", "111"), ("77", "c2", "222")):
        body = {"object": "instagram", "entry": [entry(account, cid, user)]}
        for acc, part in router.route(body).bodies.items():
            router.bot_for(acc).handle_webhook(part)

    sends = [(c.url.path, c.headers["Authorization"]) for c in calls
             if c.url.path.endswith("/messages")]
    assert any(p.endswith("/42/messages") and h == "Bearer env-tok" for p, h in sends)
    assert any(p.endswith("/77/messages") and h == "Bearer client-tok" for p, h in sends)
    assert not any(p.endswith("/77/messages") and h == "Bearer env-tok" for p, h in sends)


def test_single_account_handles_unknown_entry_id():
    # IG_USER_ID not set: the only account still gets every event.
    router, _, _ = make_router(env_id="")
    r = router.route({"object": "instagram", "entry": [entry("1784", "a")]})
    assert list(r.bodies) == ["default"] and not r.unknown
    assert router.bot_for("default").api.account_id == ""  # posts to /me/messages


def test_paused_single_account_is_not_bypassed():
    router, db, _ = make_router()
    db.set_account_active("42", False)
    r = router.route({"object": "instagram", "entry": [entry("1784", "a"), entry("42", "b")]})
    assert not r.bodies and r.paused == ["42"]


def test_no_accounts_at_all_still_reaches_default_bot():
    # e.g. DRY_RUN without any token: keep the playground/simulator working
    router, _, _ = make_router(env_token="", env_id="")
    r = router.route({"object": "instagram", "entry": [entry("5", "a"), {"changes": []}]})
    assert list(r.bodies) == ["default"] and len(r.bodies["default"]["entry"]) == 1
    assert r.unknown == ["5"]


def test_webhook_endpoint_routes_by_entry_id(monkeypatch, tmp_path):
    from tests.test_server import make_client
    monkeypatch.setenv("IG_ACCESS_TOKEN", "secret-ig-token")
    monkeypatch.setenv("IG_USER_ID", "42")
    c = make_client(monkeypatch, tmp_path)
    import app.main as m
    m.db.save_account("77", "other-token", expires_at=None)

    seen = []
    for acc in ("42", "77"):
        bot = m.router.bot_for(acc)
        monkeypatch.setattr(bot, "handle_webhook", lambda b, acc=acc: seen.append((acc, b)))

    def post(body):
        raw = json.dumps(body).encode()
        sig = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        return c.post("/webhook", content=raw, headers={"X-Hub-Signature-256": f"sha256={sig}"})

    r = post({"object": "instagram", "entry": [entry("77", "x"), entry("42", "y"),
                                                entry("13", "z")]})
    assert r.status_code == 200 and r.json()["unknown"] == ["13"]
    assert sorted(a for a, _ in seen) == ["42", "77"]
    assert all(len(b["entry"]) == 1 and b["entry"][0]["id"] == a for a, b in seen)

    seen.clear()
    m.db.set_account_active("77", False)
    r = post({"object": "instagram", "entry": [entry("77", "x2")]})
    assert r.json()["paused"] == ["77"] and seen == []
