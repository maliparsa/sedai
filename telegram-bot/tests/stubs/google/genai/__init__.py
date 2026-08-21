"""Offline stub of google.genai, enough to exercise settings.py without network or deps."""

VALID_KEYS = {"good-key-aaaa1111", "good-key-bbbb2222"}
CALLS = []

# Test knobs for image generation. IMAGE_MODE picks what an image model returns:
#   "ok"        -> an image part, as a normal edit does
#   "text_only" -> no image, just words, as the model does when it declines in prose
#   "safety"    -> no image and a blocking finish_reason
# IMAGE_RAISE, when set to a status code, makes image models raise instead.
IMAGE_MODE = "ok"
IMAGE_RAISE = None
IMAGE_BYTES = b"\x89PNG-stub-image-bytes"
IMAGE_PROMPT_TOKENS = 275
IMAGE_OUTPUT_TOKENS = 1193


def reset_image_stub():
    global IMAGE_MODE, IMAGE_RAISE
    IMAGE_MODE, IMAGE_RAISE = "ok", None


def is_image_model(name):
    return "image" in (name or "") or "banana" in (name or "")


class _Usage:
    def __init__(self, prompt_tokens, output_tokens):
        self.prompt_token_count = prompt_tokens
        self.candidates_token_count = output_tokens


class _InlineData:
    def __init__(self, data, mime_type):
        self.data = data
        self.mime_type = mime_type


class _Part:
    def __init__(self, text=None, inline_data=None):
        self.text = text
        self.inline_data = inline_data


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _Candidate:
    def __init__(self, parts, finish_reason="STOP"):
        self.content = _Content(parts)
        self.finish_reason = finish_reason


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
            # Image models report plain generateContent, exactly as the real API does —
            # only the name marks them out.
            _Model("models/gemini-3.1-flash-image"),
            _Model("models/gemini-2.5-flash-image"),
            _Model("models/gemini-3-pro-image"),
            _Model("models/nano-banana-pro-preview"),
        ]

    def generate_content(self, model=None, contents=None, config=None):
        # Keep the prompt text so tests can assert what was actually sent to Gemini.
        flat = " ".join(c for c in (contents or []) if isinstance(c, str))
        CALLS.append(("generate", model, self._client.api_key, flat))

        if is_image_model(model):
            if IMAGE_RAISE is not None:
                from google.genai import errors
                raise errors.APIError(IMAGE_RAISE, "image model unavailable")

            if IMAGE_MODE == "safety":
                parts, finish = [], "IMAGE_SAFETY"
            elif IMAGE_MODE == "text_only":
                parts, finish = [_Part(text="I can't edit that one.")], "STOP"
            else:
                parts = [_Part(inline_data=_InlineData(IMAGE_BYTES, "image/jpeg"))]
                finish = "STOP"

            class IR:
                candidates = [_Candidate(parts, finish)]
                usage_metadata = _Usage(IMAGE_PROMPT_TOKENS, IMAGE_OUTPUT_TOKENS)
                text = None
            return IR()

        class R:
            text = "stub response"
            candidates = []
            usage_metadata = None
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
