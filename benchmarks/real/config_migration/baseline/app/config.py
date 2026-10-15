"""Normalize operator configuration before the client uses it."""

from .defaults import DEFAULT_BASE_URL, DEFAULT_RETRIES, DEFAULT_TIMEOUT_S


def _positive_int(value, field):
    """Coerce `value` to a positive int, rejecting everything else."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            raise ValueError(f"{field} must be a positive integer")
        value = int(stripped)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def load_config(raw):
    """Return the normalized configuration used by the client."""
    return {
        "timeout_s": _positive_int(raw.get("timeout", DEFAULT_TIMEOUT_S), "timeout"),
        "retries": raw.get("retries", DEFAULT_RETRIES),
        "base_url": raw.get("base_url", DEFAULT_BASE_URL),
    }
