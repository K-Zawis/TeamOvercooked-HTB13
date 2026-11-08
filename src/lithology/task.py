"""Task-payload adapter for the document-local lithology pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

lazy import numpy as np
lazy import tifffile
lazy import zarr

from .classifier import analyze_lithology
from .intervals import merge_intervals, restrict_intervals
from .legend import read_legend
from .text import TextRecognizer
from .types import LegendPattern, LithologyInterval

Box = tuple[int, int, int, int]


def _validate_coordinate(val: object, field_name: str) -> int:
    """Validate that a coordinate value is an integer (rejecting booleans)."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{field_name} coordinate must be an integer")
    return val


def _validate_box(box_coords: object, box_name: str) -> Box:
    """Validate bounding box format: [left, top, right, bottom] with positive bounds."""
    if not isinstance(box_coords, (list, tuple)) or len(box_coords) != 4:
        raise ValueError(f"{box_name} must be [left, top, right, bottom]")

    left = _validate_coordinate(box_coords[0], f"{box_name} left")
    top = _validate_coordinate(box_coords[1], f"{box_name} top")
    right = _validate_coordinate(box_coords[2], f"{box_name} right")
    bottom = _validate_coordinate(box_coords[3], f"{box_name} bottom")

    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError(f"{box_name} must be a positive max-exclusive box")

    return left, top, right, bottom


@dataclass(frozen=True, slots=True)
class LithologyTask:
    log_id: str
    image_path: str
    column_bbox: Box
    legend_bbox: Box
    y_start: int
    y_end: int

    @classmethod
    def from_payload(cls, payload: object) -> LithologyTask:
        if not isinstance(payload, dict):
            raise ValueError("lithology task must be an object")

        required = (
            "log_id",
            "image_path",
            "column_bbox",
            "legend_bbox",
            "y_start",
            "y_end",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"lithology task is missing {', '.join(missing)}")

        log_id = payload["log_id"]
        image_path = payload["image_path"]
        if not isinstance(log_id, str) or not log_id.strip():
            raise ValueError("log_id must be a nonempty string")
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("image_path must be a nonempty string")

        col_box = _validate_box(payload["column_bbox"], "column_bbox")
        leg_box = _validate_box(payload["legend_bbox"], "legend_bbox")
        y_start = _validate_coordinate(payload["y_start"], "task y_start")
        y_end = _validate_coordinate(payload["y_end"], "task y_end")

        if not (col_box[1] <= y_start < y_end <= col_box[3]):
            raise ValueError("task Y range must lie inside column_bbox")

        return cls(
            log_id=log_id.strip(),
            image_path=image_path.strip(),
            column_bbox=col_box,
            legend_bbox=leg_box,
            y_start=y_start,
            y_end=y_end,
        )


@dataclass(frozen=True, slots=True)
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
                f"cluster_{idx}": pattern.name
                for idx, pattern in enumerate(self.legend)
            },
            "intervals": [interval.to_dict() for interval in self.intervals],
        }


def _normalize_to_rgb(
    array: np.ndarray,
    colormap: np.ndarray | None = None,
) -> np.ndarray:
    """Convert raw sliced image arrays to standard uint8 RGB (H, W, 3)."""
    if colormap is not None and array.ndim == 2:
        if colormap.dtype == np.uint16 or colormap.max() > 255:
            rgb = (colormap[:, array] >> 8).astype(np.uint8)
        else:
            rgb = colormap[:, array].astype(np.uint8)
        return np.ascontiguousarray(rgb.transpose(1, 2, 0))

    if array.ndim == 2:
        return np.ascontiguousarray(
            np.repeat(array[:, :, np.newaxis], 3, axis=-1),
            dtype=np.uint8,
        )

    if array.ndim == 3:
        channels = array.shape[2]
        if channels == 3:
            return np.ascontiguousarray(array, dtype=np.uint8)
        if channels == 4:
            return np.ascontiguousarray(array[:, :, :3], dtype=np.uint8)
        if channels == 1:
            return np.ascontiguousarray(
                np.repeat(array, 3, axis=-1),
                dtype=np.uint8,
            )

    raise ValueError(f"Unsupported array shape for RGB conversion: {array.shape}")


def read_task_images(
    task: LithologyTask,
    *,
    load_legend: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Read the column strip slice and legend crop directly through tifffile + zarr."""
    image_path = Path(task.image_path)
    if not image_path.is_file():
        raise FileNotFoundError(f"Well log image not found: {image_path}")

    col_left, _, col_right, col_bottom = task.column_bbox
    leg_left, leg_top, leg_right, leg_bottom = task.legend_bbox

    with tifffile.TiffFile(image_path) as tif:
        page = tif.pages[0]
        colormap = getattr(page, "colormap", None)

        store = tif.aszarr()
        root = zarr.open(store, mode="r")
        zarr_array = root if hasattr(root, "shape") else (root[0] if 0 in root else root["0"])

        img_height, img_width = zarr_array.shape[0], zarr_array.shape[1]
        if (
            col_right > img_width
            or col_bottom > img_height
            or leg_right > img_width
            or leg_bottom > img_height
        ):
            raise ValueError("task bounding box extends outside the image")

        # Lazily slices only [task.y_start:task.y_end] (2,000 px height)
        column_slice = zarr_array[task.y_start:task.y_end, col_left:col_right]
        column_array = _normalize_to_rgb(np.asarray(column_slice), colormap=colormap)

        if load_legend:
            legend_slice = zarr_array[leg_top:leg_bottom, leg_left:leg_right]
            legend_array = _normalize_to_rgb(np.asarray(legend_slice), colormap=colormap)
        else:
            legend_array = np.empty((0, 0, 3), dtype=np.uint8)

    return column_array, legend_array


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

    cache_key = (task.log_id, task.image_path, task.legend_bbox)
    cached_legend = legend_cache.get(cache_key) if legend_cache is not None else None
    needs_legend = cached_legend is None

    # Skip decoding the legend TIFF region if already cached for this log
    column_image, legend_image = read_task_images(task, load_legend=needs_legend)

    if cached_legend is not None:
        legend = cached_legend
    else:
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
        log_id=task.log_id,
        y_start=task.y_start,
        y_end=task.y_end,
        legend=legend,
        intervals=tuple(intervals),
    )