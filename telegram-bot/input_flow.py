"""
Sedai reply-based input flow: prompts that collect input via Telegram replies.

When a menu action or no-argument command needs input (set an instruction, add a user,
change the API key), the bot sends a prompt with ForceReply markup. The user replies to
that specific message, and the reply is consumed as the input, dispatched to a registered
consumer. Replies that are not to a pending prompt fall through untouched to normal
chat handling.
"""

import logging
from collections import OrderedDict
from typing import Callable

from telegram import ForceReply, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    filters,
)

import settings

log = logging.getLogger("sedai-bot")

PROMPT_SUFFIX = "\n\nReply to this message with your input."

# (chat_id, prompt_message_id) -> (action, user_id, meta)
# Keyed by both chat_id and message_id: Telegram message IDs are only unique within a
# single chat, so keying by message_id alone could let one user's reply pull up another
# user's prompt if their chats landed on the same message number. This was a real
# privacy bug in this project before.
PENDING: "OrderedDict[tuple[int, int], tuple[str, int, object]]" = OrderedDict()
MAX_PENDING = 200

# action -> async consumer coroutine. Set by @on decorators.
_CONSUMERS: dict[str, Callable] = {}


def on(action: str):
    """
    Decorator registering the coroutine that consumes a reply for `action`.
    The consumer signature is: async def fn(update, context, text: str, meta) -> None
    """
    def decorator(fn: Callable) -> Callable:
        _CONSUMERS[action] = fn
        return fn
    return decorator


async def request(msg_or_bot, chat_id: int, user_id: int, action: str,
                  prompt: str, meta=None) -> None:
    """
    Send a prompt with ForceReply markup and record the pending request.

    Args:
        msg_or_bot: Telegram message object (has .reply_text) or bot object.
        chat_id: The chat to send the prompt to.
        user_id: The user_id who issued the request (for verification on reply).
        action: Action key for consumer dispatch.
        prompt: The prompt text to send. PROMPT_SUFFIX is appended automatically.
        meta: Optional metadata to pass to the consumer.
    """
    # Determine what object has the send_message capability
    if hasattr(msg_or_bot, 'reply_text'):
        # It's a message object; use its reply_text method
        sent = await msg_or_bot.reply_text(
            prompt + PROMPT_SUFFIX,
            reply_markup=ForceReply(selective=True),
        )
    else:
        # It's a bot object; use send_message directly
        sent = await msg_or_bot.send_message(
            chat_id=chat_id,
            text=prompt + PROMPT_SUFFIX,
            reply_markup=ForceReply(selective=True),
        )

    # Record the pending request
    PENDING[(chat_id, sent.message_id)] = (action, user_id, meta)

    # Evict old entries if the map grew too large
    while len(PENDING) > MAX_PENDING:
        PENDING.popitem(last=False)


async def _consume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle replies to pending prompts. Consume and dispatch to registered consumer
    if the reply matches a pending entry, otherwise fall through untouched.
    """
    user = update.effective_user

    # Return immediately if user is not allowed (do not consume or leak anything).
    if user is None or not settings.is_allowed(user.id):
        return

    msg = update.effective_message
    reply_to = msg.reply_to_message

    # Look up the pending entry. If not found, fall through to normal chat handling.
    key = (msg.chat_id, reply_to.message_id)
    pending_entry = PENDING.get(key)
    if pending_entry is None:
        return

    # Found a pending entry. Verify the user matches.
    action, expected_user_id, meta = pending_entry
    if user.id != expected_user_id:
        # User mismatch: silently return without consuming or leaking that an entry exists.
        return

    # Pop the entry BEFORE dispatching, so a consumer that raises cannot wedge it.
    del PENDING[key]

    # From here the message is ours: every exit path must stop propagation, or handle_text
    # would answer the same message a second time.
    try:
        consumer = _CONSUMERS.get(action)
        if consumer is None:
            await msg.reply_text("That request expired — try again.")
        else:
            await consumer(update, context, msg.text, meta)
    except ApplicationHandlerStop:
        raise
    except Exception:
        log.exception("Input consumer for %s failed", action)
        await msg.reply_text("Sorry, that didn't work — try again.")
    raise ApplicationHandlerStop()


def register(app: Application) -> None:
    """
    Register the reply-consuming handler in group -1 so it is seen before
    handle_text in group 0.
    """
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, _consume),
        group=-1,
    )
