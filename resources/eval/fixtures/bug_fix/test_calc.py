from calc import add, clamp, mean


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([2, 4, 6]) == 4


def test_clamp_reversed_bounds():
    assert clamp(5, 10, 0) == 5
