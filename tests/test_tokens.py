"""Tests for the automatic Instagram token refresh."""
import logging
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.instagram import InstagramAPI
from app.storage import Storage
from app.tokens import DAY, TokenManager

NOW = 1_800_000_000.0


def make(handler, token="env-token", **kw):
    s = replace(Settings(), ig_access_token=token, dry_run=False, token_auto_refresh=True,
                token_refresh_days_before=10, **kw)
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)
    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    db = Storage(":memory:")
    clock = {"t": NOW}
    tm = TokenManager(s, db, client=client, clock=lambda: clock["t"])
    tm.seed()
    return tm, db, calls, clock


def ok(request):
    n = int(request.url.params["access_token"].split("-")[-1]) if \
        request.url.params["access_token"].startswith("new-") else 0
    return httpx.Response(200, json={"access_token": f"new-{n + 1}", "token_type": "bearer",
                                     "expires_in": 60 * DAY})


def test_seed_and_first_refresh_learns_expiry():
    tm, db, calls, _ = make(ok)
    assert tm.current() == "env-token" and tm.needs_refresh()  # expiry unknown
    tm.check()
    assert len(calls) == 1
    assert calls[0].url.path == "/refresh_access_token"
    assert calls[0].url.params["grant_type"] == "ig_refresh_token"
    assert tm.current() == "new-1"
    assert round(tm.days_left()) == 60
    assert not tm.needs_refresh()
    tm.check()  # nothing to do now
    assert len(calls) == 1


def test_refreshes_shortly_before_expiry():
    tm, _, calls, clock = make(ok)
    tm.refresh()
    clock["t"] += 49 * DAY   # 11 days left: still fine
    tm.check()
    assert len(calls) == 1
    clock["t"] += 2 * DAY    # 9 days left: refresh
    tm.check()
    assert len(calls) == 2 and tm.current() == "new-2"
    assert round(tm.days_left()) == 60


def test_refreshed_token_survives_restart_and_new_env_token_wins():
    tm, db, _, _ = make(ok)
    tm.refresh()
    again = TokenManager(tm.s, db)
    again.seed()  # same .env token: keep the refreshed one
    assert again.current() == "new-1"
    newer = TokenManager(replace(tm.s, ig_access_token="pasted-token"), db)
    newer.seed()  # user pasted a new token into .env
    assert newer.current() == "pasted-token" and newer.days_left() is None


def test_failure_is_logged_without_token_and_stored(caplog):
    tm, db, _, clock = make(lambda r: httpx.Response(400, json={"error": {"message": "nope"}}))
    with caplog.at_level(logging.WARNING, logger="tokens"):
        assert tm.refresh() is False
    assert "retry later" in caplog.text and "env-token" not in caplog.text
    assert "HTTP 400" in tm.status()["last_error"]
    assert tm.current() == "env-token"

    # close to expiry -> error level
    db.save_token("instagram", "env-token", expires_at=NOW + 3 * DAY)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokens"):
        tm.check()
    assert any(r.levelno == logging.ERROR and "expires in 3.0 days" in r.getMessage()
               for r in caplog.records)
    clock["t"] += 4 * DAY
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokens"):
        tm.check()
    assert "EXPIRED" in caplog.text


def test_network_error_does_not_leak_token(caplog):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")
    tm, _, _, _ = make(boom)
    with caplog.at_level(logging.WARNING, logger="tokens"):
        assert tm.refresh() is False
    assert "ConnectError" in caplog.text and "env-token" not in caplog.text


@pytest.mark.parametrize("flag", ["dry_run", "token_auto_refresh"])
def test_check_disabled(flag):
    tm, _, calls, _ = make(ok)
    tm.s = replace(tm.s, **{flag: flag == "dry_run"})
    tm.check()
    assert calls == []


def test_no_token_configured():
    tm, _, calls, _ = make(ok, token="")
    assert tm.current() == "" and not tm.needs_refresh()
    tm.check()
    assert calls == [] and tm.status()["configured"] is False


def test_api_uses_current_token():
    tm, _, _, _ = make(ok)
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(r.headers["Authorization"]) or httpx.Response(200, json={})))
    api = InstagramAPI(tm.s, client=client, token_provider=tm.current)
    api.reply_to_comment("c1", "hi")
    tm.refresh()
    api.reply_to_comment("c1", "hi")
    assert seen == ["Bearer env-token", "Bearer new-1"]
