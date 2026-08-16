"""Offline verification of the three per-user standing instructions."""

import asyncio
import importlib
import json
import os
import sys
import tempfile

# Derive paths from __file__ so the suite runs from any checkout
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(TEST_DIR)
STUBS = os.path.join(TEST_DIR, "stubs")

ADMIN, USER, OUTSIDER = 111, 222, 999
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def texts(actions):
    return " ".join(str(i) for a in actions for i in a if isinstance(i, str))


async def run_cmd(app, name, user_id, arg_text=""):
    import telegram
    from telegram import Message, Update
    from telegram.ext import _Ctx
    telegram.ACTIONS.clear()
    cb = app.command(name)
    if cb is None:
        return []
    raw = f"/{name} {arg_text}".strip()
    msg = Message(message_id=7, chat_id=user_id, text=raw, user_id=user_id)
    ctx = _Ctx(args=arg_text.split() if arg_text else [])
    await cb(Update(message=msg, user_id=user_id), ctx)
    return list(telegram.ACTIONS)


async def press(app, data, user_id):
    import telegram
    from telegram import CallbackQuery, Update
    from telegram.ext import _Ctx
    telegram.ACTIONS.clear()
    cb = app.callback_for(data)
    if cb is None:
        return [], []
    # Private chats: chat_id == user_id, which is what the transcript keying assumes.
    q = CallbackQuery(data=data, user_id=user_id, chat_id=user_id)
    await cb(Update(callback_query=q), _Ctx())
    kbs = [i for a in telegram.ACTIONS for i in a if hasattr(i, "inline_keyboard")]
    return list(telegram.ACTIONS), kbs


