"""Normalize operator configuration before the client uses it.

Reference fix for the config-migration task: `retries` follows the same
coercion rule as `timeout`, and `base_url` must be a usable https URL.
"""

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


def _https_base_url(value):
    """Normalize an https base URL, rejecting anything unusable."""
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError("base_url must be an https:// URL")
    normalized = value.rstrip("/")
    if not normalized[len("https://"):]:
        raise ValueError("base_url must name a host")
    return normalized


def load_config(raw):
    """Return the normalized configuration used by the client."""
    return {
        "timeout_s": _positive_int(raw.get("timeout", DEFAULT_TIMEOUT_S), "timeout"),
        "retries": _positive_int(raw.get("retries", DEFAULT_RETRIES), "retries"),
        "base_url": _https_base_url(raw.get("base_url", DEFAULT_BASE_URL)),
    }
