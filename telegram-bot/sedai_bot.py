"""
Sedai: a Telegram bot backed by Gemini for voice transcription, summarizing, drafting
replies, and general AI chat.
"""

import logging
import os
from collections import OrderedDict
from io import BytesIO

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sedai-bot")

logging.getLogger("httpx").setLevel(logging.WARNING)  # avoid leaking bot token (in request URLs) to journald

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALLOWED_USER_IDS = {int(x) for x in os.environ["ALLOWED_USER_ID"].split(",") if x.strip()}

# Transcription needs strong audio understanding, so it leads with the full model.
# Text-only tasks (chat, summarize, draft reply) lead with the cheaper lite model to
# conserve quota, since those run far more often. Each list is a fallback chain: on a
# rate-limit or server error, the next model is tried before giving up.
AUDIO_MODELS = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.1-flash-lite"]
TEXT_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.1-flash-lite"]

GEMINI_MODEL = TEXT_MODELS[0]  # default model for new chat sessions

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

_FALLBACK_STATUS_CODES = {429, 500, 503}


def _generate_with_fallback(models: list[str], contents):
    last_error = None
    for model in models:
        try:
            return gemini_client.models.generate_content(model=model, contents=contents)
        except genai_errors.APIError as e:
            last_error = e
            if e.code not in _FALLBACK_STATUS_CODES:
                raise
            log.warning("Model %s failed (%s), falling back", model, e.code)
    raise last_error

TRANSCRIBE_PROMPT = (
    "Transcribe the spoken audio verbatim, in whatever language it is spoken in, "
    "using that language's native script. Output only the transcription, no commentary, "
    "no translation, no language label."
)

SUMMARIZE_PROMPT = (
    "Summarize the following text concisely, in the same language and script it is written in. "
    "Output only the summary, no commentary, no preamble."
)

DRAFT_REPLY_PROMPT = (
    "The following text is a message someone received. Draft a short, polite reply to it, "
    "in the same language and script as the message. Output only the reply text, no commentary, "
    "no preamble, no subject line."
)

INSTRUCTED_REPLY_PROMPT = (
    "You are drafting a reply to the ORIGINAL MESSAGE below, following the INSTRUCTION for what "
    "it should say. Write the reply in the same language and script as the ORIGINAL MESSAGE. "
    "Output only the reply text, no commentary, no preamble, no subject line.\n\n"
    "ORIGINAL MESSAGE:\n{original}\n\nINSTRUCTION:\n{instruction}"
)

# (chat_id, message_id) of a transcript we attach actions to -> transcript text.
# Keyed by chat_id as well as message_id: Telegram message IDs are only unique within a
# single chat, so keying by message_id alone could let one user's button/reply pull up
# another user's transcript if their chats ever land on the same message number.
# Capped so a long-running bot doesn't leak memory.
TRANSCRIPTS: "OrderedDict[tuple[int, int], str]" = OrderedDict()
MAX_TRANSCRIPTS = 500

# chat_id -> Gemini chat session, for the plain-text "chat with Gemini" feature.
# Private chats have chat_id == the other user's user_id, so this is inherently per-person.
CHATS: "OrderedDict[int, object]" = OrderedDict()
MAX_CHATS = 50


def _remember_transcript(chat_id: int, message_id: int, text: str) -> None:
    TRANSCRIPTS[(chat_id, message_id)] = text
    while len(TRANSCRIPTS) > MAX_TRANSCRIPTS:
        TRANSCRIPTS.popitem(last=False)


def _get_chat(chat_id: int):
    entry = CHATS.get(chat_id)
    if entry is None:
        entry = [gemini_client.chats.create(model=GEMINI_MODEL), GEMINI_MODEL]
        CHATS[chat_id] = entry
        while len(CHATS) > MAX_CHATS:
            CHATS.popitem(last=False)
    else:
        CHATS.move_to_end(chat_id)
    return entry


def _send_chat_message_with_fallback(chat_id: int, text: str):
    entry = _get_chat(chat_id)
    last_error = None
    for model in TEXT_MODELS:
        chat, current_model = entry
        try:
            if current_model != model:
                chat = gemini_client.chats.create(model=model, history=chat.get_history())
                entry[0], entry[1] = chat, model
            return chat.send_message(text)
        except genai_errors.APIError as e:
            last_error = e
            if e.code not in _FALLBACK_STATUS_CODES:
                raise
            log.warning("Chat model %s failed (%s), falling back", model, e.code)
    raise last_error


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


