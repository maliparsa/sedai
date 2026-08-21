"""Offline verification of image_ui.py: edit flows, budget enforcement, failure modes."""

import asyncio
import importlib
import os
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(TEST_DIR)
STUBS = os.path.join(TEST_DIR, "stubs")

ADMIN, USER, OUTSIDER = 111, 222, 999

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def setup():
    for m in ("settings", "settings_ui", "input_flow", "image_ui", "common"):
        sys.modules.pop(m, None)
    tmp = tempfile.mkdtemp()
    os.environ["SEDAI_SETTINGS_PATH"] = os.path.join(tmp, "settings.json")
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = "good-key-aaaa1111"
    os.environ["ALLOWED_USER_ID"] = f"{ADMIN},{USER}"
    import settings
    import input_flow
    import image_ui
    importlib.reload(settings)
    importlib.reload(input_flow)
    importlib.reload(image_ui)
    settings.load()
    from telegram.ext import Application
    app = Application.builder().token("x").build()
    input_flow.register(app)
    image_ui.register(app)
    return settings, input_flow, image_ui, app


def texts(actions):
    out = []
    for a in actions:
        for item in a:
            if isinstance(item, str):
                out.append(item)
    return " | ".join(out)


def photo_message(message_id=1, chat_id=100, caption=None, user_id=USER,
                  media_group_id=None, document=None):
    from telegram import Message, PhotoSize
    msg = Message(message_id=message_id, chat_id=chat_id, user_id=user_id)
    if document is not None:
        msg.document = document
    else:
        msg.photo = [PhotoSize(file_id=f"src-{message_id}")]
    msg.caption = caption
    msg.media_group_id = media_group_id
    return msg


async def drive(fn, msg, user_id):
    import telegram
    from telegram import Update
    from telegram.ext import _Ctx
    telegram.ACTIONS.clear()
    await fn(Update(message=msg, user_id=user_id), _Ctx())
    return list(telegram.ACTIONS)


