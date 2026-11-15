"""Keep docs/app-review.md in sync with what the bot actually does."""
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "app-review.md"

PERMISSIONS = {
    "instagram_business_basic",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
}


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_doc_covers_exactly_the_permissions_the_bot_needs():
    text = _doc()
    for p in PERMISSIONS:
        assert f"### `{p}`" in text, f"missing permission section for {p}"
    # every instagram_business_* permission named in the table must be one we use
    table_perms = set(re.findall(r"^\| `(instagram_business_\w+)`", text, re.M))
    assert table_perms == PERMISSIONS
    # README lists the same permissions for setup
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for p in PERMISSIONS:
        assert p in readme
    assert "docs/app-review.md" in readme


def test_doc_urls_point_to_real_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    import app.config
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.main)
    routes = {getattr(r, "path", "") for r in app.main.app.routes}
    used = set(re.findall(r"bot\.yourdomain\.com(/[a-z\-/]*)", _doc()))
    used = {u.rstrip("/") for u in used if u.rstrip("/")}
    assert {"/privacy", "/data-deletion", "/webhook", "/health", "/admin"} <= used
    assert used <= routes, f"doc mentions routes that don't exist: {used - routes}"


def test_doc_webhook_fields_and_code_references_exist():
    text = _doc()
    for field in ("comments", "messages", "messaging_postbacks"):
        assert f"**`{field}`**" in text
    from app.instagram import InstagramAPI
    for name in re.findall(r"`InstagramAPI\.(\w+)`", text):
        assert hasattr(InstagramAPI, name), name
    for path in re.findall(r"`((?:app|scripts)/[\w/]+\.py)`", text):
        assert (ROOT / path).exists(), path
