"""Domain objects shared by the pricing, report and export modules."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    """One inventory line: a unit price in cents and the number of units sold."""

    price_cents: int
    quantity: int
