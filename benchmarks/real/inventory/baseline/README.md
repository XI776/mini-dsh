# Inventory pricing

`inventory.pricing.summarize(items)` turns a list of `inventory.models.Item`
lines into a dict with `subtotal_cents`, `discount_cents` and `total_cents`.

Billing rules:

- A line subtotal is `price_cents * quantity`.
- Volume tiers live in `inventory.pricing.DISCOUNT_TIERS` as
  `(minimum_quantity, discount_percent)` pairs, highest tier first. The first
  tier whose minimum is **less than or equal to** the line quantity applies, so
  a line of exactly 10 units gets the 10% tier and a line of exactly 50 units
  gets the 20% tier.
- Tiers are decided **per line**, never on the quantity of the whole order.
  Two lines of 6 units each stay untiered.
- The tier percent is applied to the line subtotal, and every line total is
  rounded to the nearest cent with halves rounded up.
- `total_cents` is the sum of the rounded line totals, and `discount_cents` is
  `subtotal_cents - total_cents`.

`inventory.report.render` and `inventory.serialize.to_json` are thin wrappers
used by the operator console and the nightly export job.
