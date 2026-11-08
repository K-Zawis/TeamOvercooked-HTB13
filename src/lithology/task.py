"""Task-payload adapter for the document-local lithology pipeline."""

from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

lazy import numpy as np
lazy from PIL import Image

from .classifier import analyze_lithology
from .intervals import merge_intervals, restrict_intervals
from .legend import read_legend
from .text import TextRecognizer
from .types import LegendPattern, LithologyInterval


Box = tuple[int, int, int, int]


def _box(value, name: str) -> Box:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} must be [left, top, right, bottom]")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{name} coordinates must be integers")
    left, top, right, bottom = value
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError(f"{name} must be a positive max-exclusive box")
    return left, top, right, bottom


@dataclass(frozen=True)
class LithologyTask:
    log_id: str
    image_path: str
    column_bbox: Box
    legend_bbox: Box
    y_start: int
    y_end: int

    @classmethod
    def from_payload(cls, payload) -> "LithologyTask":
        if not isinstance(payload, dict):
            raise ValueError("lithology task must be an object")
        required = (
            "log_id", "image_path", "column_bbox", "legend_bbox",
            "y_start", "y_end",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise ValueError(f"lithology task is missing {', '.join(missing)}")
        log_id, image_path = payload["log_id"], payload["image_path"]
        if not isinstance(log_id, str) or not log_id.strip():
            raise ValueError("log_id must be a nonempty string")
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("image_path must be a nonempty string")
        column = _box(payload["column_bbox"], "column_bbox")
        legend = _box(payload["legend_bbox"], "legend_bbox")
        y_start, y_end = payload["y_start"], payload["y_end"]
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (y_start, y_end)):
            raise ValueError("task Y coordinates must be integers")
        if not column[1] <= y_start < y_end <= column[3]:
            raise ValueError("task Y range must lie inside column_bbox")
        return cls(log_id.strip(), image_path, column, legend, y_start, y_end)


@dataclass(frozen=True)
class LithologyTaskResult:
    log_id: str
    y_start: int
    y_end: int
    legend: tuple[LegendPattern, ...]
    intervals: tuple[LithologyInterval, ...]

    def to_dict(self) -> dict:
        return {
            "log_id": self.log_id,
            "y_start": self.y_start,
            "y_end": self.y_end,
            "legend": {
                f"cluster_{index}": pattern.name
                for index, pattern in enumerate(self.legend)
            },
            "intervals": [interval.to_dict() for interval in self.intervals],
        }


def read_task_images(task: LithologyTask) -> tuple[np.ndarray, np.ndarray]:
    """Read the owned lithology strip and complete legend crop as RGB arrays."""
    left, _, right, _ = task.column_bbox
    with Image.open(Path(task.image_path)) as image:
        if (
            task.column_bbox[2] > image.width
            or task.column_bbox[3] > image.height
            or task.legend_bbox[2] > image.width
            or task.legend_bbox[3] > image.height
        ):
            raise ValueError("task bounding box extends outside the image")
        column = np.asarray(
            image.crop((left, task.y_start, right, task.y_end)).convert("RGB"),
            dtype=np.uint8,
        )
        legend = np.asarray(image.crop(task.legend_bbox).convert("RGB"), dtype=np.uint8)
    return np.ascontiguousarray(column), np.ascontiguousarray(legend)


def process_lithology_task(
    payload: dict | LithologyTask,
    *,
    recognizer: TextRecognizer | None = None,
    legend_cache: MutableMapping[tuple, tuple[LegendPattern, ...]] | None = None,
    window_height: int = 64,
    stride: int = 16,
    transition_penalty: float = 0.08,
    unknown_distance: float = 0.38,
) -> LithologyTaskResult:
    """Process one payload delivered by the infrastructure task listener."""
    task = (
        payload
        if isinstance(payload, LithologyTask)
        else LithologyTask.from_payload(payload)
    )
    column_image, legend_image = read_task_images(task)
    cache_key = (task.log_id, task.image_path, task.legend_bbox)
    legend = legend_cache.get(cache_key) if legend_cache is not None else None
    if legend is None:
        legend = tuple(read_legend(legend_image, recognizer))
        if legend_cache is not None:
            legend_cache[cache_key] = legend
    intervals = analyze_lithology(
        column_image,
        legend,
        global_y=task.y_start,
        window_height=window_height,
        stride=stride,
        transition_penalty=transition_penalty,
        unknown_distance=unknown_distance,
    )
    intervals = merge_intervals(
        restrict_intervals(intervals, task.y_start, task.y_end)
    )
    return LithologyTaskResult(
        task.log_id,
        task.y_start,
        task.y_end,
        tuple(legend),
        tuple(intervals),
    )
