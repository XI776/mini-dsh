import unittest
from pricing import discounted


class PricingTests(unittest.TestCase):
    def test_discount(self):
        self.assertEqual(discounted(100), 90)
