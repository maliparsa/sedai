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

# Global mutable state: the current in-memory config
_state = {
    "gemini_api_key": None,
    "gemini_client": None,
    "allowed_user_ids": [],
    "default_models": {"audio": _DEFAULT_AUDIO_MODELS.copy(), "text": _DEFAULT_TEXT_MODELS.copy()},
    "users": {},  # user_id (as int key) -> {"audio_model": ..., "text_model": ...}
    "available_models_cache": {"data": None, "timestamp": 0},
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
        _state["default_models"] = file_data.get("default_models") or {
            "audio": _DEFAULT_AUDIO_MODELS.copy(),
            "text": _DEFAULT_TEXT_MODELS.copy(),
        }
        _state["users"] = file_data.get("users") or {}
    else:
        # Fall back to env/defaults
        _state["gemini_api_key"] = _get_api_key_from_env()
        _state["allowed_user_ids"] = _get_allowed_user_ids_from_env()
        _state["default_models"] = {
            "audio": _DEFAULT_AUDIO_MODELS.copy(),
            "text": _DEFAULT_TEXT_MODELS.copy(),
        }
        _state["users"] = {}

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


def get_user_model(user_id: int, kind: str) -> str | None:
    """Get user's per-user preference for audio or text models."""
    if kind not in ("audio", "text"):
        raise ValueError(f"kind must be 'audio' or 'text', got {kind}")
    if user_id not in _state["users"]:
        return None
    return _state["users"][user_id].get(f"{kind}_model")


def set_user_model(user_id: int, kind: str, model: str | None) -> None:
    """Set user's per-user preference for audio or text models. None clears it."""
    if kind not in ("audio", "text"):
        raise ValueError(f"kind must be 'audio' or 'text', got {kind}")
    if user_id not in _state["users"]:
        _state["users"][user_id] = {"audio_model": None, "text_model": None}
    _state["users"][user_id][f"{kind}_model"] = model
    _persist_settings()


# Standing instructions (styles)
STYLE_KINDS = ("reply", "chat", "summary")
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
    """Get all standing instructions for a user: {"reply": ..., "chat": ..., "summary": ...}."""
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
    if kind not in ("audio", "text"):
        raise ValueError(f"kind must be 'audio' or 'text', got {kind}")
    return list(_state["default_models"].get(kind, []))


def set_default_model(kind: str, model: str) -> None:
    """Promote model to the head of the default chain for audio or text."""
    if kind not in ("audio", "text"):
        raise ValueError(f"kind must be 'audio' or 'text', got {kind}")
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


def snapshot() -> dict:
    """Return a snapshot of settings for the status screen (no secrets beyond fingerprint)."""
    styles_count = 0
    for user_prefs in _state["users"].values():
        styles = user_prefs.get("styles", {})
        for kind in STYLE_KINDS:
            if styles.get(kind):
                styles_count += 1

    return {
        "default_audio_models": default_models("audio"),
        "default_text_models": default_models("text"),
        "api_key_fingerprint": api_key_fingerprint(),
        "allowed_user_count": len(_state["allowed_user_ids"]),
        "settings_path": _get_settings_path(),
        "styles_count": styles_count,
    }
