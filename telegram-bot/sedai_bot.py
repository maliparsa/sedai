"""
Sedai: a Telegram bot backed by Gemini for voice transcription, summarizing, drafting
replies, and general AI chat.
"""

import logging
import os
from collections import OrderedDict
from io import BytesIO

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import FileSizeLimit
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

import common
import settings
import settings_ui
import style_ui
import input_flow
import help_ui
import image_ui

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sedai-bot")

logging.getLogger("httpx").setLevel(logging.WARNING)  # avoid leaking bot token (in request URLs) to journald

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_FALLBACK_STATUS_CODES = {429, 500, 503}


# Shared with image_ui; see common.user_error for why the exception is never interpolated.
_user_error = common.user_error


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


def _with_style(prompt: str, style: str | None, section: str | None = None) -> str:
    if not style:
        return prompt
    return prompt + "\n\n" + (section or STYLE_SECTION).format(style=style)


def _transcribe_prompt_for(user_id: int, duration: int | None) -> str:
    """Assemble the transcription prompt for this user and recording length."""
    prompt = TRANSCRIBE_PROMPT
    if settings.should_timestamp(user_id, duration):
        prompt += AUTO_TIMESTAMP_CLAUSE
    return _with_style(
        prompt,
        settings.get_user_style(user_id, "transcript"),
        TRANSCRIPT_STYLE_SECTION,
    )


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

# Transcription is the one task with a ground truth, so its standing instruction needs
# the opposite precedence from the others: TRANSCRIBE_PROMPT's "verbatim, output only the
# transcription" would otherwise override any request for timestamps or speaker labels,
# and the instruction would silently do nothing.
# Appended to TRANSCRIBE_PROMPT for recordings past the user's threshold. It sits ABOVE the
# standing instruction so TRANSCRIPT_STYLE_SECTION's "this instruction wins" precedence lets
# an explicit /transcriptstyle override it — otherwise the two would contradict each other
# with no defined winner. Cue length is stated in words because asking for sentences gives
# wildly uneven cues on unscripted speech.
AUTO_TIMESTAMP_CLAUSE = (
    "\n\nThis is a long recording, so present the transcript as caption cues: break it into "
    "chunks of roughly 10 to 15 words, each on its own line, beginning with a [MM:SS] "
    "timestamp (use [HH:MM:SS] past one hour) marking where that chunk starts in the audio. "
    "Split the speech only — never reword, merge, or summarise it."
)

TRANSCRIPT_STYLE_SECTION = (
    "STANDING INSTRUCTION FROM THE USER FOR TRANSCRIPTS:\n"
    "{style}\n"
    "Where this conflicts with the default output rules above, this instruction wins: if it "
    "asks for timestamps, speaker labels, paragraph breaks, translation, or any other "
    "formatting, provide them. Timestamps, when asked for, use [MM:SS] (or [HH:MM:SS] past "
    "an hour) and mark where that passage begins in the audio. Never invent speech that is "
    "not in the audio in order to satisfy this instruction."
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

    # Telegram reports the size up front, so an oversized file can be refused with a useful
    # message instead of a failed transfer. FILESIZE_DOWNLOAD is a hard Bot API ceiling on
    # what any bot may download — it is not something a retry or a bigger server can beat.
    file_size = getattr(voice_or_audio, "file_size", None)
    if file_size and file_size > FileSizeLimit.FILESIZE_DOWNLOAD:
        limit_mb = int(FileSizeLimit.FILESIZE_DOWNLOAD) / 1_000_000
        log.warning("Refused oversized audio: %s bytes from user_id=%s", file_size, user.id)
        await msg.reply_text(
            f"That file is {file_size / 1_000_000:.0f} MB. Telegram only lets bots download "
            f"up to {limit_mb:.0f} MB, so I can't fetch it.\n\n"
            "A voice note of the same length is usually small enough, or send a "
            "lower-bitrate copy or a shorter section."
        )
        return

    await msg.chat.send_action("typing")

    try:
        tg_file = await context.bot.get_file(voice_or_audio.file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(out=buf)
        audio_bytes = buf.getvalue()
    except Exception as e:
        log.exception("Failed to download audio from Telegram")
        await msg.reply_text(_user_error("Couldn't download that audio from Telegram", e))
        return

    mime_type = "audio/ogg" if msg.voice else (voice_or_audio.mime_type or "audio/mpeg")

    try:
        response = _generate_with_fallback(
            settings.audio_models(user.id),
            [
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                _transcribe_prompt_for(user.id, getattr(voice_or_audio, "duration", None)),
            ],
        )
        text = response.text.strip() if response.text else "(empty transcription)"
    except Exception as e:
        log.exception("Gemini transcription failed")
        await msg.reply_text(_user_error("Transcription failed", e))
        return

    reply_target = msg.reply_to_message

    # A voice note replying to an image we produced is a spoken edit instruction, the same
    # way a voice note replying to a transcript is a spoken reply instruction.
    image_target = (
        image_ui.instruction_target(msg.chat_id, reply_target.message_id)
        if reply_target else None
    )
    if image_target is not None:
        # Echo what was heard first: an edit that acts on a misheard instruction is otherwise
        # impossible to tell apart from a bad edit.
        await _send_chunks(context.bot, msg.chat_id, text, reply_to_message_id=msg.message_id)
        await image_ui.run_edit(update, context, image_target, "image/jpeg", text, msg.message_id)
        return

    # If this voice note is a reply to a transcript we know about, treat it as spoken
    # instructions for how to reply to that original message, rather than a standalone note.
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
            draft = _user_error("Drafting that reply failed", e)

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
        result = _user_error("That request failed", e)

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
        text = _user_error("That request failed", e)

    await _send_chunks(context.bot, msg.chat_id, text)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    CHATS.pop(update.effective_message.chat_id, None)
    await update.effective_message.reply_text("Conversation history cleared.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backstop for anything a handler did not catch.

    Without one, python-telegram-bot only logs the exception and the user is left with a
    typing indicator that never resolves. Tell them something went wrong, without echoing
    the exception, which may carry request material.
    """
    log.exception("Unhandled error while processing an update", exc_info=context.error)

    msg = getattr(update, "effective_message", None)
    user = getattr(update, "effective_user", None)
    # Same silence rule as every other handler: strangers get no reply at all.
    if msg is None or user is None or not settings.is_allowed(user.id):
        return
    try:
        await msg.reply_text(
            "Something went wrong handling that — it's been logged. Try again, or /help."
        )
    except Exception:
        # The failure may be that we cannot talk to Telegram at all; never recurse.
        log.exception("Could not deliver the error notice")


def main() -> None:
    settings.load()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(help_ui.post_init).build()
    settings_ui.register(app)
    settings_ui.set_key_change_hook(CHATS.clear)
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^(summarize|reply):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    input_flow.register(app)
    image_ui.register(app)
    style_ui.register(app)
    help_ui.register(app)
    app.add_error_handler(handle_error)
    log.info("Bot started, polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
