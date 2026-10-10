# Someone else's average is hidden until this many people have rated: one or two ratings are too easy to trace to a person.
PUBLIC_MIN_RATINGS = 3


def average(count: int, total: int) -> float | None:
    """The mean score in integer hundredths, rounded half up (Python's round() rounds halves to even), then divided by 100.
    8 ratings totalling 37 give 4.63. None when nobody has rated."""
    if count == 0:
        return None
    return ((total * 100 + count // 2) // count) / 100
