import os
import json
import redis
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List


class LogStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PipelineRedisClient:
    """
    High-level Redis interface for task streaming, state tracking,
    and intermediate result storage.
    """

    STREAM_DEPTH = "stream:depth_tasks"
    STREAM_LITH = "stream:lith_tasks"
    STREAM_MERGE = "stream:merge_tasks"

    GROUP_DEPTH = "group:depth_workers"
    GROUP_LITH = "group:lith_workers"
    GROUP_MERGE = "group:merger"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        db: int = 0,
        connection: Optional[redis.Redis] = None,
    ) -> None:
        self.host = host or os.getenv("REDIS_HOST", "localhost")
        self.port = int(port or os.getenv("REDIS_PORT", 6379))
        self.r = connection if connection is not None else redis.Redis(
            host=self.host,
            port=self.port,
            db=db,
            decode_responses=True,
        )

    def init_consumer_groups(self) -> None:
        """Creates required Redis streams and consumer groups idempotently."""
        mappings = [
            (self.STREAM_DEPTH, self.GROUP_DEPTH),
            (self.STREAM_LITH, self.GROUP_LITH),
            (self.STREAM_MERGE, self.GROUP_MERGE),
        ]
        for stream, group in mappings:
            try:
                # mkstream=True automatically creates stream if it doesn't exist
                self.r.xgroup_create(stream, group, id="0", mkstream=True)
            except redis.exceptions.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise e

    # -------------------------------------------------------------------------
    # Stream Task Producers
    # -------------------------------------------------------------------------
    def push_depth_task(
        self,
        log_id: str,
        image_path: str | Path,
        bbox: List[int],
        y_start: int,
        y_end: int,
    ) -> str:
        """Pushes a depth OCR task with explicit typed parameters."""
        payload = {
            "log_id": log_id,
            "image_path": str(image_path),
            "bbox": bbox,
            "y_start": y_start,
            "y_end": y_end,
        }
        return self.r.xadd(self.STREAM_DEPTH, {"data": json.dumps(payload)})

    def push_lith_task(
        self,
        log_id: str,
        image_path: str | Path,
        column_bbox: List[int],
        legend_bbox: Optional[List[int]],
        y_start: int,
        y_end: int,
    ) -> str:
        """Pushes a lithology chunk task with explicit typed parameters."""
        payload = {
            "log_id": log_id,
            "image_path": str(image_path),
            "column_bbox": column_bbox,
            "legend_bbox": legend_bbox,
            "y_start": y_start,
            "y_end": y_end,
        }
        return self.r.xadd(self.STREAM_LITH, {"data": json.dumps(payload)})

    def push_merge_task(
        self,
        log_id: str,
        image_path: str | Path,
        column_bbox: Optional[List[int]] = None,
    ) -> str:
        """Notifies the merger that all chunks for a log are dispatched/ready."""
        payload = {
            "log_id": log_id,
            "image_path": str(image_path),
            "column_bbox": column_bbox,
        }
        return self.r.xadd(self.STREAM_MERGE, {"data": json.dumps(payload)})

    # -------------------------------------------------------------------------
    # Stream Task Consumers
    # -------------------------------------------------------------------------
    def read_task(
        self, stream: str, group: str, consumer_name: str, block_ms: int = 2000
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Reads one pending or new task from a consumer group.
        Returns (message_id, parsed_payload_dict) or None if timeout.
        """
        messages = self.r.xreadgroup(group, consumer_name, {stream: "0"}, count=1)
        stream_messages = [entry for _, entries in (messages or []) for entry in entries]
        if not stream_messages:
            messages = self.r.xreadgroup(group, consumer_name, {stream: ">"},
                                        count=1, block=block_ms)
            stream_messages = [entry for _, entries in (messages or []) for entry in entries]
        if not stream_messages:
            return None

        msg_id, fields = stream_messages[0]
        payload = json.loads(fields.get("data", fields.get(b"data")))
        return msg_id, payload

    def ack_task(self, stream: str, group: str, msg_id: str) -> None:
        """Confirms successful task completion to clear it from the PEL."""
        self.r.xack(stream, group, msg_id)

    # -------------------------------------------------------------------------
    # Document State & Progress Tracking
    # -------------------------------------------------------------------------
    def set_document_meta(
        self,
        log_id: str,
        total_depth: int,
        total_lith: int,
        image_name: str,
        status: LogStatus = LogStatus.PROCESSING,
    ) -> None:
        if any(isinstance(total, bool) or not isinstance(total, int) or total < 0
               for total in (total_depth, total_lith)):
            raise ValueError("chunk totals must be nonnegative integers")
        # New-run initialization: do this before dispatch, with old workers/tasks
        # stopped/drained. Completion markers belong to the same run as results.
        prefix = f"log:{log_id}"
        with self.r.pipeline(transaction=True) as pipeline:
            pipeline.delete(f"{prefix}:meta", f"{prefix}:depths",
                            f"{prefix}:depth_completed_tasks", f"{prefix}:raw_intervals",
                            f"{prefix}:legend")
            pipeline.hset(f"{prefix}:meta", mapping={
                "image_name": image_name,
                "total_depth_chunks": total_depth,
                "completed_depth_chunks": 0,
                "completed_depth": 0,
                "total_lith_chunks": total_lith,
                "completed_lith_chunks": 0,
                "status": status.value,
            })
            pipeline.execute()

    def set_document_status(self, log_id: str, status: LogStatus) -> None:
        """Atomically updates the document processing lifecycle stage."""
        self.r.hset(f"log:{log_id}:meta", "status", status.value)

    def get_document_status(self, log_id: str) -> Optional[LogStatus]:
        """Reads and validates the status against the LogStatus enum."""
        raw_status = self.r.hget(f"log:{log_id}:meta", "status")
        if raw_status is None:
            return None
        return LogStatus(raw_status)

    # -------------------------------------------------------------------------
    # Worker Result Storage (Used by Workers & Merger)
    # -------------------------------------------------------------------------
    def save_depth_points(self, log_id: str, y_to_depth_map: Dict[int, float]) -> None:
        """Stores detected Y-coordinate -> depth value pairs."""
        if y_to_depth_map:
            str_map = {str(k): str(v) for k, v in y_to_depth_map.items()}
            self.r.hset(f"log:{log_id}:depths", mapping=str_map)

    def append_raw_interval(self, log_id: str, interval: Dict[str, Any]) -> None:
        """Pushes an identified interval prediction to the document list."""
        self.r.rpush(f"log:{log_id}:raw_intervals", json.dumps(interval))

    def save_legend_map(self, log_id: str, legend_mapping: Dict[str, str]) -> None:
        """Stores cluster_id -> lithology_name mappings."""
        if legend_mapping:
            self.r.hset(f"log:{log_id}:legend", mapping=legend_mapping)
