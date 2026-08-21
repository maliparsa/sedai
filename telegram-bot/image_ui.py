"""
Sedai image editing: send a photo with an instruction, get the edited image back.

Unlike every other feature here, image generation has no free tier on the Gemini API — each
call costs real money — so every path through this module passes a monthly budget check
before spending anything, and records the actual cost the API reports afterwards.
"""

import asyncio
import logging
from collections import OrderedDict
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import FileSizeLimit
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from google.genai import errors as genai_errors
from google.genai import types

import common
import input_flow
import settings

log = logging.getLogger("sedai-bot")

_FALLBACK_STATUS_CODES = {429, 500, 503}

IMAGE_EDIT_PROMPT = (
    "Edit the attached image according to the INSTRUCTION below and return the edited image. "
    "Leave everything the instruction does not mention unchanged. Output the image itself; "
    "add a short line of text only if you need to explain something about the edit.\n\n"
    "INSTRUCTION:\n{instruction}"
)

IMAGE_STYLE_SECTION = (
    "\n\nSTANDING INSTRUCTION FROM THE USER FOR IMAGE EDITS:\n"
    "{style}\n"
    "Apply it unless the INSTRUCTION above explicitly conflicts with it, in which case the "
    "INSTRUCTION wins."
)

# (chat_id, message_id of a result we sent) -> {"source", "result", "instruction"}
# Keyed by chat_id as well as message_id for the same reason TRANSCRIPTS is: Telegram
# message IDs are only unique within one chat, so keying by message_id alone could let one
# user's reply act on another user's image.
IMAGES: "OrderedDict[tuple[int, int], dict]" = OrderedDict()
MAX_IMAGES = 200

# (chat_id, message_id) -> (bytes, mime) for results we still hold at full quality.
# Telegram recompresses anything sent as a photo, so "Send as file" can only return a true
# original while the bytes are still here. Small cap: this is the one structure that holds
# image data in memory.
ORIGINALS: "OrderedDict[tuple[int, int], tuple[bytes, str]]" = OrderedDict()
MAX_ORIGINALS = 20

# media_group_id values already answered, so an album produces one reply rather than one per
# photo. Multi-image composition is a separate feature; this just keeps albums predictable.
_SEEN_ALBUMS: "OrderedDict[str, bool]" = OrderedDict()
MAX_ALBUMS = 50

CAPTION_LIMIT = 1024  # Telegram's max caption length


def _remember(chat_id: int, message_id: int, source: str, result: str, instruction: str) -> None:
    IMAGES[(chat_id, message_id)] = {
        "source": source, "result": result, "instruction": instruction,
    }
    while len(IMAGES) > MAX_IMAGES:
        IMAGES.popitem(last=False)


def _remember_original(chat_id: int, message_id: int, data: bytes, mime: str) -> None:
    ORIGINALS[(chat_id, message_id)] = (data, mime)
    while len(ORIGINALS) > MAX_ORIGINALS:
        ORIGINALS.popitem(last=False)


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and settings.is_allowed(user.id)


def _with_style(prompt: str, style: str | None) -> str:
    if not style:
        return prompt
    return prompt + IMAGE_STYLE_SECTION.format(style=style)


def _budget_message(user_id: int) -> str:
    """Explain a refused generation. Never shown unless the budget actually blocks."""
    budget = settings.image_budget()
    spend = settings.image_spend()
    if budget <= 0:
        text = "Image editing is switched off — the monthly image budget is set to $0."
    else:
        text = (
            f"This month's image budget is used up: ${spend['usd']:.2f} of ${budget:.2f} "
            f"across {spend['count']} images. It resets at the start of next month."
        )
    if settings.is_admin(user_id):
        text += "\n\nYou can raise or reset it under /settings → Image budget."
    else:
        text += "\n\nAsk the admin to raise it if you need more."
    return text


def _extract(response):
    """Pull (image_bytes, mime, text, finish_reason) out of a generate_content response."""
    image_bytes, mime, texts, finish = None, None, [], None
    for candidate in (response.candidates or []):
        finish = getattr(candidate, "finish_reason", None)
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None) and image_bytes is None:
                image_bytes = inline.data
                mime = getattr(inline, "mime_type", None) or "image/png"
            if getattr(part, "text", None):
                texts.append(part.text)
        if image_bytes is not None:
            break
    return image_bytes, mime, "\n".join(texts).strip(), finish


