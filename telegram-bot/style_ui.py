"""
Sedai standing instructions UI: per-user /replystyle, /chatstyle, /summarystyle commands.
"""

import logging
import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import settings

log = logging.getLogger("sedai-bot")


async def _handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """Handle standing instruction commands (reply, chat, summary)."""
    user = update.effective_user
    if not user or not settings.is_allowed(user.id):
        return

    msg = update.effective_message

    # Take everything after the first token, from the raw text: context.args collapses
    # runs of whitespace, and the command token can carry a @botname suffix that must
    # not end up stored as part of the instruction. DOTALL so multi-line text survives.
    match = re.match(r"^\S+\s*(.*)$", msg.text or "", flags=re.S)
    text_after = match.group(1) if match else ""

    if not text_after:
        # Show current instruction.
        current = settings.get_user_style(user.id, kind)
        display = current if current else "Not set."
        await msg.reply_text(
            f"Standing {kind} instruction: {display}\n\n"
            f"Set with: /{kind}style <text>\n"
            f"Clear with: /{kind}style clear"
        )
        return

    if text_after.lower() == "clear":
        # Clear the instruction.
        settings.set_user_style(user.id, kind, None)
        await msg.reply_text(f"Standing {kind} instruction cleared.")
    else:
        # Set the instruction.
        try:
            settings.set_user_style(user.id, kind, text_after)
            await msg.reply_text(f"Standing {kind} instruction set:\n\n{text_after}")
        except ValueError:
            await msg.reply_text(
                f"That's too long. Standing instructions are capped at "
                f"{settings.STYLE_MAX_LEN} characters."
            )


async def _handle_replystyle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_style(update, context, "reply")


async def _handle_chatstyle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_style(update, context, "chat")


async def _handle_summarystyle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_style(update, context, "summary")


def register(app: Application) -> None:
    """Register the three standing instruction commands."""
    app.add_handler(CommandHandler("replystyle", _handle_replystyle))
    app.add_handler(CommandHandler("chatstyle", _handle_chatstyle))
    app.add_handler(CommandHandler("summarystyle", _handle_summarystyle))
