"""
Shared helpers used by more than one Sedai module.
"""


def user_error(prefix: str, exc: Exception) -> str:
    """A reportable message that never carries an API error body.

    Interpolating the exception would put provider text — which can quote back request
    material — into the chat. The type and any status code are enough to act on; the
    full detail is in the log.
    """
    code = getattr(exc, "code", None)
    detail = f"{type(exc).__name__}" + (f" {code}" if code else "")
    return f"{prefix} ({detail}). It's logged — try again, or /help."
