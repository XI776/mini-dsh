import tempfile
import unittest
from pathlib import Path

from logtool.summary import summarize_errors

SAMPLE = (
    "2026-10-14T00:00:00Z INFO api request completed\n"
    "2026-10-14T00:00:07Z ERROR storage checksum mismatch\n"
    "\n"
    "2026-10-14T00:00:14Z CRITICAL api connection reset by peer\n"
    "not a well formed line\n"
    "2026-10-14T00:00:21Z WARN worker\n"
)


class SummaryTests(unittest.TestCase):
    def test_sample_log_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.log"
            path.write_text(SAMPLE, encoding="utf-8")
            result = summarize_errors(path)
        self.assertEqual(result["by_level"], {"INFO": 1, "ERROR": 1, "CRITICAL": 1})
        self.assertEqual(result["by_component"], {"storage": 1, "api": 1})
        self.assertEqual(result["total_errors"], 2)
        self.assertEqual(result["malformed"], 2)
        self.assertEqual(result["first_error"], "checksum mismatch")

    def test_empty_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.log"
            path.write_text("", encoding="utf-8")
            result = summarize_errors(path)
        self.assertEqual(result["total_errors"], 0)
        self.assertIsNone(result["first_error"])


if __name__ == "__main__":
    unittest.main()