COPY_TEXT_LIMIT = 256  # Telegram's max length for a copy_text button's payload


def _copy_keyboard(text: str):
    if len(text) > COPY_TEXT_LIMIT:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("Copy", copy_text=CopyTextButton(text=text))]])


async def _send_chunks(bot, chat_id: int, text: str, reply_to_message_id: int = None, keyboard=None):
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    sent = None
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        sent = await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            reply_to_message_id=reply_to_message_id if i == 0 else None,
            reply_markup=keyboard if is_last else None,
        )
    return sent


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        user = update.effective_user
        log.warning("Rejected message from unauthorized user_id=%s", user.id if user else None)
        return

    msg = update.effective_message
    voice_or_audio = msg.voice or msg.audio
    if voice_or_audio is None:
        return

    await msg.chat.send_action("typing")

    tg_file = await context.bot.get_file(voice_or_audio.file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(out=buf)
    audio_bytes = buf.getvalue()

    mime_type = "audio/ogg" if msg.voice else (voice_or_audio.mime_type or "audio/mpeg")

    try:
        response = _generate_with_fallback(
            AUDIO_MODELS,
            [
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                TRANSCRIBE_PROMPT,
            ],
        )
        text = response.text.strip() if response.text else "(empty transcription)"
    except Exception as e:
        log.exception("Gemini transcription failed")
        await msg.reply_text(f"Transcription failed: {e}")
        return

    # If this voice note is a reply to a transcript we know about, treat it as spoken
    # instructions for how to reply to that original message, rather than a standalone note.
    reply_target = msg.reply_to_message
    original_text = TRANSCRIPTS.get((msg.chat_id, reply_target.message_id)) if reply_target else None

    if original_text is not None:
        instruction_sent = await _send_chunks(context.bot, msg.chat_id, text, reply_to_message_id=msg.message_id)
        _remember_transcript(msg.chat_id, instruction_sent.message_id, text)

        await msg.chat.send_action("typing")
        try:
            draft_response = _generate_with_fallback(
                TEXT_MODELS,
                [INSTRUCTED_REPLY_PROMPT.format(original=original_text, instruction=text)],
            )
            draft = draft_response.text.strip() if draft_response.text else "(empty response)"
        except Exception as e:
            log.exception("Gemini instructed-reply request failed")
            draft = f"Request failed: {e}"

        await _send_chunks(
            context.bot, msg.chat_id, draft,
            reply_to_message_id=instruction_sent.message_id,
            keyboard=_copy_keyboard(draft),
        )
        return

    sent = await _send_chunks(context.bot, msg.chat_id, text, reply_to_message_id=msg.message_id)
    _remember_transcript(msg.chat_id, sent.message_id, text)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Summarize", callback_data=f"summarize:{sent.message_id}"),
        InlineKeyboardButton("Draft reply", callback_data=f"reply:{sent.message_id}"),
    ]])
    await sent.edit_reply_markup(reply_markup=keyboard)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return

    action, _, message_id_str = (query.data or "").partition(":")
    text = (
        TRANSCRIPTS.get((query.message.chat_id, int(message_id_str)))
        if message_id_str.isdigit()
        else None
    )
    if text is None:
        await query.answer("This transcript is no longer available.", show_alert=True)
        return

    await query.answer()
    await query.message.chat.send_action("typing")

    prompt = SUMMARIZE_PROMPT if action == "summarize" else DRAFT_REPLY_PROMPT
    try:
        response = _generate_with_fallback(TEXT_MODELS, [text, prompt])
        result = response.text.strip() if response.text else "(empty response)"
    except Exception as e:
        log.exception("Gemini follow-up request failed")
        result = f"Request failed: {e}"

    keyboard = _copy_keyboard(result) if action == "reply" else None
    await _send_chunks(
        context.bot, query.message.chat_id, result,
        reply_to_message_id=query.message.message_id,
        keyboard=keyboard,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    msg = update.effective_message
    if not msg.text:
        return

    await msg.chat.send_action("typing")
    try:
        response = _send_chat_message_with_fallback(msg.chat_id, msg.text)
        text = response.text.strip() if response.text else "(empty response)"
    except Exception as e:
        log.exception("Gemini chat request failed")
        text = f"Request failed: {e}"

    await _send_chunks(context.bot, msg.chat_id, text)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    CHATS.pop(update.effective_message.chat_id, None)
    await update.effective_message.reply_text("Conversation history cleared.")


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^(summarize|reply):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot started, polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
