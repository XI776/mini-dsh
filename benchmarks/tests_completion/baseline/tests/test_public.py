import unittest
from mathops import multiply


class ExistingTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(multiply(8, 0), 0)
