"""Validation and reduction of classified lithology intervals."""

from dataclasses import replace
from typing import Iterable

from .types import LithologyInterval


def restrict_intervals(
    intervals: Iterable[LithologyInterval],
    top_y: int,
    bottom_y: int,
) -> list[LithologyInterval]:
    """Clip intervals to an owned half-open task range."""
    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in (top_y, bottom_y)):
        raise ValueError("owned range coordinates must be integers")
    if top_y < 0 or bottom_y <= top_y:
        raise ValueError("owned range must be a positive half-open interval")
    restricted = []
    for interval in intervals:
        if not isinstance(interval, LithologyInterval):
            raise ValueError("intervals must contain LithologyInterval values")
        top = max(top_y, interval.top_y)
        bottom = min(bottom_y, interval.bottom_y)
        if top < bottom:
            restricted.append(replace(interval, top_y=top, bottom_y=bottom))
    return restricted


def merge_intervals(
    intervals: Iterable[LithologyInterval],
) -> list[LithologyInterval]:
    """Merge touching or overlapping intervals only when classes agree."""
    ordered = sorted(intervals, key=lambda interval: (interval.top_y, interval.bottom_y))
    if any(not isinstance(interval, LithologyInterval) for interval in ordered):
        raise ValueError("intervals must contain LithologyInterval values")
    merged: list[LithologyInterval] = []
    for interval in ordered:
        if not merged:
            merged.append(interval)
            continue
        current = merged[-1]
        overlaps = interval.top_y < current.bottom_y
        touches = interval.top_y == current.bottom_y
        known_class = current.lithology is not None and current.cluster_id is not None
        same_class = (
            known_class
            and current.lithology == interval.lithology
            and current.cluster_id == interval.cluster_id
        )
        if same_class and (touches or overlaps):
            merged[-1] = replace(
                current,
                bottom_y=max(current.bottom_y, interval.bottom_y),
                confidence=min(current.confidence, interval.confidence),
            )
        elif overlaps:
            raise ValueError("conflicting lithology intervals overlap")
        else:
            merged.append(interval)
    return merged
