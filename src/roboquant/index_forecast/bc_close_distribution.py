"""Empirical return distribution weighted by Model C's three-class probabilities."""

import math
from collections.abc import Iterable

RANGE_LEVEL = 0.80
WINDOW = 120
THRESHOLD = 0.03


def event_class(value: float) -> int:
    if value <= -THRESHOLD + 1e-12:
        return 0
    if value >= THRESHOLD - 1e-12:
        return 2
    return 1


def c_return_distribution(
    returns: Iterable[float], probabilities: tuple[float, float, float]
) -> tuple[float, float, float] | None:
    """Return the mean and central 80% bounds of the class-weighted mixture."""
    pools = [[], [], []]
    for value in returns:
        pools[event_class(value)].append(value)
    if not all(pools):
        return None

    total = math.fsum(probabilities)
    if not math.isclose(total, 1.0, abs_tol=1e-6) or min(probabilities) < 0:
        raise ValueError("C class probabilities must be nonnegative and sum to one")
    weighted = []
    for pool, probability in zip(pools, probabilities, strict=True):
        sample = pool[-WINDOW:]
        weight = probability / (total * len(sample))
        weighted.extend((value, weight) for value in sample)
    mean = math.fsum(value * weight for value, weight in weighted)
    weighted.sort()

    tail = (1 - RANGE_LEVEL) / 2
    low = None
    mass = 0.0
    for value, weight in weighted:
        mass += weight
        if low is None and mass >= tail - 1e-12:
            low = float(value)
        if mass >= 1 - tail - 1e-12:
            return mean, low, float(value)
    raise ValueError("incomplete C probability distribution")


def c_close_estimate(
    close: float, returns: Iterable[float], probabilities: tuple[float, float, float]
) -> dict:
    distribution = c_return_distribution(returns, probabilities)
    if distribution is None:
        return {}
    mean, low, high = distribution
    return {
        "expected_return": mean,
        "expected_index": close * (1 + mean),
        "interval": {"low": close * (1 + low), "high": close * (1 + high), "level": RANGE_LEVEL},
    }
