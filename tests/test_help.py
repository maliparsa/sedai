"""Offline verification of help_ui.py: role-aware help, scoped command menu, fallback order."""

import asyncio
import logging
import os
import sys
import tempfile

# Derive paths from __file__ so the suite runs from any checkout
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(TEST_DIR, "..", "telegram-bot")
STUBS = os.path.join(TEST_DIR, "stubs")

ADMIN, USER, OUTSIDER = 111, 222, 999
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


async def send_command(app, name, user_id, raw=None):
    import telegram
    from telegram import Message, Update
    from telegram.ext import CommandHandler, MessageHandler, _Ctx
    telegram.ACTIONS.clear()
    telegram.PARSE_MODES.clear()
    cb = app.command(name)
    if cb is None:  # unknown command -> the bare-COMMAND fallback MessageHandler
        for h in app.handlers:
            if isinstance(h, MessageHandler) and getattr(h.filters, "name", "") == "COMMAND":
                cb = h.callback
                break
        assert cb is not None, "no bare-COMMAND fallback handler registered"
    msg = Message(message_id=1, chat_id=user_id, text=raw or f"/{name}", user_id=user_id)
    await cb(Update(message=msg, user_id=user_id), _Ctx())
    return list(telegram.ACTIONS)


def texts(actions):
    return " ".join(str(i) for a in actions for i in a if isinstance(i, str))


async def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)
    tmp = tempfile.mkdtemp()
    os.environ["SEDAI_SETTINGS_PATH"] = os.path.join(tmp, "settings.json")
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = "good-key-aaaa1111"
    os.environ["ALLOWED_USER_ID"] = f"{ADMIN},{USER}"

    import telegram
    from telegram.ext import Application, CommandHandler, MessageHandler

    import sedai_bot

    captured = {}

    def fake_polling(self, **kwargs):
        captured["app"] = self
        raise SystemExit

    Application.run_polling = fake_polling
    try:
        sedai_bot.main()
    except SystemExit:
        pass
    app = captured["app"]

    # --- registration order: the catch-all must come after every CommandHandler
    cmd_idx = [i for i, h in enumerate(app.handlers) if isinstance(h, CommandHandler)]
    msg_idx = [i for i, h in enumerate(app.handlers) if isinstance(h, MessageHandler)]
    fallback_idx = [i for i, h in enumerate(app.handlers)
                    if isinstance(h, MessageHandler) and "COMMAND" in str(getattr(h.filters, "name", ""))
                    and "~" not in str(getattr(h.filters, "name", ""))]
    check("help registered", app.command("help") is not None)
    check("start registered", app.command("start") is not None)
    check("existing commands survive",
          all(app.command(c) is not None for c in ("reset", "settings", "setkey")))
    check("a command fallback handler exists", len(fallback_idx) == 1, fallback_idx)
    if fallback_idx and cmd_idx:
        check("fallback registered after every CommandHandler",
              fallback_idx[0] > max(cmd_idx), f"fallback={fallback_idx[0]} last_cmd={max(cmd_idx)}")
    check("voice/text handlers still present", len(msg_idx) >= 3, msg_idx)

    # --- /help is role-aware
    admin_help = texts(await send_command(app, "help", ADMIN))
    user_help = texts(await send_command(app, "help", USER))
    out_help = texts(await send_command(app, "help", OUTSIDER))

    for c in ("/settings", "/reset", "/help"):
        check(f"regular user help lists {c}", c in user_help, user_help[:200])
    check("regular user help hides /setkey", "/setkey" not in user_help, user_help[:300])
    check("regular user help hides /adduser", "/adduser" not in user_help, user_help[:300])
    check("admin help shows /setkey", "/setkey" in admin_help, admin_help[:300])
    check("admin help shows /adduser", "/adduser" in admin_help, admin_help[:300])
    check("admin help still lists shared commands", "/settings" in admin_help)
    check("outsider gets no help at all", out_help == "", out_help[:120])
    check("help mentions the voice-note flow",
          any(w in user_help.lower() for w in ("voice", "audio")), user_help[:200])
    check("help sent as plain text (no parse_mode)",
          all(p is None for p in telegram.PARSE_MODES), telegram.PARSE_MODES)

    # --- /start
    admin_start = texts(await send_command(app, "start", ADMIN))
    user_start = texts(await send_command(app, "start", USER))
    check("start works for regular user", "/settings" in user_start, user_start[:200])
    check("start is role-aware too", "/setkey" in admin_start and "/setkey" not in user_start)
    check("start silent for outsider", texts(await send_command(app, "start", OUTSIDER)) == "")

    # --- unknown command
    unknown_user = texts(await send_command(app, "bogus", USER, raw="/bogus"))
    unknown_out = texts(await send_command(app, "bogus", OUTSIDER, raw="/bogus"))
    check("unknown command answered for allowed user",
          "/help" in unknown_user, unknown_user[:200])
    check("unknown command silent for outsider", unknown_out == "", unknown_out[:120])

    # --- post_init pushes scoped command lists
    check("post_init attached to the app", app.post_init is not None)
    telegram.ACTIONS.clear()
    await app.post_init(app)
    calls = [a for a in telegram.ACTIONS if a[0] == "set_my_commands"]
    check("set_my_commands called at least twice (base + admin)", len(calls) >= 2, len(calls))

    base = [c for c in calls if getattr(c[2], "kind", None) in ("default", "all_private_chats")]
    admin_scoped = [c for c in calls if getattr(c[2], "kind", None) == "chat"]
    check("a base scope list was pushed", len(base) == 1, [getattr(c[2], "kind", None) for c in calls])
    check("an admin chat scope list was pushed", len(admin_scoped) == 1)
    if base:
        names = base[0][1]
        check("base scope lists settings/reset/help",
              {"settings", "reset", "help"}.issubset(set(names)), names)
        check("base scope hides setkey", "setkey" not in names, names)
        check("base scope hides adduser", "adduser" not in names, names)
    if admin_scoped:
        names = admin_scoped[0][1]
        check("admin scope includes setkey and adduser",
              {"setkey", "adduser"}.issubset(set(names)), names)
        check("admin scope targets the admin's chat",
              getattr(admin_scoped[0][2], "chat_id", None) == ADMIN,
              getattr(admin_scoped[0][2], "chat_id", None))

    # --- post_init must never break startup, and must not log the raw error
    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    cap = Capture()
    logging.getLogger("sedai-bot").addHandler(cap)
    telegram.Bot.fail_set_my_commands = True
    try:
        await app.post_init(app)
        check("post_init survives a Telegram failure", True)
    except Exception as e:
        check("post_init survives a Telegram failure", False, repr(e))
    telegram.Bot.fail_set_my_commands = False
    logged = " ".join(cap.lines)
    check("post_init failure did not log the bot token",
          "tg-token-secret" not in logged, logged[:200])

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== help_ui.py verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
