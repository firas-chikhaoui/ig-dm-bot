"""Tests for Telegram error alerts and the logging of failed Instagram API calls."""
import importlib
import json
import logging
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alerts import TelegramAlertHandler, redact, setup_alerts
from app.config import Settings
from app.instagram import InstagramAPI, describe_error


def telegram(status=200, body=None):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def make_handler(**kw):
    client, sent = telegram()
    clock = {"t": 1000.0}
    h = TelegramAlertHandler("123:SECRETBOTTOKEN", "-42", label="ig-dm-bot @me",
                             client=client, clock=lambda: clock["t"], background=False, **kw)
    logger = logging.getLogger("test.alerts")
    logger.handlers = [h]
    logger.propagate = False
    return h, logger, sent, clock


def texts(sent):
    return [json.loads(r.content)["text"] for r in sent]


def test_disabled_without_token_or_chat():
    root = logging.getLogger("test.root")
    assert setup_alerts(replace(Settings(), telegram_bot_token="", telegram_chat_id="1"),
                        root) is None
    assert setup_alerts(replace(Settings(), telegram_bot_token="t", telegram_chat_id=""),
                        root) is None
    assert root.handlers == []


def test_setup_attaches_once():
    root = logging.getLogger("test.setup")
    s = replace(Settings(), telegram_bot_token="123:abcdefgh", telegram_chat_id="1",
                ig_username="shop")
    client, _ = telegram()
    h1 = setup_alerts(s, root, client=client, background=False)
    h2 = setup_alerts(s, root, client=client, background=False)
    assert root.handlers == [h2] and h1 is not h2
    assert h2.label == "ig-dm-bot @shop"
    root.handlers = []


def test_sends_errors_not_warnings():
    _, logger, sent, _ = make_handler()
    logger.warning("just a warning")
    logger.info("info")
    assert sent == []
    logger.error("flow %s: too many chained steps", "guide")
    assert len(sent) == 1
    req = sent[0]
    assert req.url.path == "/bot123:SECRETBOTTOKEN/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "-42"
    assert body["text"].startswith("⚠️ ig-dm-bot @me: ERROR in test.alerts")
    assert "flow guide: too many chained steps" in body["text"]


def test_exception_summary():
    _, logger, sent, _ = make_handler()
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("error while handling %s", "handle_comment")
    text = texts(sent)[0]
    assert "error while handling handle_comment" in text
    assert "ValueError: boom (at test_alerts.py:" in text
    assert "Traceback" not in text


def test_same_error_is_throttled_and_counted():
    _, logger, sent, clock = make_handler(cooldown=300)
    logger.error("comment %s failed", "111")
    logger.error("comment %s failed", "222")  # same error, other id: grouped
    logger.error("comment %s failed", "333")
    logger.error("something else broke")
    assert len(sent) == 2
    clock["t"] += 301
    logger.error("comment %s failed", "444")
    assert len(sent) == 3
    assert "(+2 similar in the last 5 min)" in texts(sent)[2]


def test_secrets_are_redacted():
    h, logger, sent, _ = make_handler(secrets=("IGTOKEN-very-secret",))
    logger.error("GET https://graph.instagram.com/x?access_token=abc123&y=1 failed")
    logger.error("header Bearer IGAAabc.def-ghi leaked")
    logger.error("token IGTOKEN-very-secret and bot 123:SECRETBOTTOKEN")
    t = "\n".join(texts(sent))
    assert "abc123" not in t and "IGAAabc" not in t
    assert "very-secret" not in t and "SECRETBOTTOKEN" not in t
    assert "access_token=***" in t
    assert redact("https://api.telegram.org/bot1:xyz/sendMessage") == \
        "https://api.telegram.org/bot***/sendMessage"


def test_long_messages_are_truncated():
    _, logger, sent, _ = make_handler()
    logger.error("x" * 10000)
    assert len(texts(sent)[0]) <= 3501


