import unittest
from settings import DEFAULT_PORT


class ConfigTests(unittest.TestCase):
    def test_port(self):
        self.assertEqual(DEFAULT_PORT + 1, 8081)
