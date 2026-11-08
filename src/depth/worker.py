"""Consume depth-strip tasks and publish global pixel/depth anchors to Redis.

Uses src.redis_client.PipelineRedisClient for the central Redis broker.
Configuration comes from REDIS_HOST, REDIS_PORT, REDIS_DB, CONSUMER_NAME,
and SLICE_HEIGHT (the maximum number of owned rows per OCR input).
"""

import hashlib
import json
import logging
import math
import socket
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from os import getenv
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from numpy.typing import NDArray

lazy import numpy as np
lazy import tifffile
lazy import zarr

from src.redis_client import PipelineRedisClient

from .staging import commit_depth_result, staging_keys
lazy from .extraction import DepthExtractor

TASK_STREAM = PipelineRedisClient.STREAM_DEPTH
CONSUMER_GROUP = PipelineRedisClient.GROUP_DEPTH
logger = logging.getLogger(__name__)


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


@dataclass(frozen=True)
class DepthTask:
    log_id: str
    image_path: str
    bbox: tuple[int, int, int, int]
    y_start: int
    y_end: int

    @property
    def task_id(self):
        """Stable across consumer changes and duplicate stream dispatches."""
        encoded = json.dumps(
            [self.log_id, self.image_path, self.bbox, self.y_start, self.y_end],
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_fields(cls, fields):
        """Accept a JSON task in `data`, or the same fields directly in the stream."""
        fields = {_text(k): _text(v) for k, v in fields.items()}
        payload = json.loads(fields["data"]) if "data" in fields else fields
        if not isinstance(payload, dict):
            raise ValueError("task must be a JSON object")
        try:
            log_id, image_path = payload["log_id"], payload["image_path"]
            bbox = payload["bbox"]
            if isinstance(bbox, str):
                bbox = json.loads(bbox)
            coordinates = [*bbox, payload["y_start"], payload["y_end"]]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "task requires log_id, image_path, bbox, y_start, y_end"
            ) from exc
        if not isinstance(log_id, str) or not log_id.strip():
            raise ValueError(
                "log_id must be a nonempty string (preserve leading zeroes)"
            )
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("image_path must be a nonempty string")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError("bbox must be [left, top, right, bottom]")
        parsed = []
        for value in coordinates:
            # Flat Redis stream fields are strings; JSON numbers must be integers.
            if isinstance(value, str) and value.isdecimal():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("pixel coordinates must be integers")
            parsed.append(value)
        left, top, right, bottom, start, end = parsed
        if not (0 <= left < right and 0 <= top <= start < end <= bottom):
            raise ValueError(
                "requested slice must lie inside a positive, max-exclusive bbox"
            )
        return cls(log_id, image_path, (left, top, right, bottom), start, end)


@dataclass(frozen=True)
class DepthStrip:
    """One RGB crop with context and its owned global half-open row range."""

    # Python 3.15 dataclasses also resolves dotted string annotations. An alias
    # imported only for static checking keeps NumPy unloaded during startup.
    pixels: "NDArray"
    y_offset: int
    y_start: int
    y_end: int


@contextmanager
def _open_depth_image(task: DepthTask):
    """Validate the first page and close both resources on every exit path."""
    with tifffile.TiffFile(task.image_path) as tif:
        page = tif.pages[0]
        _, _, right, bottom = task.bbox
        if right > page.imagewidth or bottom > page.imagelength:
            raise ValueError("bbox extends outside the TIFF image")
        if page.axes not in ("YX", "YXS", "SYX"):
            raise ValueError(f"unsupported TIFF page axes: {page.axes}")
        if page.photometric not in (0, 1, 2, 3):
            raise ValueError(f"unsupported TIFF photometric: {page.photometric}")
        store = page.aszarr(maxworkers=1)
        try:
            yield page, zarr.open(store, mode="r")
        finally:
            store.close()