def test_telegram_failure_never_raises(caplog):
    def boom(request):
        raise httpx.ConnectError("down")
    h = TelegramAlertHandler("123:abcdefgh", "1", background=False,
                             client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert h.send("hi") is False
    client, _ = telegram(400, {"ok": False, "description": "Bad Request: chat not found"})
    h = TelegramAlertHandler("123:abcdefgh", "1", background=False, client=client)
    with caplog.at_level(logging.WARNING, logger="alerts"):
        assert h.send("hi") is False
    assert "chat not found" in caplog.text
    assert "abcdefgh" not in caplog.text


def test_background_worker_delivers():
    client, sent = telegram()
    h = TelegramAlertHandler("123:abcdefgh", "1", client=client)
    logger = logging.getLogger("test.bg")
    logger.handlers = [h]
    logger.propagate = False
    logger.error("async error")
    for _ in range(50):
        if sent:
            break
        time.sleep(0.02)
    assert texts(sent) and "async error" in texts(sent)[0]
    h.close()


# --- failed Instagram API calls are logged clearly ---------------------------
def test_describe_meta_error():
    r = httpx.Response(400, json={"error": {
        "message": "Error validating access token", "type": "OAuthException", "code": 190,
        "error_subcode": 463, "fbtrace_id": "AbC123"}})
    d = describe_error(r)
    assert d.startswith("HTTP 400, code 190/463, OAuthException: Error validating access token")
    assert "access token invalid or expired" in d and "[fbtrace_id AbC123]" in d
    assert describe_error(httpx.Response(502, text="Bad gateway")) == "HTTP 502: Bad gateway"


def api_with(handler):
    s = replace(Settings(), dry_run=False, ig_access_token="tok-secret", ig_user_id="999")
    return InstagramAPI(s, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_failed_api_call_is_one_error_line(caplog):
    api = api_with(lambda r: httpx.Response(400, json={"error": {
        "message": "The user is not available", "code": 10, "error_subcode": 2534022}}))
    with caplog.at_level(logging.ERROR, logger="instagram"), pytest.raises(httpx.HTTPStatusError):
        api.send({"id": "1"}, {"text": "hi"})
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert msg.startswith("Instagram API POST 999/messages failed: HTTP 400, code 10/2534022")
    assert "tok-secret" not in msg


def test_network_error_is_logged(caplog):
    def down(request):
        raise httpx.ConnectTimeout("timed out")
    api = api_with(down)
    with caplog.at_level(logging.ERROR, logger="instagram"), pytest.raises(httpx.HTTPError):
        api.reply_to_comment("c1", "hi")
    assert "Instagram API POST c1/replies failed: network error ConnectTimeout" in caplog.text


def test_failed_follow_check_logs_once(caplog):
    api = api_with(lambda r: httpx.Response(500, json={"error": {"message": "oops", "code": 1}}))
    with caplog.at_level(logging.WARNING, logger="instagram"):
        assert api.is_following("42") is False
    levels = [r.levelname for r in caplog.records]
    assert levels.count("ERROR") == 1 and "WARNING" in levels


# --- admin test endpoint ---------------------------------------------------
def make_client(monkeypatch, tmp_path, **env):
    for k, v in {"ADMIN_TOKEN": "admin", "DB_PATH": str(tmp_path / "bot.db"),
                 "DRY_RUN": "true", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
                 **env}.items():
        monkeypatch.setenv(k, v)
    import app.config
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app), app.main


def test_alert_test_endpoint(monkeypatch, tmp_path):
    c, main = make_client(monkeypatch, tmp_path)
    assert c.post("/admin/alerts/test").status_code == 401
    assert c.post("/admin/alerts/test", params={"token": "admin"}).status_code == 409

    c, main = make_client(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN="123:abcdefgh",
                          TELEGRAM_CHAT_ID="7")
    try:
        assert main.alerts is not None
        client, sent = telegram()
        main.alerts.client = client
        r = c.post("/admin/alerts/test", params={"token": "admin"})
        assert r.status_code == 200 and r.json() == {"sent": True}
        assert "test alert" in texts(sent)[0]
    finally:
        setup_alerts(replace(Settings(), telegram_bot_token=""))  # detach from root logger
