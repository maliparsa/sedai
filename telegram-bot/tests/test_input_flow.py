"""Offline verification of the reply-based input flow.

The critical property under test: a reply that is NOT to one of our prompts must fall
through to normal chat handling completely untouched. Getting that wrong would silently
swallow ordinary messages.
"""

import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(HERE)
STUBS = os.path.join(HERE, "stubs")

ADMIN, USER, OTHER = 111, 222, 333
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def texts(actions):
    return " ".join(str(i) for a in actions for i in a if isinstance(i, str))


async def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)
    tmp = tempfile.mkdtemp()
    os.environ["SEDAI_SETTINGS_PATH"] = os.path.join(tmp, "settings.json")
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = "good-key-aaaa1111"
    os.environ["ALLOWED_USER_ID"] = f"{ADMIN},{USER},{OTHER}"

    import telegram
    from telegram import Message, Update
    from telegram.ext import Application, ApplicationHandlerStop, _Ctx

    import input_flow
    import sedai_bot
    import settings

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

    # the reply consumer must be registered in group -1, ahead of handle_text
    groups = getattr(app, "handler_groups", {})
    check("reply handler registered in group -1", -1 in groups or any(
        g == -1 for g in groups), sorted(groups) if groups else "no group info")

    # Group -1 holds more than one handler now (the image refinement handler shares it), so
    # pick this module's consumer by identity rather than by position.
    group_minus_one = groups.get(-1, [])
    consume = None
    for h in group_minus_one:
        if h.callback is input_flow._consume:
            consume = h.callback
    check("found the group -1 consumer", consume is not None)

    # Ordering within the group is what keeps a reply to a ForceReply prompt from being
    # claimed by another group -1 handler first. Registration order is execution order.
    callbacks = [h.callback for h in group_minus_one]
    check("the prompt consumer runs before any other group -1 handler",
          callbacks and callbacks[0] is input_flow._consume,
          [getattr(c, "__name__", c) for c in callbacks])

    async def send_reply(user_id, reply_to_message_id, text, chat_id=None):
        telegram.ACTIONS.clear()
        chat_id = chat_id if chat_id is not None else user_id
        m = Message(message_id=900, chat_id=chat_id, text=text, user_id=user_id)
        m.reply_to_message = Message(message_id=reply_to_message_id, chat_id=chat_id)
        stopped = False
        try:
            await consume(Update(message=m, user_id=user_id), _Ctx())
        except ApplicationHandlerStop:
            stopped = True
        return list(telegram.ACTIONS), stopped

    # ---------- issuing a prompt ----------
    got = {}

    @input_flow.on("test:action")
    async def _consumer(update, context, text, meta):
        got["text"] = text
        got["meta"] = meta
        await update.effective_message.reply_text(f"got:{text}")

    telegram.ACTIONS.clear()
    bot = telegram.Bot()
    await input_flow.request(bot, chat_id=USER, user_id=USER,
                             action="test:action", prompt="Give me a value", meta={"k": 1})
    sent = [a for a in telegram.ACTIONS if a[0] == "send_message"]
    check("prompt was sent", len(sent) == 1, telegram.ACTIONS)
    check("prompt carries the reply instruction",
          input_flow.PROMPT_SUFFIX.strip() in texts(telegram.ACTIONS), texts(telegram.ACTIONS)[:200])
    markup = [i for a in telegram.ACTIONS for i in a if type(i).__name__ == "ForceReply"]
    check("prompt uses ForceReply", len(markup) == 1, [type(i).__name__ for a in telegram.ACTIONS for i in a])

    prompt_id = 9999  # stub Bot.send_message returns message_id 9999
    check("pending entry recorded under (chat_id, message_id)",
          (USER, prompt_id) in input_flow.PENDING, list(input_flow.PENDING))

    # ---------- the happy path ----------
    acts, stopped = await send_reply(USER, prompt_id, "my value")
    check("consumer received the reply text", got.get("text") == "my value", got)
    check("consumer received meta", got.get("meta") == {"k": 1}, got)
    check("consumed reply stops propagation", stopped)
    check("pending entry cleared after use", (USER, prompt_id) not in input_flow.PENDING)

    # replying again to the same prompt is no longer pending -> falls through
    acts, stopped = await send_reply(USER, prompt_id, "again")
    check("second reply to same prompt falls through", not stopped)
    check("second reply produced no output", acts == [], acts)

    # ---------- fall-through: the property that matters most ----------
    acts, stopped = await send_reply(USER, 4242, "just chatting")
    check("reply to an unrelated message falls through", not stopped)
    check("unrelated reply produced no output at all", acts == [], acts)

    # a reply to a transcript must still be untouched
    sedai_bot.TRANSCRIPTS[(USER, 77)] = "a transcript"
    acts, stopped = await send_reply(USER, 77, "what does this mean?")
    check("reply to a transcript is not swallowed", not stopped)
    check("transcript reply produced no output", acts == [], acts)

    # ---------- cross-user isolation ----------
    telegram.ACTIONS.clear()
    await input_flow.request(bot, chat_id=USER, user_id=USER,
                             action="test:action", prompt="For USER only")
    got.clear()
    acts, stopped = await send_reply(OTHER, prompt_id, "stolen", chat_id=USER)
    check("another user cannot consume someone else's prompt", got == {}, got)
    check("foreign reply does not stop propagation", not stopped)
    check("foreign reply leaks nothing", acts == [], acts)
    check("pending entry survives a foreign reply",
          (USER, prompt_id) in input_flow.PENDING, list(input_flow.PENDING))

    # same message_id in a DIFFERENT chat must not match
    got.clear()
    acts, stopped = await send_reply(OTHER, prompt_id, "different chat", chat_id=OTHER)
    check("same message id in another chat does not match", got == {} and not stopped, got)

    # rightful owner still works
    got.clear()
    acts, stopped = await send_reply(USER, prompt_id, "mine", chat_id=USER)
    check("rightful user still consumes their prompt", got.get("text") == "mine", got)

    # ---------- non-allowed user ----------
    telegram.ACTIONS.clear()
    await input_flow.request(bot, chat_id=USER, user_id=USER, action="test:action", prompt="p")
    got.clear()
    acts, stopped = await send_reply(999999, prompt_id, "outsider", chat_id=USER)
    check("outsider cannot consume a prompt", got == {} and not stopped and acts == [], (got, acts))

    # ---------- unknown action ----------
    telegram.ACTIONS.clear()
    await input_flow.request(bot, chat_id=USER, user_id=USER,
                             action="no:such:consumer", prompt="p")
    acts, stopped = await send_reply(USER, prompt_id, "value")
    check("unknown action tells the user it expired", "expired" in texts(acts).lower(), texts(acts)[:160])
    check("unknown action still stops propagation (no double reply)", stopped)

    # ---------- a raising consumer must not wedge the map or double-reply ----------
    @input_flow.on("test:boom")
    async def _boom(update, context, text, meta):
        raise RuntimeError("consumer exploded")

    telegram.ACTIONS.clear()
    await input_flow.request(bot, chat_id=USER, user_id=USER, action="test:boom", prompt="p")
    acts, stopped = await send_reply(USER, prompt_id, "value")
    check("raising consumer does not propagate the exception", True)
    check("raising consumer still stops propagation", stopped)
    check("raising consumer cleared its pending entry",
          (USER, prompt_id) not in input_flow.PENDING)
    check("raising consumer told the user something", acts != [], acts)

    # ---------- bounded memory ----------
    input_flow.PENDING.clear()
    for i in range(input_flow.MAX_PENDING + 25):
        input_flow.PENDING[(USER, i)] = ("test:action", USER, None)
        while len(input_flow.PENDING) > input_flow.MAX_PENDING:
            input_flow.PENDING.popitem(last=False)
    check("pending map stays bounded", len(input_flow.PENDING) <= input_flow.MAX_PENDING,
          len(input_flow.PENDING))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== input flow verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
