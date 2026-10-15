import unittest
from package.formatting import normalize_name


class FormatTests(unittest.TestCase):
    def test_name(self):
        self.assertEqual(normalize_name(" Bob "), "bob")
