"""Offline stub of the python-telegram-bot public surface used by this project."""

ACTIONS = []  # every outbound bot action, in order, for assertions
PARSE_MODES = []  # parse_mode passed to each reply_text, for assertions


class CopyTextButton:
    def __init__(self, text=None):
        self.text = text


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, copy_text=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.copy_text = copy_text
        self.url = url


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard

    def buttons(self):
        return [b for row in self.inline_keyboard for b in row]


class ForceReply:
    def __init__(self, selective=False):
        self.selective = selective


class BotCommand:
    def __init__(self, command, description):
        self.command = command
        self.description = description


class BotCommandScopeDefault:
    kind = "default"


class BotCommandScopeAllPrivateChats:
    kind = "all_private_chats"


class BotCommandScopeChat:
    kind = "chat"

    def __init__(self, chat_id=None):
        self.chat_id = chat_id


class User:
    def __init__(self, user_id):
        self.id = user_id
        self.first_name = f"user{user_id}"


class Chat:
    def __init__(self, chat_id, chat_type="private"):
        self.id = chat_id
        self.type = chat_type

    async def send_action(self, action):
        ACTIONS.append(("send_action", action))


class Bot:
    # Set by tests to make setMyCommands fail, proving startup survives it.
    fail_set_my_commands = False

    async def set_my_commands(self, commands, scope=None, language_code=None):
        if Bot.fail_set_my_commands:
            raise RuntimeError("telegram is down: token 123:tg-token-secret in url")
        ACTIONS.append(("set_my_commands", [c.command for c in commands], scope))
        return True

    async def send_message(self, chat_id=None, text=None, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        ACTIONS.append(("send_message", chat_id, text, reply_markup))
        return Message(message_id=9999, chat_id=chat_id, text=text)

    async def delete_message(self, chat_id=None, message_id=None):
        ACTIONS.append(("delete_message", chat_id, message_id))
        return True

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, reply_markup=None):
        ACTIONS.append(("edit_message_text", chat_id, text, reply_markup))
        return True


class Message:
    def __init__(self, message_id=1, chat_id=100, text=None, user_id=None):
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = text
        self.chat = Chat(chat_id)
        self.voice = None
        self.audio = None
        self.reply_to_message = None
        self.from_user = User(user_id) if user_id else None

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        PARSE_MODES.append(parse_mode)
        ACTIONS.append(("reply_text", self.chat_id, text, reply_markup))
        return Message(message_id=self.message_id + 1, chat_id=self.chat_id, text=text)

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        ACTIONS.append(("edit_text", self.chat_id, text, reply_markup))
        return self

    async def edit_reply_markup(self, reply_markup=None):
        ACTIONS.append(("edit_reply_markup", self.chat_id, reply_markup))
        return self

    async def delete(self):
        ACTIONS.append(("delete", self.chat_id, self.message_id))
        return True


class CallbackQuery:
    def __init__(self, data=None, user_id=None, chat_id=100, message_id=5):
        self.data = data
        self.from_user = User(user_id)
        self.message = Message(message_id=message_id, chat_id=chat_id)

    async def answer(self, text=None, show_alert=False):
        ACTIONS.append(("answer", text, show_alert))
        return True

    async def edit_message_text(self, text=None, reply_markup=None, parse_mode=None):
        ACTIONS.append(("edit_message_text", self.message.chat_id, text, reply_markup))
        return True

    async def edit_message_reply_markup(self, reply_markup=None):
        ACTIONS.append(("edit_message_reply_markup", self.message.chat_id, reply_markup))
        return True


class Update:
    ALL_TYPES = "all"

    def __init__(self, message=None, callback_query=None, user_id=None):
        self.message = message
        self.callback_query = callback_query
        self._user_id = user_id

    @property
    def effective_user(self):
        if self.callback_query is not None:
            return self.callback_query.from_user
        return User(self._user_id) if self._user_id is not None else None

    @property
    def effective_message(self):
        if self.message is not None:
            return self.message
        if self.callback_query is not None:
            return self.callback_query.message
        return None

    @property
    def effective_chat(self):
        m = self.effective_message
        return m.chat if m else None
