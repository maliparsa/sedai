"""
Sedai settings: persistent global and per-user config, live-updatable without restart.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors

log = logging.getLogger("sedai-bot")

# Hardcoded defaults matching sedai_bot.py
_DEFAULT_AUDIO_MODELS = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.1-flash-lite"]
_DEFAULT_TEXT_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.1-flash-lite"]
# Image models have no free tier at all, so this chain is ordered by cost, cheapest first.
_DEFAULT_IMAGE_MODELS = ["gemini-3.1-flash-image", "gemini-2.5-flash-image", "gemini-3-pro-image"]

# Every kind of model the bot picks per user. Adding a kind is a one-tuple edit here plus
# a default chain above; every consumer iterates this rather than hardcoding names.
MODEL_KINDS = ("audio", "text", "image")


def _default_chain(kind: str) -> list[str]:
    return {
        "audio": _DEFAULT_AUDIO_MODELS,
        "text": _DEFAULT_TEXT_MODELS,
        "image": _DEFAULT_IMAGE_MODELS,
    }[kind].copy()


def _check_kind(kind: str) -> None:
    if kind not in MODEL_KINDS:
        raise ValueError(f"kind must be one of {MODEL_KINDS}, got {kind}")

# Global mutable state: the current in-memory config
_state = {
    "gemini_api_key": None,
    "gemini_client": None,
    "allowed_user_ids": [],
    "default_models": {k: _default_chain(k) for k in MODEL_KINDS},
    "users": {},  # user_id (as int key) -> {"audio_model": ..., "text_model": ...}
    "available_models_cache": {"data": None, "timestamp": 0},
    # Estimated spend on image generation, which unlike text and audio has no free tier.
    "image_budget_usd": None,   # None means "unconfigured", so the default applies; 0 means no cap
    "image_spend": {"month": "", "usd": 0.0, "count": 0, "users": {}},
}

_settings_path = None


def _get_settings_path():
    """Resolve settings file path: env var or default to settings.json next to this file."""
    global _settings_path
    if _settings_path is None:
        path = os.environ.get("SEDAI_SETTINGS_PATH")
        if path:
            _settings_path = path
        else:
            _settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    return _settings_path


def _load_from_file():
    """Load settings from disk, return dict or None if missing/corrupt."""
    path = _get_settings_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != 1:
            log.warning("settings.json has unknown version or is not a dict, ignoring")
            return None
        return data
    except (json.JSONDecodeError, IOError) as e:
        log.warning("Failed to load settings.json: %s, using env/defaults", e)
        return None


def _persist_settings():
    """Atomically write current state to settings.json with 0600 perms."""
    data = {
        "version": 1,
        "default_models": _state["default_models"],
        "gemini_api_key": _state["gemini_api_key"],
        "allowed_user_ids": _state["allowed_user_ids"],
        "users": _state["users"],
        "image_budget_usd": _state["image_budget_usd"],
        "image_spend": _state["image_spend"],
    }

    path = _get_settings_path()
    directory = os.path.dirname(path) or "."

    # Write to temp file in the SAME directory, set perms, then atomic replace.
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def load() -> None:
    """Load settings from file or environment, initialize gemini_client."""
    global _state

    # Try to load from disk first
    file_data = _load_from_file()

    if file_data:
        # Use file values where available
        _state["gemini_api_key"] = file_data.get("gemini_api_key") or _get_api_key_from_env()
        _state["allowed_user_ids"] = file_data.get("allowed_user_ids") or _get_allowed_user_ids_from_env()
        _state["default_models"] = file_data.get("default_models") or {}
        _state["users"] = file_data.get("users") or {}
        _state["image_budget_usd"] = file_data.get("image_budget_usd")
        _state["image_spend"] = file_data.get("image_spend") or {
            "month": "", "usd": 0.0, "count": 0, "users": {}
        }
    else:
        # Fall back to env/defaults
        _state["gemini_api_key"] = _get_api_key_from_env()
        _state["allowed_user_ids"] = _get_allowed_user_ids_from_env()
        _state["default_models"] = {}
        _state["users"] = {}
        _state["image_budget_usd"] = None
        _state["image_spend"] = {"month": "", "usd": 0.0, "count": 0, "users": {}}

    # A settings.json written before a kind existed simply lacks it; fill from defaults so
    # an upgrade never leaves a chain empty.
    for kind in MODEL_KINDS:
        if not _state["default_models"].get(kind):
            _state["default_models"][kind] = _default_chain(kind)

    # Normalize users keys to int
    normalized_users = {}
    for key, value in _state["users"].items():
        user_id = int(key) if isinstance(key, str) else key
        normalized_users[user_id] = value
    _state["users"] = normalized_users

    # Ensure admin exists
    if _state["allowed_user_ids"] and _state["allowed_user_ids"][0] not in _state["users"]:
        _state["users"][_state["allowed_user_ids"][0]] = {"audio_model": None, "text_model": None}

    # Create initial gemini client
    if _state["gemini_api_key"]:
        _state["gemini_client"] = genai.Client(api_key=_state["gemini_api_key"])
    else:
        raise ValueError("GEMINI_API_KEY not set in environment or settings.json")


def _get_api_key_from_env() -> str | None:
    """Get API key from GEMINI_API_KEY env var."""
    return os.environ.get("GEMINI_API_KEY")


def _get_allowed_user_ids_from_env() -> list[int]:
    """Get allowed user IDs from ALLOWED_USER_ID env var (comma-separated)."""
    env_val = os.environ.get("ALLOWED_USER_ID", "")
    ids = [int(x) for x in env_val.split(",") if x.strip()]
    return ids


def admin_id() -> int:
    """Return the admin user ID (index 0 of allowed_user_ids)."""
    if not _state["allowed_user_ids"]:
        raise ValueError("No admin configured")
    return _state["allowed_user_ids"][0]


def is_admin(user_id: int) -> bool:
    """Check if user_id is the admin."""
    return user_id == admin_id() if _state["allowed_user_ids"] else False


def is_allowed(user_id: int) -> bool:
    """Check if user_id is in the allowed list."""
    return user_id in _state["allowed_user_ids"]


def allowed_user_ids() -> list[int]:
    """Return ordered list of allowed user IDs (admin first)."""
    return list(_state["allowed_user_ids"])


def add_user(user_id: int) -> bool:
    """Add a user to allowed_user_ids. Return False if already present."""
    if user_id in _state["allowed_user_ids"]:
        return False
    _state["allowed_user_ids"].append(user_id)
    if user_id not in _state["users"]:
        _state["users"][user_id] = {"audio_model": None, "text_model": None}
    _persist_settings()
    return True


def remove_user(user_id: int) -> bool:
    """Remove a user from allowed_user_ids. Raise ValueError if user_id is the admin."""
    if is_admin(user_id):
        raise ValueError("Cannot remove the admin user")
    if user_id not in _state["allowed_user_ids"]:
        return False
    _state["allowed_user_ids"].remove(user_id)
    _state["users"].pop(user_id, None)
    _persist_settings()
    return True


def _effective_models(user_id: int | None, kind: str) -> list[str]:
    """
    Build the effective model chain for a user.
    User preference is prepended to the default chain, deduped.
    """
    defaults = _state["default_models"].get(kind, [])
    if user_id is None or user_id not in _state["users"]:
        return list(defaults)

    user_pref = _state["users"][user_id].get(f"{kind}_model")
    if not user_pref:
        return list(defaults)

    # Prepend user pref, then defaults, removing duplicates while preserving order
    result = [user_pref]
    for model in defaults:
        if model not in result:
            result.append(model)
    return result


def audio_models(user_id: int | None = None) -> list[str]:
    """Return effective audio model chain for the user."""
    return _effective_models(user_id, "audio")


def text_models(user_id: int | None = None) -> list[str]:
    """Return effective text model chain for the user."""
    return _effective_models(user_id, "text")


def image_models(user_id: int | None = None) -> list[str]:
    """Return effective image-generation model chain for the user."""
    return _effective_models(user_id, "image")


def get_user_model(user_id: int, kind: str) -> str | None:
    """Get user's per-user model preference for one of MODEL_KINDS."""
    _check_kind(kind)
    if user_id not in _state["users"]:
        return None
    return _state["users"][user_id].get(f"{kind}_model")