def _read_rgb_window(page, image, left, right, y_start, y_end) -> np.ndarray:
    """Decode and normalize only the selected window, sharing format handling."""
    selection = tuple(
        slice(y_start, y_end) if axis == "Y" else
        slice(left, right) if axis == "X" else slice(None)
        for axis in page.axes
    )
    pixels = np.asarray(image[selection])
    if page.axes == "SYX":
        pixels = np.moveaxis(pixels, 0, -1)
    if page.photometric == 3:  # TIFF palette entries are unsigned 16-bit RGB.
        if pixels.ndim != 2 or page.colormap is None:
            raise ValueError("invalid paletted TIFF")
        pixels = (
            page.colormap[:, pixels.astype(np.intp)].transpose(1, 2, 0) >> 8
        ).astype(np.uint8)
    else:
        if pixels.dtype == np.bool_:
            pixels = pixels.astype(np.uint8) * 255
        if pixels.dtype != np.uint8:
            raise ValueError(
                f"unsupported TIFF sample type: {pixels.dtype}; expected 8-bit RGB/grayscale"
            )
        if page.photometric == 0:  # MINISWHITE: zero is white.
            pixels = 255 - pixels
        if pixels.ndim == 2:
            pixels = np.repeat(pixels[:, :, None], 3, axis=2)
        elif pixels.shape[2] == 1:
            pixels = np.repeat(pixels, 3, axis=2)
        elif pixels.shape[2] == 4:
            # Composite alpha onto white, including associated-alpha TIFFs.
            alpha = pixels[:, :, 3:4].astype(np.float32) / 255
            rgb = pixels[:, :, :3].astype(np.float32)
            associated = bool(page.extrasamples and page.extrasamples[0] == 1)
            pixels = np.clip(
                (rgb if associated else rgb * alpha) + 255 * (1 - alpha), 0, 255
            ).astype(np.uint8)
    if pixels.shape != (y_end - y_start, right - left, 3):
        raise ValueError(f"unsupported TIFF channel layout: {pixels.shape}")
    return np.ascontiguousarray(pixels)


def read_depth_strip(task: DepthTask) -> np.ndarray:
    """Decode exactly the requested first-page TIFF ROI as one RGB array."""
    with _open_depth_image(task) as (page, image):
        return _read_rgb_window(page, image, task.bbox[0], task.bbox[2],
                                task.y_start, task.y_end)


def _validate_strip_options(strip_height, context_rows):
    if isinstance(strip_height, bool) or not isinstance(strip_height, int) or strip_height <= 0:
        raise ValueError("strip_height must be a positive integer")
    if isinstance(context_rows, bool) or not isinstance(context_rows, int) or context_rows < 0:
        raise ValueError("context_rows must be a nonnegative integer")


def iter_depth_strips(task: DepthTask, strip_height=2000, context_rows=128):
    """Yield bounded RGB windows, opening the TIFF/store once for this task.

    Each crop includes context clamped to the column bounds. Only observations
    inside its owned [y_start, y_end) range belong to the task result. Consumers
    must release each DepthStrip before requesting the next; close the iterator
    explicitly if stopping early. Native TIFF block size bounds decoder granularity.
    """
    _validate_strip_options(strip_height, context_rows)
    with _open_depth_image(task) as (page, image):
        for start in range(task.y_start, task.y_end, strip_height):
            end = min(start + strip_height, task.y_end)
            top = max(task.bbox[1], start - context_rows)
            bottom = min(task.bbox[3], end + context_rows)
            pixels = _read_rgb_window(page, image, task.bbox[0], task.bbox[2], top, bottom)
            try:
                yield DepthStrip(pixels, top, start, end)
            finally:
                del pixels


