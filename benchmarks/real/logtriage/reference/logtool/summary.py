"""Summaries for the on-call log dashboard.

Reference implementation for the log-triage task.
"""

import re

_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) "
    r"(?P<level>[A-Z]+) "
    r"(?P<component>[a-z]+) "
    r"(?P<message>\S.*)$"
)
_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
_ERROR_LEVELS = ("ERROR", "CRITICAL")


def summarize_errors(path):
    """Summarize the event log at `path`; see README.md for the contract."""
    by_level = {}
    by_component = {}
    total_errors = 0
    malformed = 0
    first_error = None
    with open(path, "r", encoding="utf-8") as stream:
        for raw in stream:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            match = _LINE.match(line)
            if match is None or match.group("level") not in _LEVELS:
                malformed += 1
                continue
            level = match.group("level")
            component = match.group("component")
            by_level[level] = by_level.get(level, 0) + 1
            if level in _ERROR_LEVELS:
                total_errors += 1
                by_component[component] = by_component.get(component, 0) + 1
                if first_error is None:
                    first_error = match.group("message")
    return {
        "by_level": by_level,
        "by_component": by_component,
        "total_errors": total_errors,
        "malformed": malformed,
        "first_error": first_error,
    }