def set_user_model(user_id: int, kind: str, model: str | None) -> None:
    """Set user's per-user model preference for one of MODEL_KINDS. None clears it."""
    _check_kind(kind)
    if user_id not in _state["users"]:
        _state["users"][user_id] = {"audio_model": None, "text_model": None}
    _state["users"][user_id][f"{kind}_model"] = model
    _persist_settings()


# Automatic timestamping of long recordings
# 0 disables it; the default only fires on recordings long enough that finding your place
# in the transcript is the actual problem, so ordinary voice notes are untouched.
TIMESTAMP_THRESHOLD_DEFAULT = 600  # seconds
TIMESTAMP_THRESHOLD_CHOICES = (0, 300, 600, 900, 1800, 3600)


def timestamp_threshold(user_id: int | None = None) -> int:
    """Seconds of audio above which transcripts are auto-timestamped. 0 means never."""
    if user_id is None or user_id not in _state["users"]:
        return TIMESTAMP_THRESHOLD_DEFAULT
    value = _state["users"][user_id].get("timestamp_threshold")
    # Distinguish "not configured" from a deliberate 0, which means off.
    return TIMESTAMP_THRESHOLD_DEFAULT if value is None else int(value)


def set_timestamp_threshold(user_id: int, seconds: int | None) -> None:
    """Set the per-user auto-timestamp threshold. None restores the default, 0 disables."""
    if seconds is not None:
        seconds = int(seconds)
        if seconds < 0:
            raise ValueError("threshold must be >= 0")
    if user_id not in _state["users"]:
        _state["users"][user_id] = {"audio_model": None, "text_model": None}
    _state["users"][user_id]["timestamp_threshold"] = seconds
    _persist_settings()


