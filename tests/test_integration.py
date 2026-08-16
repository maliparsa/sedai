"""End-to-end: sedai_bot.py wiring, live settings application, no import-time capture."""

import asyncio
import os
import sys
import tempfile

# Derive paths from __file__ so the suite runs from any checkout
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(TEST_DIR, "..", "telegram-bot")
STUBS = os.path.join(TEST_DIR, "stubs")

ADMIN, USER = 111, 222
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


async def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)
    tmp = tempfile.mkdtemp()
    os.environ["SEDAI_SETTINGS_PATH"] = os.path.join(tmp, "settings.json")
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = "good-key-aaaa1111"
    os.environ["ALLOWED_USER_ID"] = f"{ADMIN},{USER}"

    import google.genai as genai_stub
    import telegram
    from telegram import CallbackQuery, Message, Update
    from telegram.ext import Application, _Ctx

    import sedai_bot
    import settings

    # --- no import-time capture of anything settable
    for gone in ("AUDIO_MODELS", "TEXT_MODELS", "GEMINI_MODEL", "gemini_client",
                 "ALLOWED_USER_IDS", "GEMINI_API_KEY"):
        check(f"{gone} no longer a module constant", not hasattr(sedai_bot, gone))
    check("TELEGRAM_TOKEN still env-only", hasattr(sedai_bot, "TELEGRAM_TOKEN"))

    # --- main() wires everything without reaching run_polling
    captured = {}

    def fake_polling(self, **kwargs):
        captured["app"] = self
        raise SystemExit("stop")

    Application.run_polling = fake_polling
    try:
        sedai_bot.main()
    except SystemExit:
        pass

    app = captured.get("app")
    check("main() built and polled an app", app is not None)
    if app:
        cmds = [h.command for h in app.handlers if hasattr(h, "command")]
        check("reset still registered", "reset" in cmds, cmds)
        check("settings registered via settings_ui", "settings" in cmds, cmds)
        check("setkey registered via settings_ui", "setkey" in cmds, cmds)
        pats = app.callback_patterns()
        check("legacy transcript callbacks still routed",
              any("summarize" in (p or "") for p in pats), pats)
        check("settings callbacks routed separately",
              any("set:" in (p or "") for p in pats), pats)
        # a legacy callback must reach the transcript handler, not the settings one
        import settings_ui
        cb = app.callback_for("summarize:5")
        check("summarize routes to the bot handler, not settings_ui",
              cb is not None and cb.__module__ != "settings_ui",
              getattr(cb, "__module__", None))

    # --- key-change hook really clears chat sessions
    import settings_ui
    sedai_bot.CHATS[999] = ["dummy", "m"]
    settings_ui._on_key_change()
    check("key-change hook clears CHATS", len(sedai_bot.CHATS) == 0, dict(sedai_bot.CHATS))

    # --- a per-user model preference actually reaches the API call, with no restart
    settings.set_user_model(USER, "text", "gemini-3.5-flash")
    genai_stub.CALLS.clear()
    telegram.ACTIONS.clear()
    msg = Message(message_id=1, chat_id=USER, text="hello", user_id=USER)
    await sedai_bot.handle_text(Update(message=msg, user_id=USER), _Ctx())
    chat_creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("user's chosen model used for chat", any(c[1] == "gemini-3.5-flash" for c in chat_creates),
          chat_creates)

    # a different user is unaffected
    genai_stub.CALLS.clear()
    msg2 = Message(message_id=2, chat_id=ADMIN, text="hello", user_id=ADMIN)
    await sedai_bot.handle_text(Update(message=msg2, user_id=ADMIN), _Ctx())
    chat_creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("admin unaffected by the other user's preference",
          all(c[1] != "gemini-3.5-flash" for c in chat_creates), chat_creates)

    # --- changing the preference mid-session switches model on the next message
    settings.set_user_model(USER, "text", "gemini-flash-latest")
    genai_stub.CALLS.clear()
    msg3 = Message(message_id=3, chat_id=USER, text="again", user_id=USER)
    await sedai_bot.handle_text(Update(message=msg3, user_id=USER), _Ctx())
    chat_creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("model change applies live, no restart",
          any(c[1] == "gemini-flash-latest" for c in chat_creates), chat_creates)

    # --- removed user is locked out immediately
    settings.remove_user(USER)
    telegram.ACTIONS.clear()
    msg4 = Message(message_id=4, chat_id=USER, text="still here?", user_id=USER)
    await sedai_bot.handle_text(Update(message=msg4, user_id=USER), _Ctx())
    check("removed user gets no response at all", len(telegram.ACTIONS) == 0, telegram.ACTIONS)

    # --- cross-chat transcript privacy must not have regressed
    sedai_bot._remember_transcript(500, 7, "user A secret")
    check("transcript keyed by (chat_id, message_id)",
          sedai_bot.TRANSCRIPTS.get((501, 7)) is None and
          sedai_bot.TRANSCRIPTS.get((500, 7)) == "user A secret",
          list(sedai_bot.TRANSCRIPTS))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== integration verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
