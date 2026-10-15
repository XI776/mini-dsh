"""Parse the short duration strings accepted by the scheduler."""

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value):
    """Parse a duration such as "45", "90s" or "2m" into whole seconds."""
    if not isinstance(value, str):
        raise ValueError("duration must be a string")
    if value.isdigit():
        return int(value)
    unit = value[-1]
    if unit not in _UNITS or not value[:-1].isdigit():
        raise ValueError(f"cannot parse duration {value!r}")
    return int(value[:-1]) * _UNITS[unit]