def should_timestamp(user_id: int | None, duration_seconds: int | None) -> bool:
    """Whether a recording of this length should be transcribed with timestamps."""
    threshold = timestamp_threshold(user_id)
    if threshold <= 0 or not duration_seconds:
        return False
    return duration_seconds >= threshold


# Standing instructions (styles)
STYLE_KINDS = ("reply", "chat", "summary", "transcript", "image")
STYLE_MAX_LEN = 500


def get_user_style(user_id: int, kind: str) -> str | None:
    """Get user's standing instruction for a given kind, or None if not set."""
    if kind not in STYLE_KINDS:
        raise ValueError(f"kind must be one of {STYLE_KINDS}, got {kind}")
    if user_id not in _state["users"]:
        return None
    user = _state["users"][user_id]
    styles = user.get("styles", {})
    return styles.get(kind)


def user_styles(user_id: int) -> dict:
    """Get all standing instructions for a user, one entry per STYLE_KINDS kind."""
    if user_id not in _state["users"]:
        return {kind: None for kind in STYLE_KINDS}
    user = _state["users"][user_id]
    styles = user.get("styles", {})
    # Return dict with all kinds, None if not set
    return {kind: styles.get(kind) for kind in STYLE_KINDS}


def set_user_style(user_id: int, kind: str, text: str | None) -> None:
    """Set or clear a standing instruction. None or "" clears it. Raises ValueError on invalid kind or too-long text."""
    if kind not in STYLE_KINDS:
        raise ValueError(f"kind must be one of {STYLE_KINDS}, got {kind}")

    # Normalize empty/whitespace-only text to None so "" and None both mean "cleared",
    # and a blank value can never render as a set instruction in the menu.
    if text is not None:
        text = text.strip() or None

    # Check length if setting
    if text and len(text) > STYLE_MAX_LEN:
        raise ValueError(f"Style text exceeds {STYLE_MAX_LEN} characters")

    # Ensure user exists
    if user_id not in _state["users"]:
        _state["users"][user_id] = {"audio_model": None, "text_model": None}

    # Initialize or ensure styles dict exists with all keys
    if "styles" not in _state["users"][user_id]:
        _state["users"][user_id]["styles"] = {k: None for k in STYLE_KINDS}

    # Store the style (None to clear, or the stripped text)
    _state["users"][user_id]["styles"][kind] = text
    _persist_settings()


def default_models(kind: str) -> list[str]:
    """Return the current global default model chain."""
    _check_kind(kind)
    return list(_state["default_models"].get(kind, []))


def set_default_model(kind: str, model: str) -> None:
    """Promote model to the head of the default chain for this kind."""
    _check_kind(kind)
    chain = _state["default_models"].setdefault(kind, [])
    if model in chain:
        chain.remove(model)
    chain.insert(0, model)
    _persist_settings()


def gemini_client():
    """Return the current live Gemini client."""
    return _state["gemini_client"]


def set_api_key(key: str) -> None:
    """
    Validate key with a live call, then persist and rebuild client.
    Raise ValueError with a generic message if validation fails.
    On failure, the old key and client remain active.
    """
    if not key:
        raise ValueError("API key cannot be empty")

    # Validate with a cheap live call (list models)
    try:
        test_client = genai.Client(api_key=key)
        test_client.models.list()  # Cheap live call
    except genai_errors.APIError as e:
        # Log the status code only, never the message: an API error body can echo back
        # request material, and this log goes to journald. Same reasoning as the httpx
        # WARNING line in sedai_bot.py.
        log.warning("API key validation failed (%s)", e.code)
        raise ValueError("That key was rejected by the Gemini API.")
    except Exception as e:
        log.warning("Unexpected error during API key validation (%s)", type(e).__name__)
        raise ValueError("That key was rejected by the Gemini API.")

    # Persist the new key
    _state["gemini_api_key"] = key
    _state["gemini_client"] = genai.Client(api_key=key)
    _state["available_models_cache"]["data"] = None  # Invalidate cache
    _persist_settings()


