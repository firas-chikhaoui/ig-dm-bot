"""Command line management of connected Instagram accounts (see scripts/accounts.py)."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Callable, TextIO

from .config import Settings
from .storage import Storage
from .tokens import TokenManager


def run(argv: list[str], settings: Settings, db: Storage, out: TextIO = sys.stdout,
        read_token: Callable[[], str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="accounts.py",
                                description="Manage the Instagram accounts the bot serves.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show accounts and token health (tokens are never shown)")
    add = sub.add_parser("add", help="add an account or replace its token")
    add.add_argument("ig_user_id", help="Instagram professional account id")
    add.add_argument("--username", help="Instagram username, for display")
    for name, text in (("pause", "stop answering and refreshing this account"),
                       ("resume", "turn a paused account back on"),
                       ("remove", "delete the account and its token from the database")):
        sub.add_parser(name, help=text).add_argument("ig_user_id")
    args = p.parse_args(argv)

    tokens = TokenManager(settings, db)
    tokens.seed()

    if args.cmd == "list":
        rows = [tokens.for_account(r["ig_user_id"]).status() for r in db.list_accounts()]
        print(json.dumps(rows, indent=2), file=out)
        return 0
    if args.cmd == "add":
        # Read the token from a prompt (not argv) so it stays out of the shell history.
        token = (read_token or (lambda: getpass.getpass("Access token: ")))().strip()
        if not token:
            print("No token given, nothing changed.", file=out)
            return 1
        db.save_account(args.ig_user_id, token, expires_at=None, username=args.username)
        print(f"Saved account {args.ig_user_id}. The token is refreshed automatically; its "
              "expiry shows up after the first refresh.", file=out)
        return 0
    if args.cmd in ("pause", "resume"):
        if not db.set_account_active(args.ig_user_id, args.cmd == "resume"):
            print(f"Unknown account {args.ig_user_id}", file=out)
            return 1
        print(f"Account {args.ig_user_id} {'resumed' if args.cmd == 'resume' else 'paused'}.",
              file=out)
        return 0
    # remove
    if not db.delete_account(args.ig_user_id):
        print(f"Unknown account {args.ig_user_id}", file=out)
        return 1
    note = (" It comes back on the next start while IG_ACCESS_TOKEN is set in .env."
            if tokens.account_id == args.ig_user_id and settings.ig_access_token else "")
    print(f"Removed account {args.ig_user_id}.{note}", file=out)
    return 0