def _generate(user_id: int, image_bytes: bytes, mime: str, instruction: str):
    """Run the edit, walking the model chain on retryable errors. Blocking; call in a thread.

    Returns (image_bytes, mime, text, finish_reason, model, cost_usd).
    """
    prompt = _with_style(
        IMAGE_EDIT_PROMPT.format(instruction=instruction),
        settings.get_user_style(user_id, "image"),
    )
    contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime), prompt]

    last_error = None
    for model in settings.image_models(user_id):
        try:
            response = settings.gemini_client().models.generate_content(
                model=model, contents=contents,
            )
        except genai_errors.APIError as e:
            last_error = e
            if e.code not in _FALLBACK_STATUS_CODES:
                raise
            log.warning("Image model %s failed (%s), falling back", model, e.code)
            continue

        usage = getattr(response, "usage_metadata", None)
        cost = settings.record_image_spend(
            user_id, model,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )
        out_bytes, out_mime, text, finish = _extract(response)
        return out_bytes, out_mime, text, finish, model, cost

    raise last_error


async def _keep_typing(chat, action: str = "upload_photo"):
    """Hold the chat action for the length of a generation, which outlasts one send_action."""
    try:
        while True:
            await chat.send_action(action)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass  # a failed chat action must never break the request it decorates


async def _download(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(out=buf)
    return buf.getvalue()


def _result_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Again", callback_data=f"img:again:{message_id}"),
        InlineKeyboardButton("Send as file", callback_data=f"img:file:{message_id}"),
    ]])


async def run_edit(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   source_file_id: str, source_mime: str, instruction: str,
                   reply_to_message_id: int) -> None:
    """Budget-check, generate, and deliver one edited image."""
    user = update.effective_user
    msg = update.effective_message
    chat_id = msg.chat_id

    instruction = (instruction or "").strip()
    if not instruction:
        await context.bot.send_message(
            chat_id=chat_id, text="Tell me what to change and I'll edit it.",
            reply_to_message_id=reply_to_message_id,
        )
        return

    head_model = (settings.image_models(user.id) or ["gemini-3.1-flash-image"])[0]
    allowed, _remaining = settings.can_generate_image(head_model)
    if not allowed:
        await context.bot.send_message(
            chat_id=chat_id, text=_budget_message(user.id),
            reply_to_message_id=reply_to_message_id,
        )
        return

    typing = asyncio.create_task(_keep_typing(msg.chat))
    try:
        try:
            source_bytes = await _download(context, source_file_id)
        except Exception as e:
            log.exception("Failed to download image from Telegram")
            await context.bot.send_message(
                chat_id=chat_id,
                text=common.user_error("Couldn't download that image from Telegram", e),
                reply_to_message_id=reply_to_message_id,
            )
            return

        try:
            # The SDK is synchronous and a generation takes 10-20s; off-thread so the bot
            # keeps answering everyone else meanwhile.
            out_bytes, out_mime, text, finish, model, cost = await asyncio.to_thread(
                _generate, user.id, source_bytes, source_mime, instruction,
            )
        except genai_errors.APIError as e:
            log.exception("Gemini image generation failed")
            if e.code == 429:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=("The image models are rate-limited or out of quota right now. "
                          "Try again in a minute."),
                    reply_to_message_id=reply_to_message_id,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, text=common.user_error("That edit failed", e),
                    reply_to_message_id=reply_to_message_id,
                )
            return
        except Exception as e:
            log.exception("Gemini image generation failed")
            await context.bot.send_message(
                chat_id=chat_id, text=common.user_error("That edit failed", e),
                reply_to_message_id=reply_to_message_id,
            )
            return
    finally:
        typing.cancel()

    if out_bytes is None:
        # No image came back. Either the model refused on safety grounds, or it answered in
        # words. Its own words are more useful than anything generic, when there are any.
        finish_name = getattr(finish, "name", str(finish or ""))
        if "SAFETY" in finish_name.upper() or "PROHIBITED" in finish_name.upper():
            reply = ("I can't make that edit — it was blocked by the model's safety filters. "
                     "Try rephrasing, or a different image.")
        elif text:
            reply = text[:3500]
        else:
            reply = "The model didn't return an image for that. Try rephrasing the instruction."
        await context.bot.send_message(
            chat_id=chat_id, text=reply, reply_to_message_id=reply_to_message_id,
        )
        return

    caption = text[:CAPTION_LIMIT] if text else None
    log.info("Image edited with %s, cost $%.4f", model, cost)

    sent = await context.bot.send_photo(
        chat_id=chat_id, photo=BytesIO(out_bytes), caption=caption,
        reply_to_message_id=reply_to_message_id,
    )
    result_file_id = sent.photo[-1].file_id if sent.photo else None
    _remember(chat_id, sent.message_id, source_file_id, result_file_id, instruction)
    _remember_original(chat_id, sent.message_id, out_bytes, out_mime or "image/png")
    await sent.edit_reply_markup(reply_markup=_result_keyboard(sent.message_id))


