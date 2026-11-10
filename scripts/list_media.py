"""Print your latest posts/reels with their media ids (to paste into rules.yaml).

Usage:  python scripts/list_media.py          (reads IG_ACCESS_TOKEN from .env)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

url = f"{settings.graph_base}/{settings.graph_version}/me/media"
params = {"fields": "id,media_product_type,caption,permalink,timestamp", "limit": 25,
          "access_token": settings.ig_access_token}
r = httpx.get(url, params=params, timeout=15)
r.raise_for_status()
for m in r.json().get("data", []):
    caption = (m.get("caption") or "").replace("\n", " ")[:50]
    print(f"{m['id']}  {m.get('media_product_type', ''):6}  {m.get('timestamp', '')[:10]}  "
          f"{caption}  {m.get('permalink', '')}")
