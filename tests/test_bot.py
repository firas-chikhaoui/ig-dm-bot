"""End-to-end tests of the bot logic with a fake Instagram API (no network)."""
from pathlib import Path

import pytest
import yaml

from app.bot import Bot, make_payload
from app.instagram import InstagramAPI
from app.rules import RulesError, RulesStore, parse_rules
from app.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
REEL = "17900000000000000"
ME = "999"


class FakeAPI:
    def __init__(self, following=False):
        self.following = following
        self.comment_replies, self.sent = [], []

    def reply_to_comment(self, cid, text):
        self.comment_replies.append((cid, text))

    def send(self, recipient, message):
        self.sent.append((recipient, message))

    build_message = staticmethod(InstagramAPI.build_message)

    def is_following(self, igsid):
        return self.following


@pytest.fixture
def setup():
    api = FakeAPI()
    bot = Bot(api, RulesStore(str(ROOT / "rules.yaml")), Storage(":memory:"), own_id=ME)
    return bot, api


def comment(cid, text, user="111", username="sara", media=REEL):
    return {"object": "instagram", "entry": [{"changes": [{"field": "comments", "value": {
        "id": cid, "text": text, "from": {"id": user, "username": username},
        "media": {"id": media}}}]}]}


def click(payload, user="111", mid="x"):
    return {"object": "instagram", "entry": [{"messaging": [{
        "sender": {"id": user}, "recipient": {"id": ME},
        "postback": {"mid": mid, "payload": payload, "title": "btn"}}]}]}


def text_msg(text, user="111", mid="t"):
    return {"object": "instagram", "entry": [{"messaging": [{
        "sender": {"id": user}, "recipient": {"id": ME},
        "message": {"mid": mid, "text": text}}]}]}


def test_example_rules_are_valid():
    rules = parse_rules(yaml.safe_load((ROOT / "rules.yaml").read_text(encoding="utf-8")))
    assert rules.reels and rules.flows


def test_keyword_comment_starts_flow_with_private_reply(setup):
    bot, api = setup
    bot.handle_webhook(comment("c1", "Send me the GUIDE please"))
    assert api.comment_replies[0][0] == "c1"
    recipient, msg = api.sent[0]
    assert recipient == {"comment_id": "c1"}  # private reply
    buttons = msg["attachment"]["payload"]["buttons"]
    assert buttons[0]["payload"] == make_payload("free_guide", "check_follow")
    assert "sara" in msg["attachment"]["payload"]["text"]


def test_non_matching_comment_and_duplicates_ignored(setup):
    bot, api = setup
    bot.handle_webhook(comment("c1", "nice video"))
    assert not api.sent
    bot.handle_webhook(comment("c2", "guide"))
    bot.handle_webhook(comment("c2", "guide"))  # same webhook delivered twice
    bot.handle_webhook(comment("c3", "guide"))  # same user, same reel again
    assert len(api.sent) == 1


def test_own_comments_ignored(setup):
    bot, api = setup
    bot.handle_webhook(comment("c9", "guide", user=ME))
    assert not api.sent and not api.comment_replies


def test_follow_gate_then_link_then_email(setup):
    bot, api = setup
    bot.handle_webhook(comment("c1", "guide"))

    # Not following -> asked to follow
    bot.handle_webhook(click(make_payload("free_guide", "check_follow"), mid="m1"))
    assert "Follow my account" in api.sent[-1][1]["attachment"]["payload"]["text"]

    # Follows, taps "I followed" -> gets the link
    api.following = True
    bot.handle_webhook(click(make_payload("free_guide", "check_follow"), mid="m2"))
    payload = api.sent[-1][1]["attachment"]["payload"]
    assert payload["buttons"][0] == {"type": "web_url", "url": "https://example.com/guide",
                                     "title": "Open the guide"}

    # Join waitlist -> invalid email -> valid email
    bot.handle_webhook(click(make_payload("free_guide", "ask_email"), mid="m3"))
    assert "email" in api.sent[-1][1]["text"]
    bot.handle_webhook(text_msg("not an email", mid="m4"))
    assert "doesn't look like" in api.sent[-1][1]["text"]
    bot.handle_webhook(text_msg("Sara@Example.com", mid="m5"))
    assert "sara@example.com" in api.sent[-1][1]["text"].lower()
    rows = bot.db.list_emails("waitlist")
    assert [(r["email"], r["username"]) for r in rows] == [("sara@example.com", "sara")]


def test_wildcard_reel_and_dm_trigger(setup):
    bot, api = setup
    bot.handle_webhook(comment("c1", "waitlist", media="any-other-post"))
    assert api.sent[-1][0] == {"comment_id": "c1"}
    bot.handle_webhook(text_msg("guide", user="222", mid="d1"))
    assert api.sent[-1][0] == {"id": "222"}


def test_invalid_rules_rejected():
    with pytest.raises(RulesError):
        parse_rules({"reels": [{"id": "1", "keywords": ["x"], "flow": "missing"}], "flows": {}})
    with pytest.raises(RulesError):
        parse_rules({"flows": {"f": {"steps": {"a": {"message": "hi", "next": "nope"}}}}})


def test_word_matching():
    rules = parse_rules({"reels": [{"id": "*", "keywords": ["link"], "flow": "f"}],
                         "flows": {"f": {"steps": {"a": {"message": "hi"}}}}})
    assert rules.find_reel_rule("1", "LINK pls")
    assert not rules.find_reel_rule("1", "linkedin")