def api_key_fingerprint() -> str:
    """Return the last 4 characters of the API key, prefixed with '…'."""
    key = _state["gemini_api_key"]
    if not key or len(key) < 4:
        return "…"
    return "…" + key[-4:]


def available_models(force: bool = False) -> list[str]:
    """
    Fetch and cache available models from Gemini API (600s cache).
    Keep only those supporting generateContent, strip "models/" prefix, sorted.
    On API failure, return union of configured chains. Never raise.
    """
    now = time.time()
    cache = _state["available_models_cache"]

    # Return cached data if fresh and not forced
    if not force and cache["data"] is not None and (now - cache["timestamp"]) < 600:
        return cache["data"]

    try:
        client = gemini_client()
        if client is None:
            raise ValueError("No client initialized")

        models_list = []
        for model in client.models.list():
            name = getattr(model, "name", None)
            if not name:
                continue
            # google-genai exposes this as supported_actions; older/other shapes use
            # supported_generation_methods. If neither is present we can't tell, so we
            # keep the model rather than hiding a usable one.
            actions = getattr(model, "supported_actions", None)
            if actions is None:
                actions = getattr(model, "supported_generation_methods", None)
            if actions is not None and "generateContent" not in actions:
                continue
            models_list.append(name[len("models/"):] if name.startswith("models/") else name)

        models_list = sorted(list(set(models_list)))
        cache["data"] = models_list
        cache["timestamp"] = now
        return models_list

    except Exception as e:
        log.warning("Failed to fetch available models (%s)", type(e).__name__)
        # Fall back to union of configured chains
        all_models = set()
        all_models.update(_state["default_models"].get("audio", []))
        all_models.update(_state["default_models"].get("text", []))
        # Also include user preferences
        for user_prefs in _state["users"].values():
            if user_prefs.get("audio_model"):
                all_models.add(user_prefs["audio_model"])
            if user_prefs.get("text_model"):
                all_models.add(user_prefs["text_model"])
        return sorted(list(all_models))


# ---------------------------------------------------------------------------
# Image spend metering
#
# Image generation is the only feature here with no free tier, so it is the only one that
# can run up a bill. Cost is derived from the token counts the API reports rather than a
# flat per-image price, because resolution changes the output token count and therefore the
# price. This is an ESTIMATE from a local price table, not billing truth — Google's console
# is authoritative, and a Cloud budget alert should back this up.
# Rates are USD per 1M tokens, as (input, output).
IMAGE_PRICING = {
    "gemini-3.1-flash-image": (0.50, 60.00),
    "gemini-3.1-flash-image-preview": (0.50, 60.00),
    "gemini-3.1-flash-lite-image": (0.25, 30.00),
    "gemini-2.5-flash-image": (0.30, 30.00),
    "gemini-3-pro-image": (2.00, 120.00),
    "gemini-3-pro-image-preview": (2.00, 120.00),
    "nano-banana-pro-preview": (2.00, 120.00),
}

# An unlisted model bills at the most expensive known rate. Erring high means a new or
# renamed model can only ever over-report spend, never quietly blow through the cap.
_IMAGE_PRICING_FALLBACK = (2.00, 120.00)

# Token counts assumed for the pre-flight check, before the real usage is known. Slightly
# above a typical 1K-resolution edit (275 in / 1193 out measured), so the check errs high.
_PRECHECK_INPUT_TOKENS = 1500
_PRECHECK_OUTPUT_TOKENS = 1400

IMAGE_BUDGET_DEFAULT = 10.0  # USD per calendar month; 0 disables image generation entirely


def _image_rates(model: str) -> tuple[float, float]:
    return IMAGE_PRICING.get(model, _IMAGE_PRICING_FALLBACK)


