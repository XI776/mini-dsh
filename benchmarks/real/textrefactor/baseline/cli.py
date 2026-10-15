"""Minimal operator CLI helpers."""

from util.text import normalize


def describe(term):
    """Render the search prompt for one operator term."""
    return f"searching for {normalize(term)!r}"


def prompt(term):
    """Return the prompt line plus the normalized echo."""
    return f"{describe(term)}\nterm> "