class DepthWorker:
    def __init__(
        self,
        redis_client: PipelineRedisClient,
        extractor=None,
        *,
        consumer_name=None,
        group=CONSUMER_GROUP,
        context_rows=128,
        strip_height=2000,
    ):
        _validate_strip_options(strip_height, context_rows)
        self.client = redis_client
        self.redis = self.client.r
        self.client.init_consumer_groups()
        self.extractor = extractor if extractor is not None else DepthExtractor()
        self.consumer_name = (
            consumer_name or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        )
        self.group = group
        self.context_rows = context_rows
        self.strip_height = strip_height
        try:
            # Start at the backlog, not just messages arriving after startup.
            self.redis.xgroup_create(TASK_STREAM, group, id="0-0", mkstream=True)
        except Exception as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise

    def process_task(self, task: DepthTask) -> dict[int, float]:
        """Independently extract, stage and count one logical task exactly once."""
        if self.redis.sismember(staging_keys(task.log_id)[2], task.task_id):
            return {}
        global_map = {}
        with closing(iter_depth_strips(task, self.strip_height, self.context_rows)) as strips:
            for strip in strips:
                try:
                    local = self.extractor.extract_observations(strip.pixels)["y_to_depth_map"]
                    if any(
                        isinstance(y, bool)
                        or not isinstance(y, (int, np.integer))
                        or not 0 <= y < len(strip.pixels)
                        or not math.isfinite(depth)
                        for y, depth in local.items()
                    ):
                        raise ValueError("extractor produced an invalid depth observation")
                    global_map.update({
                        strip.y_offset + int(y): depth for y, depth in local.items()
                        if strip.y_start <= strip.y_offset + y < strip.y_end
                    })
                finally:
                    del strip
        committed = commit_depth_result(
            self.redis,
            task.log_id,
            task.task_id,
            global_map,
            slice_bounds=(task.y_start, task.y_end),
        )
        return global_map if committed else {}

    def run_once(self, *, block_ms=1000) -> bool:
        """Retry own pending work before reading one new task. Raise on failures."""
        entries = self.redis.xreadgroup(
            self.group, self.consumer_name, {TASK_STREAM: "0"}, count=1
        )
        messages = [entry for _, rows in (entries or []) for entry in rows]
        if not messages:
            entries = self.redis.xreadgroup(
                self.group,
                self.consumer_name,
                {TASK_STREAM: ">"},
                count=1,
                block=block_ms,
            )
            messages = [entry for _, rows in (entries or []) for entry in rows]
        if not messages:
            return False
        message_id, fields = messages[0]
        try:
            task = DepthTask.from_fields(fields)
            result = self.process_task(task)
            if self.group == self.client.GROUP_DEPTH:
                # Results/counters/slice markers are already durable before this
                # method acknowledges and evaluates the shared merge barrier.
                self.client.complete_depth_chunk(
                    task.log_id,
                    message_id,
                    task.image_path,
                    None,
                    slice_bounds=(task.y_start, task.y_end),
                )
            else:
                self.client.ack_task(TASK_STREAM, self.group, message_id)
        except Exception:
            logger.exception("Depth task %r failed; left pending for retry", message_id)
            raise
        logger.info(
            "Depth task %r: log=%s rows=%s:%s anchors=%s",
            message_id,
            task.log_id,
            task.y_start,
            task.y_end,
            len(result),
        )
        return True

    def run_forever(self):
        idle = False
        try:
            while True:
                try:
                    if self.run_once():
                        idle = False
                    elif not idle:
                        logger.info("No depth tasks available; waiting for the next batch.")
                        idle = True
                except Exception:
                    # Failed/poison messages stay pending. Back off; recovery and
                    # dead-letter policy belong to the eventual broker adapter.
                    time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Depth worker stopped")


def main():
    """Start the environment-configured worker and poll dispatched tasks forever."""
    logging.basicConfig(level=logging.INFO)
    client = PipelineRedisClient(
        host=getenv("REDIS_HOST", "172.28.0.2"),
        port=int(getenv("REDIS_PORT", "6379")),
        db=int(getenv("REDIS_DB", "0")),
    )
    worker = DepthWorker(
        client,
        consumer_name=getenv("CONSUMER_NAME", "DepthWorker"),
        strip_height=int(getenv("SLICE_HEIGHT", "2000")),
    )
    worker.run_forever()


if __name__ == "__main__":
    main()
