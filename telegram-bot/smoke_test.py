#!/usr/bin/env python3
"""
Smoke test against the REAL python-telegram-bot library.

The offline suites in tests/ run against stubs that implement command matching
themselves, so they cannot catch bugs in Telegram's actual parser or in this project's
use of the real API surface. This script closes that gap. It needs no network, no bot
token, and no Gemini key.

Run it with the deployed venv:

    venv/bin/python smoke_test.py
"""

import sys

from telegram import Bot, Message, Update
from telegram.ext import CommandHandler, filters

BOT_USERNAME = "sedaibot"

# Every command the bot registers, across sedai_bot / settings_ui / style_ui / help_ui.
REGISTERED_COMMANDS = [
    "reset", "settings", "setkey", "adduser",
    "help", "start",
    "replystyle", "chatstyle", "summarystyle", "transcriptstyle",
]

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


class OfflineBot(Bot):
    """Real Bot, but with `username` answered locally.

    Bot.username raises unless the bot has been initialized against the API; overriding it
    keeps this test offline while leaving the parsing code paths genuinely real.
    """

    @property
    def username(self):
        return BOT_USERNAME


def make_update(text, bot, is_reply=False, entity_len=None):
    """Build a real Update with a real bot_command entity, as Telegram would send it."""
    entities = []
    if text.startswith("/"):
        # Telegram measures entity length in UTF-16 code units, and marks only the command
        # token itself (including any @botname suffix).
        token = text.split(" ", 1)[0].split("\n", 1)[0]
        entities = [{
            "type": "bot_command",
            "offset": 0,
            "length": entity_len if entity_len is not None else len(token),
        }]

    message = {
        "message_id": 11,
        "date": 0,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "tester"},
        "text": text,
    }
    if entities:
        message["entities"] = entities
    if is_reply:
        message["reply_to_message"] = {
            "message_id": 10,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 7, "is_bot": True, "first_name": "bot"},
            "text": "Reply to this message with your input.",
        }
    return Update.de_json({"update_id": 1, "message": message}, bot)


def arg_after_command(text):
    """Mirror style_ui's raw-text slicing, so the test covers what the bot actually does."""
    import re
    m = re.match(r"^\S+\s*(.*)$", text, flags=re.S)
    return m.group(1) if m else ""


async def noop(update, context):
    pass


def main():
    bot = OfflineBot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    # ---- every registered command must actually match its handler ----
    for cmd in REGISTERED_COMMANDS:
        handler = CommandHandler(cmd, noop)
        upd = make_update(f"/{cmd}", bot)
        check(f"/{cmd} matches its handler", bool(handler.check_update(upd)))

    # ---- argument parsing across scripts and shapes ----
    # A Farsi argument was suspected of breaking /replystyle in production; these cases
    # pin the real parser's behaviour so that suspicion can never be guesswork again.
    handler = CommandHandler("replystyle", noop)
    cases = [
        ("ascii", "/replystyle be warm and brief", "be warm and brief"),
        ("farsi", "/replystyle با لحن دوستانه بنویس", "با لحن دوستانه بنویس"),
        ("farsi single word", "/replystyle سلام", "سلام"),
        ("emoji", "/replystyle be warm 🙂", "be warm 🙂"),
        ("emoji then farsi", "/replystyle 🙂 با لحن", "🙂 با لحن"),
        ("multi-line", "/replystyle line one\nline two", "line one\nline two"),
        ("extra spacing kept", "/replystyle be   punchy", "be   punchy"),
        ("clear keyword", "/replystyle clear", "clear"),
    ]
    for name, text, expected_arg in cases:
        upd = make_update(text, bot)
        check(f"/replystyle matches with {name} argument", bool(handler.check_update(upd)), text)
        check(f"/replystyle preserves the {name} argument",
              arg_after_command(upd.message.text) == expected_arg,
              repr(arg_after_command(upd.message.text)))

    # ---- @botname suffix must match, and must not bleed into the value ----
    text = f"/replystyle@{BOT_USERNAME} Be kind"
    upd = make_update(text, bot)
    check("/replystyle@botname matches", bool(handler.check_update(upd)))
    check("@botname suffix is not part of the value",
          arg_after_command(upd.message.text) == "Be kind",
          repr(arg_after_command(upd.message.text)))

    # a command addressed to a DIFFERENT bot must not match
    upd = make_update("/replystyle@someotherbot hello", bot)
    check("command addressed to another bot does not match", not handler.check_update(upd))

    # ---- unknown command: no handler matches, but the fallback filter does ----
    upd = make_update("/notacommand", bot)
    for cmd in REGISTERED_COMMANDS:
        if CommandHandler(cmd, noop).check_update(upd):
            check("unknown command matches no registered handler", False, cmd)
            break
    else:
        check("unknown command matches no registered handler", True)
    check("unknown command is caught by the COMMAND fallback filter",
          bool(filters.COMMAND.check_update(upd)))

    # a typo'd style command must reach the fallback, not a style handler
    upd = make_update("/replystyl oops", bot)
    check("typo'd command does not match the real handler", not handler.check_update(upd))
    check("typo'd command still hits the COMMAND fallback",
          bool(filters.COMMAND.check_update(upd)))

    # ---- the predicate the reply-based input flow depends on ----
    reply_filter = filters.REPLY & filters.TEXT & ~filters.COMMAND
    upd = make_update("my new instruction", bot, is_reply=True)
    check("reply filter matches a plain text reply", bool(reply_filter.check_update(upd)))
    check("a reply message exposes reply_to_message",
          upd.message.reply_to_message is not None)

    upd = make_update("just chatting", bot, is_reply=False)
    check("reply filter does not match a non-reply", not reply_filter.check_update(upd))

    upd = make_update("/settings", bot, is_reply=True)
    check("reply filter excludes commands", not reply_filter.check_update(upd))

    upd = make_update("پاسخ فارسی", bot, is_reply=True)
    check("reply filter matches a non-ASCII reply", bool(reply_filter.check_update(upd)))

    # ---- API-surface guard: Message.bot does not exist in PTB v20+ ----
    # Using msg.bot instead of get_bot()/context.bot was a real crash in this project.
    check("Message has no .bot attribute (use context.bot / get_bot())",
          not hasattr(Message, "bot"))
    check("Message.get_bot() exists", hasattr(Message, "get_bot"))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== smoke test: real python-telegram-bot =====")
    import telegram
    print(f"python-telegram-bot {telegram.__version__}\n")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