def image_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost of one image generation call."""
    rate_in, rate_out = _image_rates(model)
    return (input_tokens or 0) / 1e6 * rate_in + (output_tokens or 0) / 1e6 * rate_out


def image_cost_estimate(model: str) -> float:
    """Pre-flight cost estimate for one image, used before the call reports real usage."""
    return image_cost(model, _PRECHECK_INPUT_TOKENS, _PRECHECK_OUTPUT_TOKENS)


def image_budget() -> float:
    """Monthly image budget in USD. 0 means image generation is switched off."""
    value = _state["image_budget_usd"]
    # Distinguish unconfigured from a deliberate 0, which means off. A truthiness check here
    # would silently restore the default budget for an admin who set it to zero.
    return IMAGE_BUDGET_DEFAULT if value is None else float(value)


def set_image_budget(usd: float | None) -> None:
    """Set the monthly image budget. None restores the default, 0 disables generation."""
    if usd is not None:
        usd = float(usd)
        if usd < 0:
            raise ValueError("budget must be >= 0")
    _state["image_budget_usd"] = usd
    _persist_settings()


def _current_month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def _roll_month(persist: bool = True) -> dict:
    """Zero the running total when the calendar month changes (UTC)."""
    spend = _state["image_spend"]
    month = _current_month()
    if spend.get("month") != month:
        spend.clear()
        spend.update({"month": month, "usd": 0.0, "count": 0, "users": {}})
        if persist:
            _persist_settings()
    return spend


def image_spend() -> dict:
    """Current month's image spend: {month, usd, count, users}."""
    spend = _roll_month()
    return {
        "month": spend["month"],
        "usd": round(float(spend.get("usd", 0.0)), 4),
        "count": int(spend.get("count", 0)),
        "users": dict(spend.get("users", {})),
    }


def image_budget_remaining() -> float:
    budget = image_budget()
    if budget <= 0:
        return 0.0
    return max(0.0, budget - image_spend()["usd"])


def can_generate_image(model: str) -> tuple[bool, float]:
    """Whether one more image fits in this month's budget. Returns (allowed, remaining)."""
    budget = image_budget()
    remaining = image_budget_remaining()
    if budget <= 0:
        return False, 0.0
    return image_cost_estimate(model) <= remaining, remaining


def record_image_spend(user_id: int | None, model: str,
                       input_tokens: int, output_tokens: int) -> float:
    """Add one call's actual cost to the running total. Returns the cost recorded."""
    cost = image_cost(model, input_tokens, output_tokens)
    spend = _roll_month(persist=False)
    spend["usd"] = float(spend.get("usd", 0.0)) + cost
    spend["count"] = int(spend.get("count", 0)) + 1
    if user_id is not None:
        users = spend.setdefault("users", {})
        key = str(user_id)
        entry = users.setdefault(key, {"usd": 0.0, "count": 0})
        entry["usd"] = float(entry.get("usd", 0.0)) + cost
        entry["count"] = int(entry.get("count", 0)) + 1
    _persist_settings()
    return cost


def reset_image_spend() -> None:
    """Zero this month's running total without waiting for the month to roll."""
    _state["image_spend"] = {"month": _current_month(), "usd": 0.0, "count": 0, "users": {}}
    _persist_settings()


# Substrings that mark a model as an image generator. The models.list() response does not
# distinguish image output from text output — every image model reports plain
# generateContent — so the name is the only signal available.
_IMAGE_MODEL_MARKERS = ("image", "banana")


def is_image_model(name: str) -> bool:
    return any(marker in name.lower() for marker in _IMAGE_MODEL_MARKERS)


def models_for_kind(kind: str, force: bool = False) -> list[str]:
    """available_models() narrowed to those usable for this kind.

    Without this the image picker lists every text model too — around sixty entries — and
    picking a text model for image work fails at generation time with nothing to explain it.
    """
    _check_kind(kind)
    models = available_models(force=force)
    if kind == "image":
        return [m for m in models if is_image_model(m)]
    return [m for m in models if not is_image_model(m)]


def snapshot() -> dict:
    """Return a snapshot of settings for the status screen (no secrets beyond fingerprint)."""
    styles_count = 0
    for user_prefs in _state["users"].values():
        styles = user_prefs.get("styles", {})
        for kind in STYLE_KINDS:
            if styles.get(kind):
                styles_count += 1

    spend = image_spend()
    return {
        "default_audio_models": default_models("audio"),
        "default_text_models": default_models("text"),
        "default_image_models": default_models("image"),
        "image_budget_usd": image_budget(),
        "image_spend_usd": spend["usd"],
        "image_spend_count": spend["count"],
        "image_spend_month": spend["month"],
        "api_key_fingerprint": api_key_fingerprint(),
        "allowed_user_count": len(_state["allowed_user_ids"]),
        "settings_path": _get_settings_path(),
        "styles_count": styles_count,
    }
