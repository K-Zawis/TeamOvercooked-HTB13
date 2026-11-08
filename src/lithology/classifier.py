"""Classify a lithology track against named patterns from its own legend."""

from __future__ import annotations

import math
from typing import Sequence

lazy import numpy as np

from .features import compute_feature_map
from .types import FeatureWindow, LegendPattern, LithologyInterval


def _decode(costs: np.ndarray, transition_penalty: float) -> np.ndarray:
    """Viterbi decoding for optimal lithology sequence with transition penalty."""
    num_windows, num_states = costs.shape
    accumulated_costs = np.empty_like(costs)
    backpointers = np.zeros((num_windows, num_states), dtype=np.intp)

    accumulated_costs[0] = costs[0]

    # Precompute the static state transition penalty matrix once
    penalty_matrix = transition_penalty * (1.0 - np.eye(num_states, dtype=np.float64))

    for step in range(1, num_windows):
        # Shape: (prev_state, curr_state)
        transition_costs = accumulated_costs[step - 1][:, None] + penalty_matrix
        backpointers[step] = np.argmin(transition_costs, axis=0)
        accumulated_costs[step] = costs[step] + np.min(transition_costs, axis=0)

    path = np.empty(num_windows, dtype=np.intp)
    path[-1] = int(np.argmin(accumulated_costs[-1]))
    for step in range(num_windows - 1, 0, -1):
        path[step - 1] = backpointers[step, path[step]]

    return path


def classify_features(
    feature_map: Sequence[FeatureWindow],
    legend: Sequence[LegendPattern],
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
    if any(
        curr.top_y < prev.top_y or curr.bottom_y <= prev.bottom_y
        for prev, curr in zip(windows, windows[1:])
    ):
        raise ValueError("feature windows must be ordered from top to bottom")
    if any(curr.top_y >= prev.bottom_y for prev, curr in zip(windows, windows[1:])):
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

    # Stack descriptors into 2D matrices: (num_windows, dim) and (num_patterns, dim)
    window_matrix = np.vstack([w.features.combined for w in windows])
    pattern_matrix = np.vstack([p.features.combined for p in patterns])

    # Both feature sets are L2 unit-normalized in features.py; cosine distance is 1.0 - (W @ P.T)
    cosine_sim = np.clip(np.dot(window_matrix, pattern_matrix.T), -1.0, 1.0)
    distances = np.clip(1.0 - cosine_sim, 0.0, 2.0)

    # Append unknown state column
    unknown_col = np.full((len(windows), 1), unknown_distance, dtype=np.float64)
    costs = np.hstack([distances, unknown_col])

    # Dynamic programming sequence decode
    state_sequence = _decode(costs, transition_penalty)
    labels = [names[s] if s < len(names) else None for s in state_sequence]
    confidence_per_window = np.clip(1.0 - distances.min(axis=1), 0.0, 1.0)

    # Identify interval transition boundaries
    centers = np.array([(w.top_y + w.bottom_y) / 2 for w in windows])
    boundaries = [windows[0].top_y]
    change_indices = [0]

    for idx in range(1, len(windows)):
        if labels[idx] != labels[idx - 1]:
            boundaries.append(round((centers[idx - 1] + centers[idx]) / 2))
            change_indices.append(idx)
    boundaries.append(windows[-1].bottom_y)
    change_indices.append(len(windows))

    # Fast O(1) cluster lookup mapping
    name_to_cluster = {name: f"cluster_{i}" for i, name in enumerate(names)}

    intervals: list[LithologyInterval] = []
    for seg_idx in range(len(boundaries) - 1):
        top_y = boundaries[seg_idx]
        bottom_y = boundaries[seg_idx + 1]
        w_start = change_indices[seg_idx]
        w_end = change_indices[seg_idx + 1]

        label = labels[w_start]
        avg_score = float(confidence_per_window[w_start:w_end].mean())
        cluster_id = name_to_cluster.get(label) if label is not None else None

        intervals.append(
            LithologyInterval(
                top_y=top_y,
                bottom_y=bottom_y,
                lithology=label,
                confidence=avg_score,
                cluster_id=cluster_id,
            )
        )

    return intervals


def analyze_lithology(
    image: np.ndarray,
    legend: Sequence[LegendPattern],
    *,
    global_y: int = 0,
    window_height: int = 64,
    stride: int = 16,
    transition_penalty: float = 0.08,
    unknown_distance: float = 0.38,
) -> list[LithologyInterval]:
    """Compute track features and return contiguous classified intervals."""
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