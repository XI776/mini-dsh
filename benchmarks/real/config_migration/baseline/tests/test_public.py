import unittest

from app.client import build_client
from app.config import load_config
from app.defaults import DEFAULT_BASE_URL, DEFAULT_RETRIES, DEFAULT_TIMEOUT_S


class LoadConfigTests(unittest.TestCase):
    def test_timeout_string_is_normalized(self):
        config = load_config({"timeout": "45"})
        self.assertEqual(config["timeout_s"], 45)
        self.assertIsInstance(config["timeout_s"], int)

    def test_missing_values_use_defaults(self):
        config = load_config({})
        self.assertEqual(config["timeout_s"], DEFAULT_TIMEOUT_S)
        self.assertEqual(config["retries"], DEFAULT_RETRIES)
        self.assertEqual(config["base_url"], DEFAULT_BASE_URL)

    def test_base_url_is_normalized_without_a_trailing_slash(self):
        config = load_config({"base_url": "https://reports.internal/"})
        self.assertEqual(config["base_url"], "https://reports.internal")

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"timeout": "soon"})


class ClientTests(unittest.TestCase):
    def test_build_client_uses_the_normalized_config(self):
        self.assertEqual(build_client({}).describe(), f"{DEFAULT_BASE_URL} timeout={DEFAULT_TIMEOUT_S}s retries={DEFAULT_RETRIES}")


if __name__ == "__main__":
    unittest.main()
