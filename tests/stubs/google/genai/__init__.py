"""Offline stub of google.genai, enough to exercise settings.py without network or deps."""

VALID_KEYS = {"good-key-aaaa1111", "good-key-bbbb2222"}
CALLS = []


class _Model:
    def __init__(self, name, actions=("generateContent",)):
        self.name = name
        # google-genai 2.x exposes only this attribute — deliberately no
        # supported_generation_methods here, to catch code written against the old shape.
        self.supported_actions = list(actions)


class _Models:
    def __init__(self, client):
        self._client = client

    def list(self):
        CALLS.append(("list", self._client.api_key))
        if self._client.api_key not in VALID_KEYS:
            from google.genai import errors
            raise errors.APIError(401, "unauthorized: key REDACTME leaked here")
        return [
            _Model("models/gemini-flash-latest"),
            _Model("models/gemini-flash-lite-latest"),
            _Model("models/gemini-3.5-flash"),
            _Model("models/gemini-3.1-flash-lite"),
            _Model("models/text-embedding-004", actions=("embedContent",)),
        ]

    def generate_content(self, model=None, contents=None):
        # Keep the prompt text so tests can assert what was actually sent to Gemini.
        flat = " ".join(c for c in (contents or []) if isinstance(c, str))
        CALLS.append(("generate", model, self._client.api_key, flat))

        class R:
            text = "stub response"
        return R()


class _Chats:
    def __init__(self, client):
        self._client = client

    def create(self, model=None, history=None, config=None):
        sysinstr = getattr(config, "system_instruction", None) if config else None
        CALLS.append(("chat_create", model, self._client.api_key, sysinstr,
                      len(history) if history else 0))

        class C:
            def __init__(self):
                self.model = model
                self._history = list(history or [])

            def send_message(self, text):
                class R:
                    text = "stub chat response"
                return R()

            def get_history(self):
                return self._history
        return C()


class Client:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.models = _Models(self)
        self.chats = _Chats(self)
