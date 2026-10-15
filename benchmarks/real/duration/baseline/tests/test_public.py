import unittest

from duration.parse import parse_duration


class ParseDurationTests(unittest.TestCase):
    def test_bare_integer_means_seconds(self):
        self.assertEqual(parse_duration("45"), 45)

    def test_single_segment(self):
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration("2h"), 7200)

    def test_concatenated_segments_add_up(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("5m30s"), 330)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(parse_duration("  2m "), 120)

    def test_rejects_empty_string(self):
        with self.assertRaises(ValueError):
            parse_duration("")

    def test_rejects_unknown_unit(self):
        with self.assertRaises(ValueError):
            parse_duration("10x")


if __name__ == "__main__":
    unittest.main()
