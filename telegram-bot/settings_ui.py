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
import input_flow

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

    # "Transcript timestamps" — available to all allowed users
    buttons.append([InlineKeyboardButton("Transcript timestamps",
                                         callback_data="set:timestamps")])

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

    styles = settings.user_styles(user_id)

    lines = []
    buttons = []
    for kind in settings.STYLE_KINDS:
        value = styles.get(kind)
        display = _truncate_for_display(value) if value else "Not set."
        lines.append(f"{kind.capitalize()}: {display}")
        # Name the kind on the button: with one row per kind, bare "View" buttons are
        # told apart only by position, which is guesswork on a narrow screen.
        row = [InlineKeyboardButton(f"View {kind}", callback_data=f"set:instr:{kind}")]
        if value:
            row.append(InlineKeyboardButton("Clear", callback_data=f"set:clear_{kind}"))
        buttons.append(row)

    text = "\n".join(lines)
    buttons.append([InlineKeyboardButton("Back", callback_data="set:back")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


def _format_threshold(seconds: int) -> str:
    if seconds <= 0:
        return "Off"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hr"
    return f"{seconds // 60} min"


async def _handle_timestamps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Choose the recording length above which transcripts get [MM:SS] cues."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    current = settings.timestamp_threshold(user_id)
    if current <= 0:
        state = "Off — transcripts are never timestamped."
    else:
        state = f"On for recordings longer than {_format_threshold(current)}."

    text = (
        "Automatic transcript timestamps\n\n"
        f"{state}\n\n"
        "Long recordings are transcribed as [MM:SS] caption cues so you can find your "
        "place. Shorter ones are left as plain text. Your own /transcriptstyle "
        "instruction, if you have set one, takes precedence over this."
    )

    buttons = []
    row = []
    for choice in settings.TIMESTAMP_THRESHOLD_CHOICES:
        label = _format_threshold(choice)
        if choice == current:
            label = f"• {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"set:ts:{choice}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Back", callback_data="set:back")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_set_timestamps(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 seconds: int) -> None:
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    settings.set_timestamp_threshold(user_id, seconds)
    await query.answer(
        "Timestamps off." if seconds <= 0
        else f"Timestamps on above {_format_threshold(seconds)}.",
        show_alert=False,
    )
    await _handle_timestamps(update, context)


async def _handle_instr_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """Show full instruction with Set/Clear/Back buttons."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    styles = settings.user_styles(user_id)
    value = styles.get(kind)

    # Defensive truncation at ~3500 to stay well under Telegram's 4096-char message limit
    if value and len(value) > 3500:
        display_value = value[:3500] + "…"
    else:
        display_value = value if value else "Not set."

    text = f"{kind.capitalize()} instruction:\n\n{display_value}"

    buttons = [
        [InlineKeyboardButton("Set", callback_data=f"set:instr_set_{kind}")],
    ]

    if value:
        buttons.append([InlineKeyboardButton("Clear", callback_data=f"set:instr_clear_{kind}")])

    buttons.append([InlineKeyboardButton("Back", callback_data="set:my_instructions")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_instr_set(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """Initiate reply-based input for setting an instruction."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    prompt = f"Enter your {kind} instruction (or reply 'clear' to remove it)."
    await input_flow.request(context.bot, update.effective_chat.id, user_id, f"style:{kind}", prompt)


async def _handle_instr_clear(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """Clear an instruction and return to detail view."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_allowed(user_id):
        await query.answer()
        return

    await query.answer()

    settings.set_user_style(user_id, kind, None)
    await query.answer(f"{kind.capitalize()} instruction cleared.", show_alert=False)

    # Re-show the detail screen
    await _handle_instr_detail(update, context, kind)


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

    buttons.append([InlineKeyboardButton("Add user", callback_data="set:add_user")])
    buttons.append([InlineKeyboardButton("Back", callback_data="set:back")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_add_user_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate reply-based input for adding a user."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    await query.answer()

    prompt = "Enter the Telegram user ID to add."
    await input_flow.request(context.bot, update.effective_chat.id, user_id, "adduser", prompt)


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

    text = "To change the API key, use the button below."
    buttons = [
        [InlineKeyboardButton("Set key", callback_data="set:set_key")],
        [InlineKeyboardButton("Back", callback_data="set:back")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_set_key_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate reply-based input for setting the API key."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not settings.is_admin(user_id):
        await query.answer()
        return

    await query.answer()

    prompt = "Reply with your new API key. Your reply will be deleted immediately."
    await input_flow.request(context.bot, update.effective_chat.id, user_id, "setkey", prompt)


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
    elif data == "set:timestamps":
        await _handle_timestamps(update, context)
    elif data.startswith("set:ts:"):
        raw = data[len("set:ts:"):]
        # Only honour values the menu actually offers, never an arbitrary callback payload.
        if raw.isdigit() and int(raw) in settings.TIMESTAMP_THRESHOLD_CHOICES:
            await _handle_set_timestamps(update, context, int(raw))
        else:
            await query.answer()
    elif data == "set:my_instructions":
        await _handle_my_instructions(update, context)
    elif data.startswith("set:instr:") and data[len("set:instr:"):] in settings.STYLE_KINDS:
        await _handle_instr_detail(update, context, kind=data[len("set:instr:"):])
    elif data.startswith("set:instr_set_") and data[len("set:instr_set_"):] in settings.STYLE_KINDS:
        await _handle_instr_set(update, context, kind=data[len("set:instr_set_"):])
    elif data.startswith("set:instr_clear_") and data[len("set:instr_clear_"):] in settings.STYLE_KINDS:
        await _handle_instr_clear(update, context, kind=data[len("set:instr_clear_"):])
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
    elif data == "set:add_user":
        await _handle_add_user_prompt(update, context)
    elif data.startswith("set:remove_user:"):
        await _handle_remove_user(update, context)
    elif data == "set:api_key":
        await _handle_api_key(update, context)
    elif data == "set:set_key":
        await _handle_set_key_prompt(update, context)
    elif data == "set:status":
        await _handle_status(update, context)
    elif data.startswith("set:clear_") and data[len("set:clear_"):] in settings.STYLE_KINDS:
        await _handle_clear_style(update, context, kind=data[len("set:clear_"):])
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
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception as e:
        # Log the type only: an API error body can echo back request material.
        log.warning("Failed to delete message with key (%s)", type(e).__name__)
        await msg.reply_text("Failed to delete your message automatically. Please delete it manually.")

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


@input_flow.on("adduser")
async def _on_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, meta) -> None:
    """Consumer for reply-based adduser flow."""
    user_id = update.effective_user.id

    # Re-check authorization
    if not settings.is_admin(user_id):
        return

    # Parse user ID
    try:
        new_user_id = int(text.strip())
    except ValueError:
        await update.effective_message.reply_text("Please enter a valid numeric user ID.")
        return

    # Add the user
    if settings.add_user(new_user_id):
        await update.effective_message.reply_text(f"User {new_user_id} added.")
    else:
        await update.effective_message.reply_text(f"User {new_user_id} is already in the list.")


@input_flow.on("setkey")
async def _on_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, meta) -> None:
    """Consumer for reply-based setkey flow."""
    user_id = update.effective_user.id

    # Re-check authorization
    if not settings.is_admin(user_id):
        return

    msg = update.effective_message

    # Delete the message containing the key FIRST
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception as e:
        # Log the type only: an API error body can echo back request material.
        log.warning("Failed to delete message with key (%s)", type(e).__name__)
        await msg.reply_text("Failed to delete your message automatically. Please delete it manually.")

    # Validate the key (continue even if deletion failed)
    try:
        settings.set_api_key(text)
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


def register(app: Application) -> None:
    app.add_handler(CommandHandler("settings", _handle_settings))
    app.add_handler(CommandHandler("setkey", _handle_setkey))
    app.add_handler(CommandHandler("adduser", _handle_adduser, filters=None))
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern=r"^set:"))
