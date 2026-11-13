"""Settings loaded from environment variables (or a .env file)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Instagram API with Instagram Login
    ig_access_token: str = os.getenv("IG_ACCESS_TOKEN", "")
    ig_user_id: str = os.getenv("IG_USER_ID", "")  # your professional account's IG id
    app_secret: str = os.getenv("APP_SECRET", "")  # used to verify webhook signatures
    verify_token: str = os.getenv("VERIFY_TOKEN", "change-me")  # you choose it in the Meta dashboard
    graph_version: str = os.getenv("GRAPH_VERSION", "v23.0")
    graph_base: str = os.getenv("GRAPH_BASE", "https://graph.instagram.com")

    # Long-lived tokens expire after 60 days; the bot refreshes them on its own.
    token_auto_refresh: bool = os.getenv("TOKEN_AUTO_REFRESH", "true").lower() in ("1", "true", "yes")
    token_refresh_days_before: int = int(os.getenv("TOKEN_REFRESH_DAYS_BEFORE", "10"))
    token_check_hours: float = float(os.getenv("TOKEN_CHECK_HOURS", "6"))

    # Public pages (/privacy, /data-deletion) required by Meta
    public_url: str = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. https://bot.example.com
    privacy_operator: str = os.getenv("PRIVACY_OPERATOR", "")  # your name or business name
    privacy_contact: str = os.getenv("PRIVACY_CONTACT", "")  # email people can write to
    ig_username: str = os.getenv("IG_USERNAME", "")  # shown on the privacy page

    # Optional Telegram alerts on errors (disabled while either value is empty)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_alert_cooldown: float = float(os.getenv("TELEGRAM_ALERT_COOLDOWN", "300"))

    # App
    rules_path: str = os.getenv("RULES_PATH", "rules.yaml")
    db_path: str = os.getenv("DB_PATH", "data/bot.db")
    admin_token: str = os.getenv("ADMIN_TOKEN", "")  # protects /admin endpoints
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
    # In DRY_RUN, pretend the user follows you (true) or not (false) to test the follow gate
    dry_run_follows: bool = os.getenv("DRY_RUN_FOLLOWS", "true").lower() in ("1", "true", "yes")


settings = Settings()
