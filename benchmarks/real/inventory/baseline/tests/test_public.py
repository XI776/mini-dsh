import unittest

from inventory.models import Item
from inventory.pricing import summarize
from inventory.report import render
from inventory.serialize import to_json


class SummarizeTests(unittest.TestCase):
    def test_single_line_volume_discount(self):
        totals = summarize([Item(price_cents=100, quantity=12)])
        self.assertEqual(totals["subtotal_cents"], 1200)
        self.assertEqual(totals["discount_cents"], 120)
        self.assertEqual(totals["total_cents"], 1080)

    def test_small_lines_are_not_aggregated_into_a_tier(self):
        totals = summarize([Item(100, 6), Item(100, 6)])
        self.assertEqual(totals["subtotal_cents"], 1200)
        self.assertEqual(totals["discount_cents"], 0)
        self.assertEqual(totals["total_cents"], 1200)

    def test_no_discount_below_the_first_tier(self):
        totals = summarize([Item(250, 3)])
        self.assertEqual(totals["total_cents"], 750)


class RenderTests(unittest.TestCase):
    def test_render_and_export_use_the_same_totals(self):
        items = [Item(100, 12)]
        self.assertIn("total: 1080 cents", render(items))
        self.assertEqual(to_json(items), '{"discount_cents": 120, "subtotal_cents": 1200, "total_cents": 1080}')


if __name__ == "__main__":
    unittest.main()
