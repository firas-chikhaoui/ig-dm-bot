"""Routes webhook events to the Instagram account they belong to.

Meta sends events for every connected account to the same webhook URL. Each `entry`
carries the id of the Instagram professional account it is about (`entry.id`), so the
router splits a webhook body by entry and hands each part to that account's own bot,
which talks to Instagram with that account's token.

Lookup order for an entry id:
1. a stored account with that id (paused accounts are skipped),
2. no id at all (e.g. the playground) -> the `.env` account,
3. an unknown id while the bot serves exactly one account -> that account, so a
   single-account setup keeps working even if IG_USER_ID is not set or differs,
4. otherwise the entry is ignored with a warning.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from .bot import Bot
from .config import Settings
from .instagram import InstagramAPI
from .rules import AccountRulesStore, RulesStore
from .storage import Storage
from .tokens import DEFAULT_ACCOUNT, TokenManager

log = logging.getLogger("router")


@dataclass
class RouteResult:
    """A webhook body split per account."""
    bodies: dict[str, dict[str, Any]] = field(default_factory=dict)  # account id -> body
    paused: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


class AccountRouter:
    def __init__(self, settings: Settings, db: Storage,
                 rules: AccountRulesStore | RulesStore, tokens: TokenManager,
                 client: httpx.Client | None = None):
        self.s = settings
        self.db = db
        # Each account uses its own stored rules, or the rules file when it has none.
        self.rules = rules if isinstance(rules, AccountRulesStore) else \
            AccountRulesStore(db, rules)
        self.tokens = tokens
        self.client = client or httpx.Client(timeout=15)
        self._bots: dict[str, Bot] = {}
        self._lock = threading.Lock()
        self._warned: set[str] = set()

    @property
    def default_id(self) -> str:
        return self.tokens.account_id

    @property
    def default(self) -> Bot:
        """Bot of the .env account (also used by the playground)."""
        return self.bot_for(self.default_id)

    # --- per-account bots ------------------------------------------------
    def bot_for(self, account_id: str) -> Bot:
        """The bot (and API client) of one account, created on first use."""
        with self._lock:
            bot = self._bots.get(account_id)
            if bot is None:
                tm = self.tokens if account_id == self.default_id else \
                    self.tokens.for_account(account_id)
                ig_id = "" if account_id == DEFAULT_ACCOUNT else account_id
                api = InstagramAPI(self.s, self.client, token_provider=tm.current,
                                   account_id=ig_id)
                bot = self._bots[account_id] = Bot(api, self.rules.for_account(account_id), self.db,
                                                   own_id=ig_id, account_id=account_id)
            return bot

    # --- routing ---------------------------------------------------------
    def _accounts(self) -> dict[str, bool]:
        """Every account the bot serves -> active?"""
        accounts = {r["ig_user_id"]: bool(r["active"]) for r in self.db.list_accounts()}
        if self.default_id not in accounts and self.tokens.current():
            accounts[self.default_id] = True  # .env token not stored (yet)
        return accounts

    def resolve(self, entry_id: str | None, accounts: dict[str, bool] | None = None
                ) -> tuple[str | None, str]:
        """Account for one entry id: (account_id, "ok" | "paused" | "unknown")."""
        accounts = self._accounts() if accounts is None else accounts
        if entry_id and entry_id in accounts:
            account = entry_id
        elif not entry_id or entry_id == self.default_id:
            account = self.default_id
        elif len(accounts) == 1:
            account = next(iter(accounts))
            if entry_id not in self._warned:
                self._warned.add(entry_id)
                log.info("event for account %s handled by the only account %s "
                         "(set IG_USER_ID=%s in .env to match)", entry_id, account, entry_id)
        else:
            if entry_id not in self._warned:
                self._warned.add(entry_id)
                log.warning("webhook event for unknown Instagram account %s ignored; "
                            "add it with scripts/accounts.py", entry_id)
            return None, "unknown"
        if not accounts.get(account, account == self.default_id):
            return account, "paused"
        return account, "ok"

    def route(self, body: dict[str, Any]) -> RouteResult:
        """Split a webhook body into one body per account."""
        result = RouteResult()
        accounts = self._accounts()
        for entry in body.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry["id"]) if entry.get("id") is not None else None
            account, status = self.resolve(entry_id, accounts)
            if status == "unknown":
                result.unknown.append(entry_id or "")
            elif status == "paused":
                if account not in result.paused:
                    result.paused.append(account)
            else:
                part = result.bodies.setdefault(
                    account, {"object": body.get("object"), "entry": []})
                part["entry"].append(entry)
        return result
