"""Refresh the long-lived Instagram token right now.

The bot already does this on its own in the background, so you normally don't need this
script. It is handy to check the token, or to force a refresh:

    python scripts/refresh_token.py           # refresh and save to the database
    python scripts/refresh_token.py --status  # only show expiry, don't refresh
    python scripts/refresh_token.py --account 1784...  # another stored account

The new token is saved in the bot's database (DB_PATH); the running bot picks it up
immediately, no restart needed. The token itself is never printed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.storage import Storage  # noqa: E402
from app.tokens import TokenManager  # noqa: E402

tm = TokenManager(settings, Storage(settings.db_path))
tm.seed()
if "--account" in sys.argv:
    i = sys.argv.index("--account")
    if i + 1 >= len(sys.argv):
        sys.exit("Usage: --account <instagram account id>")
    tm = tm.for_account(sys.argv[i + 1])
if not tm.current():
    sys.exit("No token: set IG_ACCESS_TOKEN in .env first (or add the account with "
             "scripts/accounts.py).")
ok = True if "--status" in sys.argv else tm.refresh()
print(json.dumps(tm.status(), indent=2))
sys.exit(0 if ok else 1)
