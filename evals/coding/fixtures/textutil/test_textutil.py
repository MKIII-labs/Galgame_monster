import unittest

from textutil import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_trim_and_spaces(self):
        self.assertEqual(slugify("  A  B "), "a-b")


if __name__ == "__main__":
    unittest.main()
