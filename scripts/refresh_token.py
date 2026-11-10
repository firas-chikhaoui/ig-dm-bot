"""Refresh the long-lived Instagram token (valid 60 days). Run it monthly, e.g. via cron:
   0 4 1 * *  cd /path/to/ig-dm-bot && python scripts/refresh_token.py
Prints the new token; put it in .env and restart the bot.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

r = httpx.get(f"{settings.graph_base}/refresh_access_token",
              params={"grant_type": "ig_refresh_token",
                      "access_token": settings.ig_access_token}, timeout=15)
r.raise_for_status()
data = r.json()
print(f"new token (expires in {data.get('expires_in', 0) // 86400} days):")
print(data["access_token"])
