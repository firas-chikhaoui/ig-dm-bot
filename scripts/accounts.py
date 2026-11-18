"""Manage the Instagram accounts stored in the bot's database.

    python scripts/accounts.py list                         # accounts + token health
    python scripts/accounts.py add 1784... --username me    # asks for the token
    python scripts/accounts.py pause 1784...                # stop answering this account
    python scripts/accounts.py resume 1784...
    python scripts/accounts.py remove 1784...
    python scripts/accounts.py rules-export 1784... -o shop.yaml   # rules as YAML
    python scripts/accounts.py rules-import 1784... shop.yaml      # validate + store
    python scripts/accounts.py rules-reset 1784...                 # back to rules.yaml
    python scripts/accounts.py rules-validate shop.yaml            # check only

The account from IG_USER_ID / IG_ACCESS_TOKEN in .env is added automatically on start.
Tokens are never printed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.accounts_cli import run  # noqa: E402
from app.config import settings  # noqa: E402
from app.storage import Storage  # noqa: E402

sys.exit(run(sys.argv[1:], settings, Storage(settings.db_path)))
