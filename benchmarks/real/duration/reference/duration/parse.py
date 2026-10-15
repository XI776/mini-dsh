"""Parse the short duration strings accepted by the scheduler.

Reference fix for the duration task: concatenated segments, optional
surrounding whitespace, and ValueError for every invalid shape.
"""

import re

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SEGMENT = re.compile(r"(\d+)([smhd]?)")


def parse_duration(value):
    """Parse a duration such as "45", "90s" or "1h30m" into whole seconds."""
    if not isinstance(value, str):
        raise ValueError("duration must be a string")
    text = value.strip()
    if not text:
        raise ValueError("duration must not be empty")
    if text.isdigit():
        return int(text)
    total = 0
    position = 0
    while position < len(text):
        match = _SEGMENT.match(text, position)
        if match is None or not match.group(2):
            raise ValueError(f"cannot parse duration {value!r}")
        total += int(match.group(1)) * _UNITS[match.group(2)]
        position = match.end()
    return total
