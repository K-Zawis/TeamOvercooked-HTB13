"""Classify a lithology track against named patterns from its own legend."""

import math

lazy import numpy as np

from .features import compute_feature_map
from .types import FeatureWindow, LegendPattern, LithologyInterval


def _cosine_distance(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        raise ValueError("legend and track features use incompatible dimensions")
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if not denominator:
        return 1.0
    similarity = float(np.dot(left, right) / denominator)
    return float(np.clip(1.0 - similarity, 0.0, 2.0))


def _decode(costs: np.ndarray, transition_penalty: float) -> np.ndarray:
    count, states = costs.shape
    totals = np.empty_like(costs)
    previous = np.zeros((count, states), dtype=np.intp)
    totals[0] = costs[0]
    indices = np.arange(states)
    for row in range(1, count):
        transitions = totals[row - 1][:, None] + transition_penalty * (
            indices[:, None] != indices[None, :]
        )
        previous[row] = np.argmin(transitions, axis=0)
        totals[row] = costs[row] + transitions[previous[row], indices]
    path = np.empty(count, dtype=np.intp)
    path[-1] = int(np.argmin(totals[-1]))
    for row in range(count - 1, 0, -1):
        path[row - 1] = previous[row, path[row]]
    return path


def classify_features(
    feature_map: list[FeatureWindow] | tuple[FeatureWindow, ...],
    legend: list[LegendPattern] | tuple[LegendPattern, ...],
    *,
    transition_penalty: float = 0.08,
    unknown_distance: float = 0.38,
) -> list[LithologyInterval]:
    """Classify a spatial feature map into contiguous lithology intervals."""
    windows = tuple(feature_map)
    if not windows:
        raise ValueError("feature_map must contain at least one window")
    if any(not isinstance(window, FeatureWindow) for window in windows):
        raise ValueError("feature_map must contain FeatureWindow values")
    if any(right.top_y < left.top_y or right.bottom_y <= left.bottom_y
           for left, right in zip(windows, windows[1:])):
        raise ValueError("feature windows must be ordered from top to bottom")
    if any(right.top_y >= left.bottom_y
           for left, right in zip(windows, windows[1:])):
        raise ValueError("feature windows must overlap")
    if not math.isfinite(transition_penalty) or transition_penalty < 0:
        raise ValueError("transition_penalty must be finite and nonnegative")
    if not math.isfinite(unknown_distance) or not 0 < unknown_distance <= 2:
        raise ValueError("unknown_distance must be in (0, 2]")
    patterns = tuple(legend)
    if not patterns:
        raise ValueError("legend must contain at least one named pattern")
    names = [pattern.name for pattern in patterns]
    if len(set(names)) != len(names):
        raise ValueError("legend names must be unique")

    distances = np.empty((len(windows), len(patterns)), dtype=np.float64)
    for row, window in enumerate(windows):
        distances[row] = [
            _cosine_distance(window.features.combined, pattern.features.combined)
            for pattern in patterns
        ]
    costs = np.column_stack(
        [distances, np.full(len(windows), unknown_distance, dtype=np.float64)]
    )
    states = _decode(costs, transition_penalty)
    labels = [names[state] if state < len(names) else None for state in states]
    confidence = np.clip(1.0 - distances.min(axis=1), 0.0, 1.0)

    centers = np.array([(window.top_y + window.bottom_y) / 2 for window in windows])
    boundaries = [windows[0].top_y]
    for index in range(1, len(windows)):
        if labels[index] != labels[index - 1]:
            boundaries.append(round((centers[index - 1] + centers[index]) / 2))
    boundaries.append(windows[-1].bottom_y)

    intervals = []
    start_window = 0
    for interval_index, (top, bottom) in enumerate(zip(boundaries, boundaries[1:])):
        label = labels[start_window]
        next_start = len(windows)
        if interval_index + 1 < len(boundaries) - 1:
            for index in range(start_window + 1, len(windows)):
                if labels[index] != label:
                    next_start = index
                    break
        score = float(confidence[start_window:next_start].mean())
        cluster_id = f"cluster_{names.index(label)}" if label is not None else None
        intervals.append(LithologyInterval(top, bottom, label, score, cluster_id))
        start_window = next_start
    return intervals


def analyze_lithology(
    image,
    legend: list[LegendPattern] | tuple[LegendPattern, ...],
    *,
    global_y: int = 0,
    window_height: int = 64,
    stride: int = 16,
    transition_penalty: float = 0.08,
    unknown_distance: float = 0.38,
) -> list[LithologyInterval]:
    """Compute track features and return contiguous global-Y intervals.

    Small overlapping windows are compared with named legend patterns. Dynamic
    programming discourages isolated label changes, and adjacent equal labels
    are collapsed into half-open intervals. ``None`` represents an unmatched
    pattern.
    """
    feature_map = compute_feature_map(
        image,
        global_y=global_y,
        window_height=window_height,
        stride=stride,
    )
    return classify_features(
        feature_map,
        legend,
        transition_penalty=transition_penalty,
        unknown_distance=unknown_distance,
    )
