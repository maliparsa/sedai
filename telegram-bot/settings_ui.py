"""
Sedai in-bot settings UI: menus for configuring models, API keys, users, and viewing status.
Registers /settings, /setkey, and settings-related callback handlers.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import settings

log = logging.getLogger("sedai-bot")

_on_key_change = None


def _truncate_for_display(text: str, max_len: int = 80) -> str:
    """Truncate text to max_len chars with ellipsis if needed."""
    if text and len(text) > max_len:
        return text[:max_len] + "…"
    return text


def set_key_change_hook(fn):
    global _on_key_change
    _on_key_change = fn


async def _handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.is_allowed(update.effective_user.id):
        return
    await _show_settings_menu(update, context)


async def _show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    is_admin = settings.is_admin(user_id)

    buttons = []

    # "My models" — available to all allowed users
    buttons.append([InlineKeyboardButton("My models", callback_data="set:my_models")])

    # "My instructions" — available to all allowed users
    buttons.append([InlineKeyboardButton("My instructions", callback_data="set:my_instructions")])

    # Admin-only options
    if is_admin:
        buttons.append([InlineKeyboardButton("Default models", callback_data="set:default_models")])
        buttons.append([InlineKeyboardButton("Users", callback_data="set:users")])
        buttons.append([InlineKeyboardButton("API key", callback_data="set:api_key")])
        buttons.append([InlineKeyboardButton("Status", callback_data="set:status")])

    markup = InlineKeyboardMarkup(buttons)
    msg = update.effective_message
    await msg.reply_text("Settings", reply_markup=markup)


async def _handle_my_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    buttons = [
        [InlineKeyboardButton("Audio model", callback_data="set:my_audio")],
        [InlineKeyboardButton("Text model", callback_data="set:my_text")],
        [InlineKeyboardButton("Back", callback_data="set:back")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("My models", reply_markup=markup)


async def _handle_my_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    # Get all three standing instruction styles
    styles = settings.user_styles(user_id)
    reply_style = styles.get("reply")
    chat_style = styles.get("chat")
    summary_style = styles.get("summary")

    # Build the display text with truncated values
    display_reply = _truncate_for_display(reply_style) if reply_style else "Not set."
    display_chat = _truncate_for_display(chat_style) if chat_style else "Not set."
    display_summary = _truncate_for_display(summary_style) if summary_style else "Not set."

    text = (
        f"Reply: {display_reply}\n\n"
        f"Chat: {display_chat}\n\n"
        f"Summary: {display_summary}\n\n"
        "To set or change instructions:\n"
        "/replystyle <text>\n"
        "/chatstyle <text>\n"
        "/summarystyle <text>\n\n"
        "Or /replystyle clear (and same for others) to clear."
    )

    buttons = []

    # Add clear buttons for each kind that is set
    if reply_style:
        buttons.append([InlineKeyboardButton("Clear reply", callback_data="set:clear_reply")])
    if chat_style:
        buttons.append([InlineKeyboardButton("Clear chat", callback_data="set:clear_chat")])
    if summary_style:
        buttons.append([InlineKeyboardButton("Clear summary", callback_data="set:clear_summary")])

    buttons.append([InlineKeyboardButton("Back", callback_data="set:back")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_clear_style(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    # Clear the style for this user and kind
    settings.set_user_style(user_id, kind, None)
    await query.answer(f"{kind.capitalize()} instruction cleared.", show_alert=False)

    # Re-show the instructions menu
    await _handle_my_instructions(update, context)


async def _handle_default_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    await query.answer()

    buttons = [
        [InlineKeyboardButton("Audio model", callback_data="set:default_audio")],
        [InlineKeyboardButton("Text model", callback_data="set:default_text")],
        [InlineKeyboardButton("Back", callback_data="set:back")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("Default models", reply_markup=markup)


async def _handle_model_list(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, is_default: bool, is_admin_check: bool) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    # Re-check authorization
    if is_admin_check and not settings.is_admin(user_id):
        await query.answer()
        return

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    # Pagination: max 8 models per page
    page = int(context.user_data.get(f"model_page_{kind}_{is_default}", 0))
    available = settings.available_models()
    models_per_page = 8

    if not available:
        await query.edit_message_text("No models available.")
        return

    # Add "Use default" option at the beginning
    display_models = ["(Use default)"] + available
    start_idx = page * models_per_page
    end_idx = start_idx + models_per_page
    page_models = display_models[start_idx:end_idx]

    # Determine which model is currently effective
    if is_default:
        current_chain = settings.default_models(kind)
        current = current_chain[0] if current_chain else None
    else:
        current = settings.get_user_model(user_id, kind)
        if current is None:
            # Use default if no user preference
            current_chain = settings.default_models(kind)
            current = current_chain[0] if current_chain else None

    buttons = []
    for i, model_name in enumerate(page_models):
        idx = start_idx + i

        if model_name == "(Use default)":
            is_current = False  # "(Use default)" is never marked as current, only actual models are
            callback = f"set:clear_{kind}_{is_default}"
        else:
            is_current = (model_name == current)
            callback = f"set:choose_{kind}_{is_default}:{idx - 1}"  # idx - 1 to account for "Use default"

        prefix = "• " if is_current else ""
        label = prefix + model_name
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("Prev", callback_data=f"set:page_{kind}_{is_default}:{page - 1}"))
    if end_idx < len(display_models):
        nav_buttons.append(InlineKeyboardButton("Next", callback_data=f"set:page_{kind}_{is_default}:{page + 1}"))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton("Back", callback_data="set:back")])

    markup = InlineKeyboardMarkup(buttons)
    model_type = "Audio" if kind == "audio" else "Text"
    mode = "Default" if is_default else "My"
    await query.edit_message_text(f"{mode} {model_type} model", reply_markup=markup)


async def _handle_my_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_model_list(update, context, kind="audio", is_default=False, is_admin_check=False)


async def _handle_my_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_model_list(update, context, kind="text", is_default=False, is_admin_check=False)


async def _handle_default_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_model_list(update, context, kind="audio", is_default=True, is_admin_check=True)


async def _handle_default_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_model_list(update, context, kind="text", is_default=True, is_admin_check=True)


async def _handle_choose_model(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, is_default: bool) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if is_default and not settings.is_admin(user_id):
        await query.answer()
        return

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    # Extract index from callback_data
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer("Invalid callback.", show_alert=True)
        return

    try:
        idx = int(parts[2])
    except (ValueError, IndexError):
        await query.answer("Invalid callback.", show_alert=True)
        return

    available = settings.available_models()
    if idx < 0 or idx >= len(available):
        await query.answer("This menu expired — send /settings again.", show_alert=True)
        return

    model = available[idx]

    if is_default:
        settings.set_default_model(kind, model)
    else:
        settings.set_user_model(user_id, kind, model)

    await query.answer(f"{model} set.", show_alert=False)

    # Re-show the model list
    context.user_data[f"model_page_{kind}_{is_default}"] = 0
    await _handle_model_list(update, context, kind=kind, is_default=is_default, is_admin_check=is_default)


async def _handle_clear_model(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, is_default: bool) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if is_default and not settings.is_admin(user_id):
        await query.answer()
        return

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    if is_default:
        # Cannot clear default; this shouldn't be shown for admin
        await query.answer("Cannot clear default model.", show_alert=True)
    else:
        settings.set_user_model(user_id, kind, None)
        await query.answer("Preference cleared; using default.", show_alert=False)

    context.user_data[f"model_page_{kind}_{is_default}"] = 0
    await _handle_model_list(update, context, kind=kind, is_default=is_default, is_admin_check=is_default)


async def _handle_paginate_models(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, is_default: bool) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if is_default and not settings.is_admin(user_id):
        await query.answer()
        return

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer()
        return

    try:
        page = int(parts[2])
    except (ValueError, IndexError):
        await query.answer()
        return

    context.user_data[f"model_page_{kind}_{is_default}"] = page
    await query.answer()
    await _handle_model_list(update, context, kind=kind, is_default=is_default, is_admin_check=is_default)


async def _handle_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    await query.answer()

    allowed = settings.allowed_user_ids()
    admin_id = settings.admin_id()

    text = "Allowed users:\n\n"
    buttons = []

    for uid in allowed:
        label = f"{uid} (admin)" if uid == admin_id else str(uid)
        buttons.append([InlineKeyboardButton(label, callback_data="set:noop")])

        # Add remove button for non-admin users
        if uid != admin_id:
            buttons.append([InlineKeyboardButton(f"Remove {uid}", callback_data=f"set:remove_user:{uid}")])

    text += "\n".join([f"{uid} {'(admin)' if uid == admin_id else ''}" for uid in allowed])
    text += "\n\nTo add a user: /adduser <id>"

    buttons.append([InlineKeyboardButton("Back", callback_data="set:back")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer()
        return

    try:
        target_id = int(parts[2])
    except (ValueError, IndexError):
        await query.answer()
        return

    # Prevent removing the admin
    if target_id == settings.admin_id():
        await query.answer("Cannot remove the admin.", show_alert=True)
        return

    # Prevent emptying the list
    if len(settings.allowed_user_ids()) <= 1:
        await query.answer("Cannot remove the last user.", show_alert=True)
        return

    try:
        settings.remove_user(target_id)
        await query.answer(f"User {target_id} removed.", show_alert=False)
    except ValueError:
        await query.answer(f"User {target_id} not found.", show_alert=True)
        return

    # Re-show the users menu
    await _handle_users(update, context)


async def _handle_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    await query.answer()

    text = (
        "To change the API key, send:\n\n"
        "/setkey <new_key>\n\n"
        "The message containing the key will be automatically deleted. "
        "Do this in a private chat with the bot."
    )
    buttons = [
        [InlineKeyboardButton("Back", callback_data="set:back")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    await query.answer()

    snapshot = settings.snapshot()
    audio_chain = "\n".join(snapshot.get("default_audio_models", []))
    text_chain = "\n".join(snapshot.get("default_text_models", []))
    key_fingerprint = snapshot.get("api_key_fingerprint", "unknown")
    num_users = snapshot.get("allowed_user_count", 0)
    settings_path = snapshot.get("settings_path", "unknown")

    text = (
        f"Default audio chain:\n{audio_chain}\n\n"
        f"Default text chain:\n{text_chain}\n\n"
        f"API key: {key_fingerprint}\n"
        f"Allowed users: {num_users}\n"
        f"Settings file: {settings_path}"
    )

    buttons = [
        [InlineKeyboardButton("Back", callback_data="set:back")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()
    await _show_settings_menu(update, context)


async def _handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    data = query.data or ""

    if data == "set:my_models":
        await _handle_my_models(update, context)
    elif data == "set:my_instructions":
        await _handle_my_instructions(update, context)
    elif data == "set:default_models":
        await _handle_default_models(update, context)
    elif data == "set:my_audio":
        await _handle_my_audio(update, context)
    elif data == "set:my_text":
        await _handle_my_text(update, context)
    elif data == "set:default_audio":
        await _handle_default_audio(update, context)
    elif data == "set:default_text":
        await _handle_default_text(update, context)
    elif data.startswith("set:choose_audio_False:"):
        await _handle_choose_model(update, context, kind="audio", is_default=False)
    elif data.startswith("set:choose_text_False:"):
        await _handle_choose_model(update, context, kind="text", is_default=False)
    elif data.startswith("set:choose_audio_True:"):
        await _handle_choose_model(update, context, kind="audio", is_default=True)
    elif data.startswith("set:choose_text_True:"):
        await _handle_choose_model(update, context, kind="text", is_default=True)
    elif data.startswith("set:clear_audio"):
        await _handle_clear_model(update, context, kind="audio", is_default="True" in data)
    elif data.startswith("set:clear_text"):
        await _handle_clear_model(update, context, kind="text", is_default="True" in data)
    elif data.startswith("set:page_audio"):
        is_default = "True" in data
        await _handle_paginate_models(update, context, kind="audio", is_default=is_default)
    elif data.startswith("set:page_text"):
        is_default = "True" in data
        await _handle_paginate_models(update, context, kind="text", is_default=is_default)
    elif data == "set:users":
        await _handle_users(update, context)
    elif data.startswith("set:remove_user:"):
        await _handle_remove_user(update, context)
    elif data == "set:api_key":
        await _handle_api_key(update, context)
    elif data == "set:status":
        await _handle_status(update, context)
    elif data == "set:clear_reply":
        await _handle_clear_style(update, context, kind="reply")
    elif data == "set:clear_chat":
        await _handle_clear_style(update, context, kind="chat")
    elif data == "set:clear_summary":
        await _handle_clear_style(update, context, kind="summary")
    elif data == "set:back":
        await _handle_back(update, context)
    elif data == "set:noop":
        await _handle_noop(update, context)
    else:
        await query.answer()


async def _handle_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.effective_message

    # Only admin, private chat only
    if not settings.is_admin(user_id):
        await msg.reply_text("This command is for the admin only.")
        return

    if msg.chat_id != user_id:
        await msg.reply_text("Use /setkey in a private chat with the bot.")
        return

    # Check if argument is provided
    if not context.args or not context.args[0]:
        await msg.reply_text("Usage: /setkey <new_key>")
        return

    new_key = context.args[0]

    # Delete the message containing the key
    deletion_failed = False
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception as e:
        log.warning("Failed to delete message with key: %s", e)
        await msg.reply_text("Failed to delete your message automatically. Please delete it manually.")
        deletion_failed = True

    # Validate the key (continue even if deletion failed)
    try:
        settings.set_api_key(new_key)
    except ValueError:
        await msg.reply_text("That key was rejected by the Gemini API.")
        return

    # On success, show fingerprint and clear chat sessions
    fingerprint = settings.api_key_fingerprint()
    await msg.reply_text(
        f"API key updated ({fingerprint}). Chat history cleared. "
        f"Revoke the old key if it is no longer needed."
    )

    # Call the key change hook to clear chat sessions
    if _on_key_change:
        _on_key_change()


async def _handle_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await update.effective_message.reply_text("This command is for the admin only.")
        return

    if not context.args or not context.args[0]:
        await update.effective_message.reply_text("Usage: /settings adduser <user_id>")
        return

    try:
        new_user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid user ID.")
        return

    if settings.add_user(new_user_id):
        await update.effective_message.reply_text(f"User {new_user_id} added.")
    else:
        await update.effective_message.reply_text(f"User {new_user_id} already in the list.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("settings", _handle_settings))
    app.add_handler(CommandHandler("setkey", _handle_setkey))
    app.add_handler(CommandHandler("adduser", _handle_adduser, filters=None))
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern=r"^set:"))
