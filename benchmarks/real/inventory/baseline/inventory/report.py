"""Plain-text rendering for the operator console."""

from .pricing import summarize


def render(items):
    """Render one order as a small text block."""
    totals = summarize(items)
    return "\n".join([
        f"lines: {len(items)}",
        f"subtotal: {totals['subtotal_cents']} cents",
        f"discount: {totals['discount_cents']} cents",
        f"total: {totals['total_cents']} cents",
    ])
