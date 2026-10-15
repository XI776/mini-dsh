import unittest

from calculator import add


class PublicTests(unittest.TestCase):
    def test_positive_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)
