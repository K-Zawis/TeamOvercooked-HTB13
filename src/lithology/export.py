"""Structured output for lithology intervals."""

import csv
from pathlib import Path
from typing import Iterable

from .types import LithologyInterval


FIELDS = ("top_y", "bottom_y", "cluster_id", "lithology", "confidence")


def write_intervals(
    path: str | Path,
    intervals: Iterable[LithologyInterval],
) -> None:
    """Write Person 3/4 interval fields without depth interpolation."""
    rows = tuple(intervals)
    if not rows:
        raise ValueError("intervals cannot be empty")
    if any(not isinstance(interval, LithologyInterval) for interval in rows):
        raise ValueError("intervals must contain LithologyInterval values")
    destination = Path(path)
    with destination.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        for interval in rows:
            writer.writerow({field: interval.to_dict()[field] for field in FIELDS})
