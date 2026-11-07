"""Consume depth-strip tasks and publish global pixel/depth anchors to Redis.

The default CLI uses an in-memory Redis stub. Inject the real Redis client into
DepthWorker and call run_forever() when broker integration is available.
"""

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import math
from pathlib import Path
import socket
import time
import uuid

import numpy as np
import tifffile
import zarr

from models import DepthExtractor
from depth_staging import commit_depth_result, staging_keys
from redis_stub import RedisClient, StubRedisClient


TASK_STREAM = "stream:depth_tasks"
CONSUMER_GROUP = "depth-workers"
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
        encoded = json.dumps([self.log_id, self.image_path, self.bbox,
                              self.y_start, self.y_end], separators=(",", ":"))
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
            raise ValueError("task requires log_id, image_path, bbox, y_start, y_end") from exc
        if not isinstance(log_id, str) or not log_id.strip():
            raise ValueError("log_id must be a nonempty string (preserve leading zeroes)")
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
            raise ValueError("requested slice must lie inside a positive, max-exclusive bbox")
        return cls(log_id, image_path, (left, top, right, bottom), start, end)


def read_depth_strip(task: DepthTask) -> np.ndarray:
    """Decode the requested first-page TIFF ROI, not a full-page NumPy array.

    TIFF strips/tiles intersecting the ROI are decoded via tifffile's Zarr store.
    Native TIFF block size still determines the minimum amount of decoding.
    """
    with tifffile.TiffFile(task.image_path) as tif:
        page = tif.pages[0]
        left, top, right, bottom = task.bbox
        if right > page.imagewidth or bottom > page.imagelength:
            raise ValueError("bbox extends outside the TIFF image")
        axes = page.axes
        if axes not in ("YX", "YXS", "SYX"):
            raise ValueError(f"unsupported TIFF page axes: {axes}")
        if page.photometric not in (0, 1, 2, 3):
            raise ValueError(f"unsupported TIFF photometric: {page.photometric}")
        selection = tuple(slice(task.y_start, task.y_end) if a == "Y" else
                          slice(left, right) if a == "X" else slice(None) for a in axes)
        store = page.aszarr(maxworkers=1)
        try:
            pixels = np.asarray(zarr.open(store, mode="r")[selection])
        finally:
            store.close()
        if axes == "SYX":
            pixels = np.moveaxis(pixels, 0, -1)
        if page.photometric == 3:  # TIFF palette entries are unsigned 16-bit RGB.
            if pixels.ndim != 2 or page.colormap is None:
                raise ValueError("invalid paletted TIFF")
            pixels = (page.colormap[:, pixels.astype(np.intp)].transpose(1, 2, 0) >> 8).astype(np.uint8)
        else:
            if pixels.dtype == np.bool_:
                pixels = pixels.astype(np.uint8) * 255
            if pixels.dtype != np.uint8:
                raise ValueError(f"unsupported TIFF sample type: {pixels.dtype}; expected 8-bit RGB/grayscale")
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
                pixels = np.clip((rgb if associated else rgb * alpha) + 255 * (1-alpha), 0, 255).astype(np.uint8)
        if pixels.shape != (task.y_end-task.y_start, right-left, 3):
            raise ValueError(f"unsupported TIFF channel layout: {pixels.shape}")
        return np.ascontiguousarray(pixels)


class DepthWorker:
    def __init__(self, redis_client: RedisClient, extractor=None, *,
                 consumer_name=None, group=CONSUMER_GROUP, context_rows=128):
        if isinstance(context_rows, bool) or not isinstance(context_rows, int) or context_rows < 0:
            raise ValueError("context_rows must be a nonnegative integer")
        self.redis = redis_client
        self.extractor = extractor if extractor is not None else DepthExtractor()
        self.consumer_name = consumer_name or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.group = group
        self.context_rows = context_rows
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
        context = replace(task, y_start=max(task.bbox[1], task.y_start-self.context_rows),
                          y_end=min(task.bbox[3], task.y_end+self.context_rows))
        pixels = read_depth_strip(context)
        local = self.extractor.extract_observations(pixels)["y_to_depth_map"]
        if any(isinstance(y, bool) or not isinstance(y, (int, np.integer))
               or not 0 <= y < len(pixels) or not math.isfinite(depth)
               for y, depth in local.items()):
            raise ValueError("extractor produced an invalid depth observation")
        # Adjacent tasks may see the same context; exactly one owns each centre.
        global_map = {context.y_start + int(y): depth for y, depth in local.items()
                      if task.y_start <= context.y_start + y < task.y_end}
        committed = commit_depth_result(self.redis, task.log_id, task.task_id, global_map)
        return global_map if committed else {}

    def run_once(self, *, block_ms=1000) -> bool:
        """Retry own pending work before reading one new task. Raise on failures."""
        entries = self.redis.xreadgroup(self.group, self.consumer_name,
                                       {TASK_STREAM: "0"}, count=1)
        messages = [entry for _, rows in (entries or []) for entry in rows]
        if not messages:
            entries = self.redis.xreadgroup(self.group, self.consumer_name,
                                           {TASK_STREAM: ">"}, count=1, block=block_ms)
            messages = [entry for _, rows in (entries or []) for entry in rows]
        if not messages:
            return False
        message_id, fields = messages[0]
        try:
            task = DepthTask.from_fields(fields)
            result = self.process_task(task)
            self.redis.xack(TASK_STREAM, self.group, message_id)
        except Exception:
            logger.exception("Depth task %r failed; left pending for retry", message_id)
            raise
        logger.info("Depth task %r: log=%s rows=%s:%s anchors=%s",
                    message_id, task.log_id, task.y_start, task.y_end, len(result))
        return True

    def run_forever(self):
        try:
            while True:
                try:
                    self.run_once()
                except Exception:
                    # Failed/poison messages stay pending. Back off; recovery and
                    # dead-letter policy belong to the eventual broker adapter.
                    time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Depth worker stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path,
                        help="JSON array of tasks to enqueue in the in-memory Redis stub")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    client = StubRedisClient()
    logger.warning("Using in-memory Redis stub; no external broker is connected")
    if args.tasks is None:
        DepthWorker(client).run_forever()
        return
    tasks = json.loads(args.tasks.read_text())
    if not isinstance(tasks, list):
        parser.error("--tasks must contain a JSON array")
    for task in tasks:
        client.xadd(TASK_STREAM, {"data": json.dumps(task)})
    worker = DepthWorker(client, consumer_name="stub-depth-worker")
    while worker.run_once(block_ms=1):
        pass
    print(json.dumps(dict(client.hashes), indent=2))


if __name__ == "__main__":
    main()
