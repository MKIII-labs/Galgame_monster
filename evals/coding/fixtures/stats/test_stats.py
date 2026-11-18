import unittest

from stats import average


class TestAverage(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(average([2, 4]), 3)

    def test_empty(self):
        self.assertEqual(average([]), 0)


if __name__ == "__main__":
    unittest.main()
