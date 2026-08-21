"""
Sedai help UI: /help, /start, command discovery, and unknown-command fallback.
"""

import logging

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import settings

log = logging.getLogger("sedai-bot")


def _help_text_for_user(user_id: int) -> str:
    """Generate /help text, role-aware."""
    is_admin = settings.is_admin(user_id)

    text = (
        "Send a voice note or audio file to get a transcript, with Summarize and Draft reply buttons.\n\n"
        "Recordings longer than 10 minutes are transcribed as [MM:SS] caption cues so you\n"
        "can find your place. Change or switch that off under /settings.\n\n"
        "Reply to a transcript with a voice note to dictate instructions for a reply.\n\n"
        "Any text message is a chat with Gemini.\n\n"
        "Send a photo with a caption saying what to change, and you get the edited image\n"
        "back. No caption and I'll ask. Reply to a result — by text or by voice note — to\n"
        "refine it further, or tap Again for another take.\n\n"
        "Standing instructions shape how the bot writes for you:\n"
        "/replystyle — for draft replies\n"
        "/chatstyle — for chat\n"
        "/summarystyle — for summaries\n"
        "/imagestyle — for image edits\n"
        "/transcriptstyle — for transcripts, e.g. \"add [MM:SS] timestamps\"\n"
        "  (unlike the others this can change what a transcript says, not just how it\n"
        "  reads — asking to tidy or shorten it costs you a verbatim record)\n\n"
        "/settings — choose your audio, text and image models\n"
        "/reset — clear your chat history\n"
        "/help — this message"
    )

    if is_admin:
        text += (
            "\n\nAdmin:\n"
            "/setkey <key> — replace the Gemini API key\n"
            "/adduser <id> — allow another Telegram user\n"
            "/settings → Image budget — monthly cap on image spend"
        )

    return text


async def _handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not settings.is_allowed(user.id):
        return

    text = _help_text_for_user(user.id)
    await update.effective_message.reply_text(text)


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not settings.is_allowed(user.id):
        return

    text = _help_text_for_user(user.id)
    await update.effective_message.reply_text(text)


async def _handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not settings.is_allowed(user.id):
        return

    await update.effective_message.reply_text("Unknown command — try /help")


async def post_init(app: Application) -> None:
    """Populate Telegram's / command menu."""
    try:
        # Base commands for all users.
        base_commands = [
            BotCommand("settings", "Choose your audio, text and image models"),
            BotCommand("reset", "Clear your chat history"),
            BotCommand("replystyle", "Standing instructions for draft replies"),
            BotCommand("chatstyle", "Standing instructions for chat"),
            BotCommand("summarystyle", "Standing instructions for summaries"),
            BotCommand("imagestyle", "Standing instructions for image edits"),
            BotCommand("transcriptstyle", "Standing instructions for transcripts"),
            BotCommand("help", "Show this message"),
        ]

        # Set base scope for all private chats.
        await app.bot.set_my_commands(
            base_commands,
            scope=BotCommandScopeAllPrivateChats(),
        )

        # Admin commands for admin only.
        admin_id = settings.admin_id()
        admin_commands = base_commands + [
            BotCommand("setkey", "Replace the Gemini API key"),
            BotCommand("adduser", "Allow another Telegram user"),
        ]

        await app.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
    except Exception as e:
        log.warning("Failed to set command menu (%s)", type(e).__name__)


def register(app: Application) -> None:
    """Register /help, /start, and unknown-command handler."""
    # Help and start commands must come before the unknown-command fallback.
    app.add_handler(CommandHandler("help", _handle_help))
    app.add_handler(CommandHandler("start", _handle_start))

    # Unknown command fallback (must be after all CommandHandlers).
    app.add_handler(MessageHandler(filters.COMMAND, _handle_unknown_command))
