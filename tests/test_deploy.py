"""Sanity checks for the production deployment files (docker-compose.yml + Caddyfile)."""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_bot_port_is_not_public():
    ports = _compose()["services"]["bot"]["ports"]
    assert ports, "bot must still be reachable locally"
    for p in ports:
        assert str(p).startswith("127.0.0.1:"), f"bot port {p} would bypass HTTPS"


def test_bot_keeps_data_and_rules_volumes():
    vols = _compose()["services"]["bot"]["volumes"]
    targets = {v.split(":")[1] for v in vols}
    assert {"/app/data", "/app/rules.yaml"} <= targets


def test_caddy_service_is_opt_in_and_persists_certificates():
    c = _compose()
    caddy = c["services"]["caddy"]
    assert caddy["profiles"] == ["https"]  # plain `docker compose up` stays local-only
    assert {"80:80", "443:443"} <= set(caddy["ports"])
    assert any(v.startswith("./Caddyfile:/etc/caddy/Caddyfile") for v in caddy["volumes"])
    assert any(v.startswith("caddy_data:/data") for v in caddy["volumes"])
    assert "caddy_data" in c["volumes"]
    assert "DOMAIN" in caddy["environment"]


def test_caddyfile_proxies_to_bot_service():
    text = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert text.count("{") == text.count("}"), "unbalanced braces in Caddyfile"
    m = re.search(r"reverse_proxy\s+(\S+)", text)
    assert m and m.group(1) == "bot:8000"
    assert "{$DOMAIN" in text


def test_dockerfile_trusts_proxy_headers():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--proxy-headers" in text and "--forwarded-allow-ips" in text
    assert "8000" in text


def test_env_example_documents_domain():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^DOMAIN=", text, re.M)
    assert re.search(r"^PUBLIC_URL=https://", text, re.M)
