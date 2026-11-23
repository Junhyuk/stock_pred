"""Expanding-window updates for the index transformer.

Each block is scored before its labels are allowed into training.
"""
from __future__ import annotations


def iter_learning_blocks(n_windows: int, first_test_window: int, block_size: int):
    """Yield half-open window ranges that a fitted model must score before seeing."""
    if block_size < 1:
        raise ValueError("block_size must be positive")
    if not 0 < first_test_window < n_windows:
        raise ValueError("first test window must leave training history and future blocks")
    start = first_test_window
    while start < n_windows:
        end = min(start + block_size, n_windows)
        yield start, end
        start = end


def block_score(actual_up, probability) -> dict:
    actual = [1.0 if value else 0.0 for value in actual_up]
    clipped = [min(1 - 1e-6, max(1e-6, float(value))) for value in probability]
    n = len(actual)
    if n == 0 or n != len(clipped):
        raise ValueError("actual and probability must be non-empty and aligned")
    brier = sum((pred - label) ** 2 for pred, label in zip(clipped, actual)) / n
    hits = sum((pred >= 0.5) == (label >= 0.5) for pred, label in zip(clipped, actual)) / n
    return {
        "n": n,
        "brier": round(brier, 4),
        "direction_hit": round(hits, 4),
        "mean_p_up": round(sum(clipped) / n, 4),
    }
