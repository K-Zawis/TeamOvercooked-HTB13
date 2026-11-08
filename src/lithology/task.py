"""Task-payload adapter for the document-local lithology pipeline."""

from dataclasses import dataclass
from typing import MutableMapping
import warnings

lazy import numpy as np
lazy import rasterio
lazy from rasterio.enums import ColorInterp
lazy from rasterio.errors import NotGeoreferencedWarning
lazy from rasterio.windows import Window

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


def _read_rgb_window(dataset, box: Box) -> np.ndarray:
    """Decode one TIFF window and normalize its bands to contiguous RGB."""
    left, top, right, bottom = box
    window = Window(left, top, right - left, bottom - top)
    pixels = dataset.read(window=window)
    if pixels.dtype != np.uint8:
        raise ValueError(
            f"unsupported TIFF sample type {pixels.dtype}; expected 8-bit samples"
        )

    color_interpretation = tuple(dataset.colorinterp)
    if pixels.shape[0] == 1:
        values = pixels[0]
        if color_interpretation == (ColorInterp.palette,):
            palette = dataset.colormap(1)
            lookup = np.zeros((256, 3), dtype=np.uint8)
            for index, color in palette.items():
                if 0 <= index < len(lookup):
                    lookup[index] = color[:3]
            rgb = lookup[values]
        else:
            # GDAL exposes bilevel TIFFs as 0/1 uint8 samples.
            if values.size and values.max() <= 1:
                values = values * np.uint8(255)
            rgb = np.repeat(values[:, :, None], 3, axis=2)
    elif pixels.shape[0] >= 3:
        rgb = np.moveaxis(pixels[:3], 0, -1)
        if pixels.shape[0] >= 4 and ColorInterp.alpha in color_interpretation:
            alpha_index = color_interpretation.index(ColorInterp.alpha)
            alpha = pixels[alpha_index].astype(np.float32)[:, :, None] / 255.0
            rgb = np.rint(rgb.astype(np.float32) * alpha + 255.0 * (1.0 - alpha))
            rgb = rgb.astype(np.uint8)
    else:
        raise ValueError(f"unsupported TIFF band count: {pixels.shape[0]}")
    return np.ascontiguousarray(rgb, dtype=np.uint8)


def read_task_images(
    task: LithologyTask,
    *,
    include_legend: bool = True,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Decode the task's TIFF strip and, when needed, its legend window."""
    left, _, right, _ = task.column_bbox
    column_box = (left, task.y_start, right, task.y_end)
    boxes = (column_box, task.legend_bbox) if include_legend else (column_box,)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(task.image_path) as dataset:
            for box in boxes:
                if box[2] > dataset.width or box[3] > dataset.height:
                    raise ValueError("task bounding box extends outside the image")
            column = _read_rgb_window(dataset, column_box)
            legend = (
                _read_rgb_window(dataset, task.legend_bbox)
                if include_legend
                else None
            )
    return column, legend


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
    legend = legend_cache.get(cache_key) if legend_cache is not None else None
    column_image, legend_image = read_task_images(
        task,
        include_legend=legend is None,
    )
    if legend is None:
        assert legend_image is not None
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