async def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)

    import google.genai as genai_stub
    settings, input_flow, image_ui, app = setup()

    # ---- settings layer -------------------------------------------------
    check("image is a model kind", "image" in settings.MODEL_KINDS, settings.MODEL_KINDS)
    check("image is a style kind", "image" in settings.STYLE_KINDS, settings.STYLE_KINDS)
    check("image chain defaults populated", len(settings.image_models(USER)) >= 2,
          settings.image_models(USER))
    check("default budget is $10", settings.image_budget() == 10.0, settings.image_budget())

    cost = settings.image_cost("gemini-3.1-flash-image", 275, 1193)
    check("cost matches published rates", abs(cost - 0.0717) < 0.0005, cost)
    check("unknown model priced at the top rate",
          settings.image_cost("brand-new-image", 275, 1193) > cost,
          settings.image_cost("brand-new-image", 275, 1193))

    settings.set_image_budget(0)
    check("budget 0 is honoured, not treated as unset", settings.image_budget() == 0.0,
          settings.image_budget())
    settings.set_image_budget(None)
    check("budget None restores the default", settings.image_budget() == 10.0,
          settings.image_budget())

    img_list = settings.models_for_kind("image")
    txt_list = settings.models_for_kind("text")
    check("image picker lists only image models",
          img_list and all(settings.is_image_model(m) for m in img_list), img_list)
    check("text picker excludes image models",
          txt_list and not any(settings.is_image_model(m) for m in txt_list), txt_list)

    # ---- photo with a caption -------------------------------------------
    genai_stub.reset_image_stub()
    acts = await drive(image_ui.handle_photo, photo_message(caption="make it blue"), USER)
    kinds = [a[0] for a in acts]
    check("captioned photo produces an image", "send_photo" in kinds, kinds)
    spend = settings.image_spend()
    check("spend recorded after a generation", spend["count"] == 1 and spend["usd"] > 0, spend)
    check("spend attributed to the user", str(USER) in spend["users"], spend["users"])
    check("result carries Again / Send as file buttons",
          any(k == "edit_reply_markup" for k in kinds), kinds)

    # ---- refinement by replying to a result ------------------------------
    from telegram import Message
    reply = Message(message_id=50, chat_id=100, text="warmer tones", user_id=USER)
    reply.reply_to_message = Message(message_id=8888, chat_id=100)
    before = settings.image_spend()["count"]
    try:
        acts = await drive(image_ui.handle_image_reply, reply, USER)
    except Exception as e:
        acts = []
        check("refinement stops further handlers",
              type(e).__name__ == "ApplicationHandlerStop", type(e).__name__)
    check("reply to a result re-edits it",
          settings.image_spend()["count"] == before + 1, settings.image_spend())

    # A reply to something we did not send must fall through to plain chat untouched.
    stray = Message(message_id=51, chat_id=100, text="hello", user_id=USER)
    stray.reply_to_message = Message(message_id=4242, chat_id=100)
    before = settings.image_spend()["count"]
    acts = await drive(image_ui.handle_image_reply, stray, USER)
    check("unrelated reply falls through untouched",
          acts == [] and settings.image_spend()["count"] == before, acts)

    # Message IDs repeat across chats: another chat's id 8888 is not this chat's image.
    other = Message(message_id=52, chat_id=777, text="change it", user_id=USER)
    other.reply_to_message = Message(message_id=8888, chat_id=777)
    before = settings.image_spend()["count"]
    acts = await drive(image_ui.handle_image_reply, other, USER)
    check("images are scoped per chat, not per message id",
          acts == [] and settings.image_spend()["count"] == before, acts)

    # ---- photo with no caption asks, then consumes the answer ------------
    acts = await drive(image_ui.handle_photo, photo_message(message_id=2), USER)
    check("uncaptioned photo asks what to change",
          any("What should I change" in t for t in texts(acts).split(" | ")), texts(acts))
    check("asking costs nothing", settings.image_spend()["count"] == before, settings.image_spend())

    pending_keys = list(input_flow.PENDING.keys())
    check("a pending input was registered", len(pending_keys) == 1, pending_keys)
    if pending_keys:
        action, uid, meta = input_flow.PENDING[pending_keys[0]]
        check("pending input is the image action", action == "image_edit", action)
        check("pending input remembers the source file", meta and meta[0] == "src-2", meta)

    # ---- failure modes ---------------------------------------------------
    genai_stub.IMAGE_MODE = "safety"
    acts = await drive(image_ui.handle_photo, photo_message(message_id=3, caption="x"), USER)
    kinds = [a[0] for a in acts]
    check("safety block sends words, not an image", "send_photo" not in kinds, kinds)
    check("safety block is explained", "safety" in texts(acts).lower(), texts(acts))

    genai_stub.IMAGE_MODE = "text_only"
    acts = await drive(image_ui.handle_photo, photo_message(message_id=4, caption="x"), USER)
    check("a prose refusal is passed through", "can't edit that one" in texts(acts), texts(acts))

    genai_stub.IMAGE_MODE = "ok"
    genai_stub.IMAGE_RAISE = 429
    acts = await drive(image_ui.handle_photo, photo_message(message_id=5, caption="x"), USER)
    check("rate limiting is reported plainly", "rate-limited" in texts(acts), texts(acts))
    genai_stub.IMAGE_RAISE = 400
    acts = await drive(image_ui.handle_photo, photo_message(message_id=6, caption="x"), USER)
    check("a hard API error never echoes the provider body",
          "unavailable" not in texts(acts) and "APIError" in texts(acts), texts(acts))
    genai_stub.reset_image_stub()

    # ---- budget enforcement ----------------------------------------------
    settings.set_image_budget(5)
    for _ in range(80):
        settings.record_image_spend(USER, "gemini-3.1-flash-image", 275, 1193)
    before = settings.image_spend()["count"]
    acts = await drive(image_ui.handle_photo, photo_message(message_id=7, caption="x"), USER)
    kinds = [a[0] for a in acts]
    check("over budget refuses to generate", "send_photo" not in kinds, kinds)
    check("over budget spends nothing more",
          settings.image_spend()["count"] == before, settings.image_spend())
    check("over budget explains itself", "budget" in texts(acts).lower(), texts(acts))
    check("a regular user is pointed at the admin, not the settings menu",
          "admin" in texts(acts).lower(), texts(acts))

    settings.set_image_budget(0)
    acts = await drive(image_ui.handle_photo, photo_message(message_id=8, caption="x"), USER)
    check("budget of $0 switches image editing off", "switched off" in texts(acts), texts(acts))
    settings.reset_image_spend()
    settings.set_image_budget(None)
    check("reset clears the running total", settings.image_spend()["usd"] == 0.0,
          settings.image_spend())

    # ---- albums -----------------------------------------------------------
    acts1 = await drive(image_ui.handle_photo,
                        photo_message(message_id=10, caption="edit", media_group_id="g1"), USER)
    acts2 = await drive(image_ui.handle_photo,
                        photo_message(message_id=11, caption="edit", media_group_id="g1"), USER)
    check("an album is answered once", acts2 == [], [a[0] for a in acts2])
    check("the album's first photo is still edited",
          "send_photo" in [a[0] for a in acts1], [a[0] for a in acts1])

    # ---- documents --------------------------------------------------------
    from telegram import Document
    acts = await drive(image_ui.handle_photo,
                       photo_message(message_id=12, caption="edit",
                                     document=Document(mime_type="image/png")), USER)
    check("an image sent as a file is edited too",
          "send_photo" in [a[0] for a in acts], [a[0] for a in acts])

    big = Document(mime_type="image/png", file_size=30_000_000)
    acts = await drive(image_ui.handle_photo,
                       photo_message(message_id=13, caption="edit", document=big), USER)
    check("an oversized file is refused before download",
          "send_photo" not in [a[0] for a in acts] and "MB" in texts(acts), texts(acts))

    non_image = Document(mime_type="application/pdf")
    acts = await drive(image_ui.handle_photo,
                       photo_message(message_id=14, caption="edit", document=non_image), USER)
    check("a non-image document is ignored", acts == [], [a[0] for a in acts])

    # ---- authorization -----------------------------------------------------
    before = settings.image_spend()["count"]
    acts = await drive(image_ui.handle_photo,
                       photo_message(message_id=15, caption="edit", user_id=OUTSIDER), OUTSIDER)
    check("an outsider gets no reply at all", acts == [], [a[0] for a in acts])
    check("an outsider spends nothing", settings.image_spend()["count"] == before)

    # ---- callback wiring ---------------------------------------------------
    patterns = app.callback_patterns()
    check("image callbacks are registered",
          any("img:" in (p or "") for p in patterns), patterns)
    check("image callbacks cannot match the transcript buttons",
          not any((p or "").startswith("^(summarize|reply)") and "img" in p for p in patterns),
          patterns)

    import telegram
    from telegram import CallbackQuery, Update
    from telegram.ext import _Ctx
    telegram.ACTIONS.clear()
    cb = CallbackQuery(data="img:file:8888", user_id=USER, chat_id=100, message_id=8888)
    await image_ui.handle_callback(Update(callback_query=cb), _Ctx())
    check("Send as file returns the uncompressed original",
          any(a[0] == "send_document" for a in telegram.ACTIONS),
          [a[0] for a in telegram.ACTIONS])

    telegram.ACTIONS.clear()
    before = settings.image_spend()["count"]
    cb = CallbackQuery(data="img:again:8888", user_id=USER, chat_id=100, message_id=8888)
    await image_ui.handle_callback(Update(callback_query=cb), _Ctx())
    check("Again re-runs the same instruction",
          settings.image_spend()["count"] == before + 1, settings.image_spend())

    telegram.ACTIONS.clear()
    cb = CallbackQuery(data="img:again:12345", user_id=USER, chat_id=100, message_id=12345)
    await image_ui.handle_callback(Update(callback_query=cb), _Ctx())
    check("a stale image button says so, and spends nothing",
          any(a[0] == "answer" for a in telegram.ACTIONS), [a[0] for a in telegram.ACTIONS])

    # ---- standing instruction ------------------------------------------------
    settings.set_user_style(USER, "image", "always photorealistic")
    genai_stub.CALLS.clear()
    await drive(image_ui.handle_photo, photo_message(message_id=20, caption="add a hat"), USER)
    sent_prompts = [c[3] for c in genai_stub.CALLS if c[0] == "generate"]
    check("the standing image instruction reaches the model",
          any("photorealistic" in p for p in sent_prompts), sent_prompts[:1])
    check("the per-message instruction reaches the model",
          any("add a hat" in p for p in sent_prompts), sent_prompts[:1])

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== image_ui.py verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
