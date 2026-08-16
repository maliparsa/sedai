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
from google.genai import errors as genai_errors
from google.genai import types

import settings
import settings_ui
import style_ui
import help_ui

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sedai-bot")

logging.getLogger("httpx").setLevel(logging.WARNING)  # avoid leaking bot token (in request URLs) to journald

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_FALLBACK_STATUS_CODES = {429, 500, 503}


def _generate_with_fallback(models: list[str], contents):
    last_error = None
    for model in models:
        try:
            return settings.gemini_client().models.generate_content(model=model, contents=contents)
        except genai_errors.APIError as e:
            last_error = e
            if e.code not in _FALLBACK_STATUS_CODES:
                raise
            log.warning("Model %s failed (%s), falling back", model, e.code)
    raise last_error


def _with_style(prompt: str, style: str | None) -> str:
    if not style:
        return prompt
    return prompt + "\n\n" + STYLE_SECTION.format(style=style)


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

STYLE_SECTION = (
    "STANDING INSTRUCTION FROM THE USER (governs tone, register, and formatting):\n"
    "{style}\n"
    "Follow it unless it conflicts with a more specific instruction above, which wins. "
    "Keep the original language and script unless this standing instruction explicitly "
    "says otherwise. Never invent facts that are not in the source text in order to "
    "satisfy it."
)

# (chat_id, message_id) of a transcript we attach actions to -> transcript text.
# Keyed by chat_id as well as message_id: Telegram message IDs are only unique within a
# single chat, so keying by message_id alone could let one user's button/reply pull up
# another user's transcript if their chats ever land on the same message number.
# Capped so a long-running bot doesn't leak memory.
TRANSCRIPTS: "OrderedDict[tuple[int, int], str]" = OrderedDict()
MAX_TRANSCRIPTS = 500

# chat_id -> [chat, model, style] for the plain-text "chat with Gemini" feature.
# chat: Gemini session object, model: currently active model, style: the user's chat style when this session was created.
# Private chats have chat_id == the other user's user_id, so this is inherently per-person.
CHATS: "OrderedDict[int, list]" = OrderedDict()
MAX_CHATS = 50


def _remember_transcript(chat_id: int, message_id: int, text: str) -> None:
    TRANSCRIPTS[(chat_id, message_id)] = text
    while len(TRANSCRIPTS) > MAX_TRANSCRIPTS:
        TRANSCRIPTS.popitem(last=False)


def _get_chat(chat_id: int, user_id: int):
    entry = CHATS.get(chat_id)
    model = settings.text_models(user_id)[0]
    style = settings.get_user_style(user_id, "chat")
    if entry is None:
        config = types.GenerateContentConfig(system_instruction=style) if style else None
        chat = settings.gemini_client().chats.create(model=model, config=config)
        entry = [chat, model, style]
        CHATS[chat_id] = entry
        while len(CHATS) > MAX_CHATS:
            CHATS.popitem(last=False)
    else:
        CHATS.move_to_end(chat_id)
    return entry


def _send_chat_message_with_fallback(chat_id: int, user_id: int, text: str):
    entry = _get_chat(chat_id, user_id)
    current_style = settings.get_user_style(user_id, "chat")
    last_error = None
    for model in settings.text_models(user_id):
        chat, current_model, session_style = entry
        try:
            if current_model != model or current_style != session_style:
                config = types.GenerateContentConfig(system_instruction=current_style) if current_style else None
                chat = settings.gemini_client().chats.create(model=model, config=config, history=chat.get_history())
                entry[0], entry[1], entry[2] = chat, model, current_style
            return chat.send_message(text)
        except genai_errors.APIError as e:
            last_error = e
            if e.code not in _FALLBACK_STATUS_CODES:
                raise
            log.warning("Chat model %s failed (%s), falling back", model, e.code)
    raise last_error


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and settings.is_allowed(user.id)


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

    user = update.effective_user
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
            settings.audio_models(user.id),
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
            formatted_prompt = INSTRUCTED_REPLY_PROMPT.format(original=original_text, instruction=text)
            prompt_with_style = _with_style(formatted_prompt, settings.get_user_style(user.id, "reply"))
            draft_response = _generate_with_fallback(
                settings.text_models(user.id),
                [prompt_with_style],
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

    user = update.effective_user
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

    if action == "summarize":
        prompt = _with_style(SUMMARIZE_PROMPT, settings.get_user_style(user.id, "summary"))
    else:
        prompt = _with_style(DRAFT_REPLY_PROMPT, settings.get_user_style(user.id, "reply"))
    try:
        response = _generate_with_fallback(settings.text_models(user.id), [text, prompt])
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

    user = update.effective_user
    msg = update.effective_message
    if not msg.text:
        return

    await msg.chat.send_action("typing")
    try:
        response = _send_chat_message_with_fallback(msg.chat_id, user.id, msg.text)
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
    settings.load()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(help_ui.post_init).build()
    settings_ui.register(app)
    settings_ui.set_key_change_hook(CHATS.clear)
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^(summarize|reply):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    style_ui.register(app)
    help_ui.register(app)
    log.info("Bot started, polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