def _photo_from_message(msg):
    """Return (file_id, mime) for a photo or an image document, or (None, None)."""
    if msg.photo:
        return msg.photo[-1].file_id, "image/jpeg"
    doc = msg.document
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        return doc.file_id, doc.mime_type
    return None, None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        user = update.effective_user
        log.warning("Rejected image from unauthorized user_id=%s", user.id if user else None)
        return

    user = update.effective_user
    msg = update.effective_message
    file_id, mime = _photo_from_message(msg)
    if file_id is None:
        return

    # Telegram delivers an album as one update per photo. Answer the first and say so, rather
    # than firing a separate edit — and a separate charge — for every member.
    group_id = msg.media_group_id
    if group_id:
        if group_id in _SEEN_ALBUMS:
            return
        _SEEN_ALBUMS[group_id] = True
        while len(_SEEN_ALBUMS) > MAX_ALBUMS:
            _SEEN_ALBUMS.popitem(last=False)
        await msg.reply_text("I can only edit one image at a time — using the first one.")

    # Documents can be far larger than photos, and this ceiling is a Bot API limit no retry
    # can beat. Same pre-check as handle_voice.
    doc = msg.document
    if doc is not None and doc.file_size and doc.file_size > FileSizeLimit.FILESIZE_DOWNLOAD:
        limit_mb = int(FileSizeLimit.FILESIZE_DOWNLOAD) / 1_000_000
        await msg.reply_text(
            f"That file is {doc.file_size / 1_000_000:.0f} MB. Telegram only lets bots "
            f"download up to {limit_mb:.0f} MB, so I can't fetch it."
        )
        return

    caption = (msg.caption or "").strip()
    if caption:
        await run_edit(update, context, file_id, mime, caption, msg.message_id)
        return

    if not settings.can_generate_image(
        (settings.image_models(user.id) or ["gemini-3.1-flash-image"])[0]
    )[0]:
        await msg.reply_text(_budget_message(user.id))
        return

    await input_flow.request(
        msg, msg.chat_id, user.id, "image_edit",
        "What should I change about this image?", meta=(file_id, mime),
    )


@input_flow.on("image_edit")
async def _on_image_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                text: str, meta) -> None:
    file_id, mime = meta
    await run_edit(update, context, file_id, mime, text, update.effective_message.message_id)


async def handle_image_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A text reply to an image we produced is a refinement of that image."""
    if not _is_allowed(update):
        return

    msg = update.effective_message
    reply_to = msg.reply_to_message
    if reply_to is None or not msg.text:
        return

    entry = IMAGES.get((msg.chat_id, reply_to.message_id))
    if entry is None:
        return  # not ours: fall through to normal chat handling

    await run_edit(update, context, entry["result"], "image/jpeg", msg.text, msg.message_id)
    raise ApplicationHandlerStop()


def instruction_target(chat_id: int, message_id: int):
    """If message_id is an image we produced, return its file_id for a further edit.

    Used by the voice handler so a spoken reply to an image becomes an edit instruction,
    mirroring the spoken-reply-to-a-transcript flow.
    """
    entry = IMAGES.get((chat_id, message_id))
    return entry["result"] if entry else None


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return

    _, _, rest = (query.data or "").partition(":")
    action, _, message_id_str = rest.partition(":")
    if not message_id_str.isdigit():
        await query.answer()
        return

    chat_id = query.message.chat_id
    message_id = int(message_id_str)
    entry = IMAGES.get((chat_id, message_id))
    if entry is None:
        await query.answer("That image is no longer available.", show_alert=True)
        return

    if action == "again":
        await query.answer("Regenerating…")
        await run_edit(update, context, entry["source"], "image/jpeg",
                       entry["instruction"], message_id)
        return

    if action == "file":
        original = ORIGINALS.get((chat_id, message_id))
        if original is None:
            await query.answer(
                "The full-quality copy is no longer cached — tap Again to regenerate it.",
                show_alert=True,
            )
            return
        await query.answer("Sending…")
        data, mime = original
        ext = "png" if "png" in (mime or "") else "jpg"
        await context.bot.send_document(
            chat_id=chat_id,
            document=BytesIO(data),
            filename=f"edited-{message_id}.{ext}",
            reply_to_message_id=message_id,
        )


def register(app: Application) -> None:
    """Register image handlers.

    Must be called AFTER input_flow.register so a reply to a ForceReply prompt is consumed
    there first; a reply that is not to a prompt then falls through to the refinement
    handler, and anything that is neither falls through again to plain chat.
    """
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_image_reply),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^img:(again|file):\d+$"))
