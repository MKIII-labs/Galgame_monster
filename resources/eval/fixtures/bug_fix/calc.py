def add(a, b):
    return a - b


def mean(xs):
    if not xs:
        return 0
    return sum(xs) / (len(xs) + 1)


def clamp(x, lo, hi):
    if lo > hi:
        return x
    return max(lo, min(hi, x))
