from legacy import normalize_records, total


def test_normalize():
    assert normalize_records([" A ", "", "B"]) == ["a", "b"]


def test_total():
    assert total([" A ", "B"]) == 2
