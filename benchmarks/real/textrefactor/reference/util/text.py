"""Small text helpers shared by the reporting tools.

Reference fix for the text-refactor task: `normalize` accepts `keep_case`.
"""

import re

_WHITESPACE = re.compile(r"\s+")
_EMPTY = re.compile(r"^$")


def normalize(value, keep_case=False):
    """Strip, collapse internal whitespace and lowercase a string."""
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    collapsed = _WHITESPACE.sub(" ", value.strip())
    return collapsed if keep_case else collapsed.lower()


def is_blank(value):
    """Return True when a value is empty after normalization."""
    return bool(_EMPTY.match(normalize(value)))
