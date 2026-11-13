"""Optional Telegram alerts for errors.

When TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, every log record of level ERROR
(failed Instagram API calls, crashes while handling an event, a token that is about to
expire, a broken rules.yaml, ...) is also sent to that Telegram chat. With either value
empty, nothing is sent and nothing changes.

Alerts are sent from a background thread so logging never blocks the webhook, and the
same error is sent at most once per TELEGRAM_ALERT_COOLDOWN seconds (the next alert says
how many were skipped), so a burst of identical failures doesn't flood the chat.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import traceback
from typing import Callable

import httpx

from .config import Settings

log = logging.getLogger("alerts")

TELEGRAM_API = "https://api.telegram.org"
MAX_LEN = 3500  # Telegram allows 4096 characters per message
_SECRET_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+"),
    re.compile(r"(Bearer )[A-Za-z0-9._\-]+"),
    re.compile(r"(api\.telegram\.org/bot)[^/\s]+"),
]


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove access tokens from a message before it leaves the server."""
    for p in _SECRET_PATTERNS:
        text = p.sub(r"\1***", text)
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, "***")
    return text


def _dedup_key(record: logging.LogRecord, message: str) -> str:
    # Ids and numbers differ between otherwise identical errors; group those together.
    return f"{record.name}:{re.sub(r'[0-9]+', '#', message)[:200]}"


class TelegramAlertHandler(logging.Handler):
    """logging.Handler that forwards ERROR records to a Telegram chat."""

    def __init__(self, bot_token: str, chat_id: str, label: str = "ig-dm-bot",
                 cooldown: float = 300, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, background: bool = True,
                 secrets: tuple[str, ...] = ()):
        super().__init__(level=logging.ERROR)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.label = label
        self.cooldown = cooldown
        self.client = client or httpx.Client(timeout=10)
        self.clock = clock
        self.secrets = tuple(secrets) + (bot_token,)
        self._last: dict[str, float] = {}
        self._skipped: dict[str, int] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] | None = None
        if background:
            self._queue = queue.Queue(maxsize=100)
            threading.Thread(target=self._worker, name="telegram-alerts", daemon=True).start()

    # --- logging.Handler -------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        if record.name == log.name:  # never alert about failing to alert
            return
        try:
            message = record.getMessage()
            key = _dedup_key(record, message)
            now = self.clock()
            with self._lock:
                if now - self._last.get(key, float("-inf")) < self.cooldown:
                    self._skipped[key] = self._skipped.get(key, 0) + 1
                    return
                self._last[key] = now
                skipped = self._skipped.pop(key, 0)
            self._enqueue(self.format_alert(record, message, skipped))
        except Exception:
            self.handleError(record)

    def format_alert(self, record: logging.LogRecord, message: str, skipped: int = 0) -> str:
        lines = [f"⚠️ {self.label}: {record.levelname} in {record.name}", message]
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            tb = traceback.extract_tb(record.exc_info[2])
            where = f" (at {tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno})" if tb else ""
            lines.append(f"{type(exc).__name__}: {exc}{where}")
        if skipped:
            lines.append(f"(+{skipped} similar in the last {int(self.cooldown // 60) or 1} min)")
        text = redact("\n".join(lines), self.secrets)
        return text if len(text) <= MAX_LEN else text[:MAX_LEN] + "…"

    # --- sending ---------------------------------------------------------
    def _enqueue(self, text: str) -> None:
        if self._queue is None:
            self.send(text)
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            pass  # Telegram is unreachable or very slow; dropping is better than blocking

    def _worker(self) -> None:
        assert self._queue is not None
        while True:
            text = self._queue.get()
            if text is None:
                return
            self.send(text)
            time.sleep(1)  # Telegram allows about one message per second per chat

    def send(self, text: str) -> bool:
        """Send one message now. Returns True on success; never raises."""
        try:
            r = self.client.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage",
                                 json={"chat_id": self.chat_id, "text": text,
                                       "disable_web_page_preview": True})
        except httpx.HTTPError as e:  # the message would contain the URL with the bot token
            log.warning("Telegram alert not sent: network error %s", type(e).__name__)
            return False
        if r.status_code >= 400:
            try:
                desc = r.json().get("description", "")
            except ValueError:
                desc = r.text[:200]
            log.warning("Telegram alert not sent: HTTP %s %s", r.status_code, desc)
            return False
        return True

    def close(self) -> None:
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        super().close()


def setup_alerts(settings: Settings, logger: logging.Logger | None = None,
                 **kwargs) -> TelegramAlertHandler | None:
    """Attach the Telegram handler to the root logger if it is configured."""
    logger = logger or logging.getLogger()
    for h in [h for h in logger.handlers if isinstance(h, TelegramAlertHandler)]:
        logger.removeHandler(h)  # e.g. after a reload; never send everything twice
        h.close()
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return None
    label = "ig-dm-bot" + (f" @{settings.ig_username}" if settings.ig_username else "")
    handler = TelegramAlertHandler(settings.telegram_bot_token, settings.telegram_chat_id,
                                   label=label, cooldown=settings.telegram_alert_cooldown,
                                   secrets=(settings.ig_access_token, settings.app_secret),
                                   **kwargs)
    logger.addHandler(handler)
    log.info("Telegram error alerts enabled")
    return handler
