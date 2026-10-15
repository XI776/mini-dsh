import unittest

from cli import describe
from report import render_title
from util.text import normalize


class NormalizeTests(unittest.TestCase):
    def test_collapses_internal_whitespace(self):
        self.assertEqual(normalize("  a \t b\n c "), "a b c")

    def test_lowercases_by_default(self):
        self.assertEqual(normalize("Already Lower"), "already lower")

    def test_keep_case_preserves_the_original_case(self):
        self.assertEqual(normalize("  Acme  Corp ", keep_case=True), "Acme Corp")

    def test_rejects_non_strings(self):
        with self.assertRaises(ValueError):
            normalize(42)


class CallerTests(unittest.TestCase):
    def test_report_titles_are_lowercased(self):
        self.assertEqual(render_title(" Q3 Revenue "), "== q3 revenue ==")

    def test_cli_describe_lowercases_the_term(self):
        self.assertEqual(describe(" Hello "), "searching for 'hello'")


if __name__ == "__main__":
    unittest.main()
