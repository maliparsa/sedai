"""Offline stub of telegram.ext, enough to register and drive handlers in tests."""

import re


class ContextTypes:
    DEFAULT_TYPE = object


class _Ctx:
    """Stand-in for the context object handlers receive."""

    def __init__(self, args=None, bot=None):
        self.args = args or []
        from telegram import Bot
        self.bot = bot or Bot()
        self.user_data = {}
        self.chat_data = {}


class BaseHandler:
    def __init__(self, callback):
        self.callback = callback


class CommandHandler(BaseHandler):
    def __init__(self, command, callback, **kwargs):
        super().__init__(callback)
        self.command = command


class CallbackQueryHandler(BaseHandler):
    def __init__(self, callback, pattern=None, **kwargs):
        super().__init__(callback)
        self.pattern = pattern

    def matches(self, data):
        return self.pattern is None or re.match(self.pattern, data or "") is not None


class MessageHandler(BaseHandler):
    def __init__(self, filters, callback, **kwargs):
        super().__init__(callback)
        self.filters = filters


class _Filter:
    def __init__(self, name):
        self.name = name

    def __and__(self, other):
        return _Filter(f"{self.name}&{other.name}")

    def __or__(self, other):
        return _Filter(f"{self.name}|{other.name}")

    def __invert__(self):
        return _Filter(f"~{self.name}")


class _Filters:
    TEXT = _Filter("TEXT")
    COMMAND = _Filter("COMMAND")
    REPLY = _Filter("REPLY")
    VOICE = _Filter("VOICE")
    AUDIO = _Filter("AUDIO")
    ChatType = type("ChatType", (), {"PRIVATE": _Filter("PRIVATE")})


filters = _Filters()


class Application:
    def __init__(self):
        self.handlers = []
        self.handler_groups = {}
        self.post_init = None
        from telegram import Bot
        self.bot = Bot()

    def add_handler(self, handler, group=0):
        self.handlers.append(handler)
        # Track the group: real PTB runs lower groups first, and the reply-input handler
        # depends on sitting in group -1 ahead of the chat handler in group 0.
        self.handler_groups.setdefault(group, []).append(handler)

    def run_polling(self, **kwargs):
        raise RuntimeError("run_polling must not be called in tests")

    # --- test drivers ---
    def command(self, name):
        for h in self.handlers:
            if isinstance(h, CommandHandler) and h.command == name:
                return h.callback
        return None

    def callback_for(self, data):
        for h in self.handlers:
            if isinstance(h, CallbackQueryHandler) and h.matches(data):
                return h.callback
        return None

    def callback_patterns(self):
        return [h.pattern for h in self.handlers if isinstance(h, CallbackQueryHandler)]

    @classmethod
    def builder(cls):
        class _B:
            def __init__(self):
                self._post_init = None

            def token(self, *a, **k):
                return self

            def post_init(self, fn):
                self._post_init = fn
                return self

            def build(self):
                app = cls()
                app.post_init = self._post_init
                return app
        return _B()


class ApplicationHandlerStop(Exception):
    """Raised by a handler to prevent further handlers from being called."""
    pass
