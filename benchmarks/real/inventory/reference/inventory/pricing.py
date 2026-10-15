"""Volume discount rules for inventory lines.

Reference fix for the inventory-tiers task: tiers use >= boundaries, the tier
is chosen per line, and line totals round halves up.
"""

from .models import Item

# (minimum quantity, discount percent), highest tier first
DISCOUNT_TIERS = ((50, 20), (10, 10))


def tier_percent(quantity):
    """Return the discount percent for a single line quantity."""
    for threshold, percent in DISCOUNT_TIERS:
        if quantity >= threshold:
            return percent
    return 0


def _discounted_total(subtotal_cents, quantity):
    """Apply the tier percent for one quantity to a subtotal."""
    return (subtotal_cents * (100 - tier_percent(quantity)) + 50) // 100


def line_total(price_cents, quantity):
    """Return the discounted total for one line, rounded half up."""
    if price_cents < 0 or quantity < 0:
        raise ValueError("price and quantity must not be negative")
    return _discounted_total(price_cents * quantity, quantity)


def summarize(items):
    """Summarize a list of items into subtotal, discount and total cents."""
    subtotal = sum(item.price_cents * item.quantity for item in items)
    total = sum(line_total(item.price_cents, item.quantity) for item in items)
    return {
        "subtotal_cents": subtotal,
        "discount_cents": subtotal - total,
        "total_cents": total,
    }
