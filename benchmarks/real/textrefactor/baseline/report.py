"""Report titles for the operator console."""

from util.text import normalize


def render_title(title):
    """Render a normalized section title."""
    return f"== {normalize(title)} =="
