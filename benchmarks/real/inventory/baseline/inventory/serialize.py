"""Nightly export helpers."""

import json

from .pricing import summarize


def to_json(items):
    """Serialize an order summary with stable key order."""
    return json.dumps(summarize(items), sort_keys=True)