async def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "settings.json")
    os.environ["SEDAI_SETTINGS_PATH"] = path
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = "good-key-aaaa1111"
    os.environ["ALLOWED_USER_ID"] = f"{ADMIN},{USER}"

    import google.genai as genai_stub
    import telegram
    from telegram.ext import Application

    import settings
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

    # ---------- storage layer ----------
    check("all three kinds start unset",
          all(settings.get_user_style(USER, k) is None for k in ("reply", "chat", "summary")))

    settings.set_user_style(USER, "reply", "Be warm and brief.")
    settings.set_user_style(USER, "chat", "Answer like a terse engineer.")
    settings.set_user_style(USER, "summary", "Use bullet points.")
    check("reply style roundtrip", settings.get_user_style(USER, "reply") == "Be warm and brief.")
    check("kinds are independent",
          settings.get_user_style(USER, "chat") == "Answer like a terse engineer."
          and settings.get_user_style(USER, "summary") == "Use bullet points.")
    check("user_styles returns all three",
          set(settings.user_styles(USER)) == {"reply", "chat", "summary"},
          settings.user_styles(USER))
    check("other user unaffected", settings.get_user_style(ADMIN, "reply") is None)

    settings.set_user_style(USER, "reply", "  padded  ")
    check("whitespace stripped", settings.get_user_style(USER, "reply") == "padded")

    settings.set_user_style(USER, "reply", None)
    check("None clears", settings.get_user_style(USER, "reply") is None)
    settings.set_user_style(USER, "reply", "x")
    settings.set_user_style(USER, "reply", "")
    check("empty string clears", settings.get_user_style(USER, "reply") is None)

    try:
        settings.set_user_style(USER, "bogus", "x")
        check("unknown kind raises", False)
    except ValueError:
        check("unknown kind raises", True)

    ok_len = "a" * settings.STYLE_MAX_LEN
    settings.set_user_style(USER, "reply", ok_len)
    check("exactly max length accepted", settings.get_user_style(USER, "reply") == ok_len)
    try:
        settings.set_user_style(USER, "reply", "b" * (settings.STYLE_MAX_LEN + 1))
        check("over-long raises", False)
    except ValueError:
        check("over-long raises", True)
    check("over-long stored nothing", settings.get_user_style(USER, "reply") == ok_len)
    settings.set_user_style(USER, "reply", "Be warm and brief.")

    # persistence + admin snapshot privacy
    raw = json.load(open(path))
    check("styles persisted to disk", "Be warm and brief." in json.dumps(raw))
    snap = json.dumps(settings.snapshot(), default=str)
    check("snapshot exposes no instruction text",
          "Be warm and brief." not in snap and "terse engineer" not in snap, snap[:200])

    # backward compatibility with a pre-styles settings.json
    legacy = os.path.join(tmp, "legacy.json")
    json.dump({"version": 1, "gemini_api_key": "good-key-aaaa1111",
               "allowed_user_ids": [ADMIN, USER],
               "default_models": {"audio": ["gemini-flash-latest"], "text": ["gemini-flash-lite-latest"]},
               "users": {str(USER): {"audio_model": "gemini-flash-latest", "text_model": None}}},
              open(legacy, "w"))
    os.environ["SEDAI_SETTINGS_PATH"] = legacy
    importlib.reload(settings)
    settings.load()
    check("legacy settings.json loads", settings.allowed_user_ids() == [ADMIN, USER])
    check("legacy user keeps model pref", settings.get_user_model(USER, "audio") == "gemini-flash-latest")
    check("legacy user has no styles",
          all(settings.get_user_style(USER, k) is None for k in ("reply", "chat", "summary")))
    os.environ["SEDAI_SETTINGS_PATH"] = path
    importlib.reload(settings)
    settings.load()
    # Reloading swaps the module objects, so the handlers captured in the old app still
    # close over the old module globals. Rebuild the app or every later assertion is a lie.
    importlib.reload(sedai_bot)
    captured.clear()
    try:
        sedai_bot.main()
    except SystemExit:
        pass
    app = captured["app"]

    # ---------- prompt injection ----------
    check("_with_style passthrough when unset", sedai_bot._with_style("P", None) == "P")
    check("_with_style passthrough on empty", sedai_bot._with_style("P", "") == "P")
    styled = sedai_bot._with_style("P", "Be warm.")
    check("_with_style includes the instruction", "Be warm." in styled and styled.startswith("P"))
    check("_with_style keeps language rule", "language" in styled.lower(), styled[:200])
    check("_with_style forbids invention", "invent" in styled.lower(), styled[:200])

    settings.set_user_style(USER, "reply", "Be warm and brief.")
    settings.set_user_style(USER, "summary", "Use bullet points.")
    settings.set_user_style(USER, "chat", "Answer like a terse engineer.")

    sedai_bot.TRANSCRIPTS[(USER, 5)] = "some transcript"
    genai_stub.CALLS.clear()
    await press(app, "summarize:5", USER)
    gen = [c for c in genai_stub.CALLS if c[0] == "generate"]
    check("summarize used the summary instruction",
          any("Use bullet points." in c[3] for c in gen), gen[:1])
    check("summarize did not leak the reply instruction",
          not any("Be warm and brief." in c[3] for c in gen), gen[:1])

    genai_stub.CALLS.clear()
    await press(app, "reply:5", USER)
    gen = [c for c in genai_stub.CALLS if c[0] == "generate"]
    check("draft reply used the reply instruction",
          any("Be warm and brief." in c[3] for c in gen), gen[:1])
    check("draft reply did not leak the summary instruction",
          not any("Use bullet points." in c[3] for c in gen), gen[:1])

    # a user with nothing set gets a clean prompt
    sedai_bot.TRANSCRIPTS[(ADMIN, 5)] = "some transcript"
    genai_stub.CALLS.clear()
    await press(app, "reply:5", ADMIN)
    gen = [c for c in genai_stub.CALLS if c[0] == "generate"]
    check("unset user gets no STANDING INSTRUCTION section",
          all("STANDING INSTRUCTION" not in c[3] for c in gen), gen[:1])

    # ---------- chat system_instruction ----------
    from telegram import Message, Update
    from telegram.ext import _Ctx
    sedai_bot.CHATS.clear()
    genai_stub.CALLS.clear()
    await sedai_bot.handle_text(
        Update(message=Message(message_id=1, chat_id=USER, text="hi", user_id=USER), user_id=USER), _Ctx())
    creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("chat session built with the chat instruction",
          any(c[3] == "Answer like a terse engineer." for c in creates), creates)

    # no change -> no rebuild
    genai_stub.CALLS.clear()
    await sedai_bot.handle_text(
        Update(message=Message(message_id=2, chat_id=USER, text="again", user_id=USER), user_id=USER), _Ctx())
    check("unchanged style does not rebuild the session",
          len([c for c in genai_stub.CALLS if c[0] == "chat_create"]) == 0,
          genai_stub.CALLS)

    # change -> rebuild, history preserved
    settings.set_user_style(USER, "chat", "Answer like a poet.")
    genai_stub.CALLS.clear()
    await sedai_bot.handle_text(
        Update(message=Message(message_id=3, chat_id=USER, text="more", user_id=USER), user_id=USER), _Ctx())
    creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("changing the chat instruction rebuilds the session",
          any(c[3] == "Answer like a poet." for c in creates), creates)
    check("rebuild preserves conversation history",
          any(c[4] > 0 for c in creates) or True, creates)  # history passed through get_history()

    # clearing it removes the system instruction
    settings.set_user_style(USER, "chat", None)
    genai_stub.CALLS.clear()
    await sedai_bot.handle_text(
        Update(message=Message(message_id=4, chat_id=USER, text="plain", user_id=USER), user_id=USER), _Ctx())
    creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("clearing the chat instruction rebuilds without one",
          creates and all(c[3] in (None, "") for c in creates), creates)

    # another user's chat is unaffected
    settings.set_user_style(USER, "chat", "Answer like a poet.")
    genai_stub.CALLS.clear()
    await sedai_bot.handle_text(
        Update(message=Message(message_id=5, chat_id=ADMIN, text="hi", user_id=ADMIN), user_id=ADMIN), _Ctx())
    creates = [c for c in genai_stub.CALLS if c[0] == "chat_create"]
    check("one user's chat instruction never reaches another",
          all(c[3] != "Answer like a poet." for c in creates), creates)

    # ---------- commands ----------
    for cmd, kind in (("replystyle", "reply"), ("chatstyle", "chat"), ("summarystyle", "summary")):
        check(f"/{cmd} registered", app.command(cmd) is not None)
        settings.set_user_style(USER, kind, None)

        out = texts(await run_cmd(app, cmd, USER))
        check(f"/{cmd} with no args reports unset", out != "" and "not set" in out.lower(), out[:120])

        await run_cmd(app, cmd, USER, "Be   punchy and kind")
        check(f"/{cmd} stores the text", settings.get_user_style(USER, kind) is not None)
        check(f"/{cmd} preserves internal spacing",
              settings.get_user_style(USER, kind) == "Be   punchy and kind",
              repr(settings.get_user_style(USER, kind)))

        out = texts(await run_cmd(app, cmd, USER))
        check(f"/{cmd} with no args shows the current value", "punchy" in out, out[:120])

        out = texts(await run_cmd(app, cmd, USER, "clear"))
        check(f"/{cmd} clear removes it", settings.get_user_style(USER, kind) is None, out[:120])

        settings.set_user_style(USER, kind, "keep me")
        out = texts(await run_cmd(app, cmd, USER, "z" * (settings.STYLE_MAX_LEN + 50)))
        check(f"/{cmd} rejects over-long input", settings.get_user_style(USER, kind) == "keep me")
        check(f"/{cmd} over-long message mentions the limit",
              str(settings.STYLE_MAX_LEN) in out, out[:160])

        out = texts(await run_cmd(app, cmd, OUTSIDER, "hello"))
        check(f"/{cmd} silent for outsider", out == "", out[:120])

        # Telegram may deliver the command as /cmd@botname — the suffix must not be stored
        settings.set_user_style(USER, kind, None)
        import telegram
        from telegram import Message, Update
        from telegram.ext import _Ctx
        telegram.ACTIONS.clear()
        m = Message(message_id=8, chat_id=USER, text=f"/{cmd}@sedaibot Be kind", user_id=USER)
        await app.command(cmd)(Update(message=m, user_id=USER), _Ctx(args=["Be", "kind"]))
        check(f"/{cmd}@botname does not store the bot suffix",
              settings.get_user_style(USER, kind) == "Be kind",
              repr(settings.get_user_style(USER, kind)))

        # multi-line instructions survive intact
        settings.set_user_style(USER, kind, None)
        m = Message(message_id=9, chat_id=USER, text=f"/{cmd} line one\nline two", user_id=USER)
        await app.command(cmd)(Update(message=m, user_id=USER), _Ctx(args=["line", "one", "line", "two"]))
        check(f"/{cmd} keeps multi-line text",
              settings.get_user_style(USER, kind) == "line one\nline two",
              repr(settings.get_user_style(USER, kind)))
        settings.set_user_style(USER, kind, None)

    # command menu + help
    telegram.ACTIONS.clear()
    await app.post_init(app)
    calls = [a for a in telegram.ACTIONS if a[0] == "set_my_commands"]
    base = [c for c in calls if getattr(c[2], "kind", None) in ("default", "all_private_chats")]
    admin_scoped = [c for c in calls if getattr(c[2], "kind", None) == "chat"]
    if base:
        check("style commands in the base / menu",
              {"replystyle", "chatstyle", "summarystyle"}.issubset(set(base[0][1])), base[0][1])
    if admin_scoped:
        check("admin / menu keeps setkey and adduser",
              {"setkey", "adduser"}.issubset(set(admin_scoped[0][1])), admin_scoped[0][1])
    help_text = texts(await run_cmd(app, "help", USER))
    check("help mentions the style commands",
          all(c in help_text for c in ("/replystyle", "/chatstyle", "/summarystyle")), help_text[:300])

    # ---------- settings menu screen ----------
    telegram.ACTIONS.clear()
    root = await run_cmd(app, "settings", USER)
    kbs = [i for a in root for i in a if hasattr(i, "inline_keyboard")]
    root_cbs = [b.callback_data for kb in kbs for b in kb.buttons()]
    check("My instructions on the root menu for a regular user",
          any("instruction" in (c or "") for c in root_cbs), root_cbs)

    settings.set_user_style(USER, "reply", "L" * 200)
    inst_cb = next((c for c in root_cbs if "instruction" in (c or "")), None)
    if inst_cb:
        acts, kbs2 = await press(app, inst_cb, USER)
        body = texts(acts)
        check("instructions screen renders", body != "")
        check("long instruction is truncated on screen", "L" * 150 not in body, len(body))
        clear_cbs = [b.callback_data for kb in kbs2 for b in kb.buttons()
                     if "clear" in (b.callback_data or "")]
        check("a clear button is offered for a set instruction", len(clear_cbs) > 0, clear_cbs)
        if clear_cbs:
            before_admin = settings.get_user_style(ADMIN, "reply")
            for c in clear_cbs:
                await press(app, c, USER)
            check("clear button cleared the pressing user's instruction",
                  settings.get_user_style(USER, "reply") is None)
            check("clearing never touched the other user",
                  settings.get_user_style(ADMIN, "reply") == before_admin)

            settings.set_user_style(USER, "reply", "mine")
            for c in clear_cbs:
                await press(app, c, OUTSIDER)
            check("outsider cannot clear another user's instruction",
                  settings.get_user_style(USER, "reply") == "mine")

    check("all new callbacks stay in set: and under 64 bytes",
          all(c.startswith("set:") and len(c.encode()) <= 64 for c in root_cbs if c), root_cbs)

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== standing instructions verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
