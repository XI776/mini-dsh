import unittest
from slug import slugify


class SlugTests(unittest.TestCase):
    def test_words(self):
        self.assertEqual(slugify("Hello  World"), "hello-world")
