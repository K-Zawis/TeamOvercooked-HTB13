import os
import json
lazy import redis
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List, Set, Generator


class LogStatus(str, Enum):
    """Lifecycle statuses for a geological well log document."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CORRUPTED = "CORRUPTED"


class PipelineRedisClient:
    """High-level Redis interface for task streaming, event broadcasting, and ML data storage."""

    STREAM_DEPTH = "stream:depth_tasks"
    STREAM_LITH = "stream:lith_tasks"
    STREAM_MERGE = "stream:merge_tasks"
    STREAM_EVENTS = "stream:events"

    GROUP_DEPTH = "group:depth_workers"
    GROUP_LITH = "group:lith_workers"
    GROUP_MERGE = "group:merger"

    KEY_PIPELINE_STATE = "pipeline:state"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        db: int = 0,
    ) -> None:
        """Initializes connection to the Redis server.

        Args:
            host (Optional[str], optional): Redis host address. Reads REDIS_HOST env if not provided. Defaults to None.
            port (Optional[int], optional): Redis port number. Reads REDIS_PORT env if not provided. Defaults to None.
            db (int, optional): Redis database index. Defaults to 0.
        """
        self.host = host or os.getenv("REDIS_HOST", "localhost")
        self.port = int(port or os.getenv("REDIS_PORT", 6379))
        self.r = redis.Redis(
            host=self.host,
            port=self.port,
            db=db,
            decode_responses=True,
        )

    def init_consumer_groups(self) -> None:
        """Creates required Redis task streams and consumer groups idempotently.

        Raises:
            redis.exceptions.ResponseError: If stream creation fails with an unexpected error other than BUSYGROUP.
        """
        mappings = [
            (self.STREAM_DEPTH, self.GROUP_DEPTH),
            (self.STREAM_LITH, self.GROUP_LITH),
            (self.STREAM_MERGE, self.GROUP_MERGE),
        ]
        for stream, group in mappings:
            try:
                self.r.xgroup_create(stream, group, id="0", mkstream=True)
            except redis.exceptions.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise e

    # -------------------------------------------------------------------------
    # PERSISTENT PIPELINE ORCHESTRATION STATE
    # -------------------------------------------------------------------------

    def save_pipeline_state(
        self,
        mode: str,
        is_running: bool,
        current_batch: int,
        total_batches: int,
        batch_size: int,
        target_file: Optional[str] = None,
    ) -> None:
        """Stores global orchestrator execution parameters into Redis.

        Args:
            mode (str): Active operational mode string ('IDLE', 'LIVE', 'BATCH').
            is_running (bool): Execution flag indicating an active background orchestrator loop.
            current_batch (int): Current 1-based index of batch execution.
            total_batches (int): Total batch count scheduled for the current pipeline run.
            batch_size (int): Number of well logs partitioned into each sequential batch.
            target_file (Optional[str], optional): Target file identifier if executing in LIVE single-log mode. Defaults to None.
        """
        self.r.hset(
            self.KEY_PIPELINE_STATE,
            mapping={
                "mode": mode,
                "is_running": "1" if is_running else "0",
                "current_batch": current_batch,
                "total_batches": total_batches,
                "batch_size": batch_size,
                "target_file": target_file or "",
            },
        )

    def get_pipeline_state(self) -> Dict[str, Any]:
        """Recovers global orchestrator execution state from Redis.

        Returns:
            Dict[str, Any]: Parsed execution parameters including mode, is_running, current_batch, total_batches, batch_size, and target_file.
        """
        state = self.r.hgetall(self.KEY_PIPELINE_STATE)
        if not state:
            return {
                "mode": "IDLE",
                "is_running": False,
                "current_batch": 0,
                "total_batches": 0,
                "batch_size": 5,
                "target_file": None,
            }
        return {
            "mode": state.get("mode", "IDLE"),
            "is_running": state.get("is_running") == "1",
            "current_batch": int(state.get("current_batch", 0)),
            "total_batches": int(state.get("total_batches", 0)),
            "batch_size": int(state.get("batch_size", 5)),
            "target_file": state.get("target_file") or None,
        }

    def clear_pipeline_state(self) -> None:
        """Resets the global orchestrator state back to IDLE."""
        self.save_pipeline_state("IDLE", False, 0, 0, 5, None)

    # -------------------------------------------------------------------------
    # ATOMIC PIPELINED DISPATCH & STREAM RECONCILIATION
    # -------------------------------------------------------------------------

    def dispatch_log_atomic(
        self,
        log_id: str,
        image_name: str,
        image_path: str | Path,
        image_sha256: str,
        json_sha256: str,
        depth_bbox: List[int],
        lith_bbox: List[int],
        legend_bbox: Optional[List[int]],
        depth_slices: List[Tuple[int, int]],
        lith_slices: List[Tuple[int, int]],
    ) -> None:
        """Atomically stores metadata, slice sets, and enqueues all depth and lith tasks in one transaction.

        Args:
            log_id (str): Unique identifier for the well log document.
            image_name (str): Original filename of the TIFF image.
            image_path (str | Path): Absolute or relative filesystem path to the TIFF image.
            image_sha256 (str): SHA-256 cryptographic digest of the TIFF image file.
            json_sha256 (str): SHA-256 cryptographic digest of the annotation JSON file.
            depth_bbox (List[int]): Depth column bounding box in [x0, y0, x1, y1] format.
            lith_bbox (List[int]): Lithology column bounding box in [x0, y0, x1, y1] format.
            legend_bbox (Optional[List[int]]): Legend column bounding box in [x0, y0, x1, y1] format, if available.
            depth_slices (List[Tuple[int, int]]): List of (y_start, y_end) coordinate pairs for depth OCR slices.
            lith_slices (List[Tuple[int, int]]): List of (y_start, y_end) coordinate pairs for lithology slices.
        """
        pipe = self.r.pipeline(transaction=True)

        meta_key = f"log:{log_id}:meta"
        pipe.hset(
            meta_key,
            mapping={
                "image_name": image_name,
                "image_sha256": image_sha256,
                "json_sha256": json_sha256,
                "total_depth_chunks": len(depth_slices),
                "completed_depth_chunks": 0,
                "total_lith_chunks": len(lith_slices),
                "completed_lith_chunks": 0,
                "status": LogStatus.PROCESSING.value,
            },
        )

        all_depth_keys = [f"{y0}:{y1}" for y0, y1 in depth_slices]
        all_lith_keys = [f"{y0}:{y1}" for y0, y1 in lith_slices]

        if all_depth_keys:
            pipe.sadd(f"log:{log_id}:slices:depth:all", *all_depth_keys)
        if all_lith_keys:
            pipe.sadd(f"log:{log_id}:slices:lith:all", *all_lith_keys)

        for y_start, y_end in depth_slices:
            payload = {
                "log_id": log_id,
                "image_path": str(image_path),
                "bbox": depth_bbox,
                "y_start": y_start,
                "y_end": y_end,
            }
            pipe.xadd(self.STREAM_DEPTH, {"data": json.dumps(payload)})

        for y_start, y_end in lith_slices:
            payload = {
                "log_id": log_id,
                "image_path": str(image_path),
                "column_bbox": lith_bbox,
                "legend_bbox": legend_bbox,
                "y_start": y_start,
                "y_end": y_end,
            }
            pipe.xadd(self.STREAM_LITH, {"data": json.dumps(payload)})

        event_payload = {
            "log_id": log_id,
            "status": LogStatus.PROCESSING.value,
            "depth_chunks": len(depth_slices),
            "lith_chunks": len(lith_slices),
        }
        pipe.xadd(self.STREAM_EVENTS, {"data": json.dumps(event_payload)})

        pipe.execute()

    def get_stream_group_backlog(self, stream: str, group: str) -> Dict[str, int]:
        """Inspects stream consumer group lag and Pending Entries List (PEL) count.

        Args:
            stream (str): Stream key to inspect.
            group (str): Consumer group name assigned to the stream.

        Returns:
            Dict[str, int]: Dictionary containing integer counts for 'lag' and 'pending'.
        """
        try:
            groups = self.r.xinfo_groups(stream)
            for g in groups:
                if g.get("name") == group:
                    lag = g.get("lag")
                    if lag is None:
                        lag = 0
                    return {
                        "lag": int(lag),
                        "pending": int(g.get("pending", 0)),
                    }
        except redis.exceptions.ResponseError:
            pass
        return {"lag": 0, "pending": 0}

    def get_missing_slices(
        self,
        log_id: str,
        expected_depth_slices: List[Tuple[int, int]],
        expected_lith_slices: List[Tuple[int, int]],
    ) -> Dict[str, List[Tuple[int, int]]]:
        """Compares expected slices against completed slice sets to identify unfulfilled chunks.

        Args:
            log_id (str): Target well log identifier.
            expected_depth_slices (List[Tuple[int, int]]): Full list of expected (y_start, y_end) depth slices.
            expected_lith_slices (List[Tuple[int, int]]): Full list of expected (y_start, y_end) lithology slices.

        Returns:
            Dict[str, List[Tuple[int, int]]]: Dictionary containing lists of missing (y_start, y_end) slices under 'missing_depth' and 'missing_lith'.
        """
        completed_depth: Set[str] = (
            self.r.smembers(f"log:{log_id}:slices:depth:completed") or set()
        )
        completed_lith: Set[str] = (
            self.r.smembers(f"log:{log_id}:slices:lith:completed") or set()
        )

        missing_depth = [
            (y0, y1)
            for y0, y1 in expected_depth_slices
            if f"{y0}:{y1}" not in completed_depth
        ]
        missing_lith = [
            (y0, y1)
            for y0, y1 in expected_lith_slices
            if f"{y0}:{y1}" not in completed_lith
        ]

        return {
            "missing_depth": missing_depth,
            "missing_lith": missing_lith,
        }

    def re_enqueue_missing_slices(
        self,
        log_id: str,
        image_path: str | Path,
        depth_bbox: List[int],
        lith_bbox: List[int],
        legend_bbox: Optional[List[int]],
        missing_depth: List[Tuple[int, int]],
        missing_lith: List[Tuple[int, int]],
    ) -> Dict[str, int]:
        """Atomically re-enqueues only unfulfilled slices into the respective streams.

        Args:
            log_id (str): Target well log identifier.
            image_path (str | Path): Absolute or relative filesystem path to the TIFF image.
            depth_bbox (List[int]): Depth column bounding box in [x0, y0, x1, y1] format.
            lith_bbox (List[int]): Lithology column bounding box in [x0, y0, x1, y1] format.
            legend_bbox (Optional[List[int]]): Legend column bounding box in [x0, y0, x1, y1] format, if available.
            missing_depth (List[Tuple[int, int]]): Coordinates of missing depth slices to re-enqueue.
            missing_lith (List[Tuple[int, int]]): Coordinates of missing lithology slices to re-enqueue.

        Returns:
            Dict[str, int]: Counts of tasks re-enqueued under 're_enqueued_depth' and 're_enqueued_lith'.
        """
        pipe = self.r.pipeline(transaction=True)

        for y_start, y_end in missing_depth:
            payload = {
                "log_id": log_id,
                "image_path": str(image_path),
                "bbox": depth_bbox,
                "y_start": y_start,
                "y_end": y_end,
            }
            pipe.xadd(self.STREAM_DEPTH, {"data": json.dumps(payload)})

        for y_start, y_end in missing_lith:
            payload = {
                "log_id": log_id,
                "image_path": str(image_path),
                "column_bbox": lith_bbox,
                "legend_bbox": legend_bbox,
                "y_start": y_start,
                "y_end": y_end,
            }
            pipe.xadd(self.STREAM_LITH, {"data": json.dumps(payload)})

        pipe.execute()
        return {
            "re_enqueued_depth": len(missing_depth),
            "re_enqueued_lith": len(missing_lith),
        }

    # -------------------------------------------------------------------------
    # TASK PRODUCERS (Single-Task Utilities)
    # -------------------------------------------------------------------------

    def push_depth_task(
        self,
        log_id: str,
        image_path: str | Path,
        bbox: List[int],
        y_start: int,
        y_end: int,
    ) -> str:
        """Enqueues an OCR task for a 2000px depth column slice.

        Args:
            log_id (str): Unique identifier for the well log document.
            image_path (str | Path): Absolute or relative filesystem path to the TIFF image.
            bbox (List[int]): Depth column bounding box in [x0, y0, x1, y1] format.
            y_start (int): Top global pixel boundary of the slice.
            y_end (int): Bottom global pixel boundary of the slice.

        Returns:
            str: Generated Redis stream entry ID.
        """
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
        """Enqueues a lithology pattern segmentation and classification task for a slice.

        Args:
            log_id (str): Unique identifier for the well log document.
            image_path (str | Path): Absolute or relative filesystem path to the TIFF image.
            column_bbox (List[int]): Lithology column bounding box in [x0, y0, x1, y1] format.
            legend_bbox (Optional[List[int]]): Legend column bounding box in [x0, y0, x1, y1] format, if available.
            y_start (int): Top global pixel boundary of the slice.
            y_end (int): Bottom global pixel boundary of the slice.

        Returns:
            str: Generated Redis stream entry ID.
        """
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
        """Enqueues a final reduction/merging task when all document slices finish processing.

        Args:
            log_id (str): Unique identifier for the well log document.
            image_path (str | Path): Path to the source TIFF image.
            column_bbox (Optional[List[int]], optional): Column bounding box required for annotated rendering. Defaults to None.

        Returns:
            str: Generated Redis stream entry ID.
        """
        payload = {
            "log_id": log_id,
            "image_path": str(image_path),
            "column_bbox": column_bbox,
        }
        return self.r.xadd(self.STREAM_MERGE, {"data": json.dumps(payload)})

    # -------------------------------------------------------------------------
    # TASK CONSUMER STREAMS (Workers & Merger)
    # -------------------------------------------------------------------------

    def listen_tasks(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        block_ms: int = 2000,
    ) -> Generator[Tuple[str, Dict[str, Any]], None, None]:
        """Streams unacknowledged tasks from a consumer group continuously as a generator.

        Args:
            stream (str): Name of the stream to consume from.
            group (str): Consumer group assigned to the stream.
            consumer_name (str): Unique identifier for the calling worker instance.
            block_ms (int, optional): Blocking timeout in milliseconds per tick. Defaults to 2000.

        Yields:
            Tuple[str, Dict[str, Any]]: Tuple of (message_id, parsed_payload_dict) on task arrival.
        """
        while True:
            messages = self.r.xreadgroup(
                groupname=group,
                consumername=consumer_name,
                streams={stream: ">"},
                count=1,
                block=block_ms,
            )
            if not messages:
                continue

            _, stream_messages = messages[0]
            if not stream_messages:
                continue

            for msg_id, fields in stream_messages:
                payload = json.loads(fields["data"])
                yield msg_id, payload

    def read_task(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        block_ms: int = 2000,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Reads a single unacknowledged task on demand without streaming.

        Args:
            stream (str): Name of the stream to read from.
            group (str): Consumer group assigned to the stream.
            consumer_name (str): Unique identifier for the calling worker instance.
            block_ms (int, optional): Blocking timeout in milliseconds. Defaults to 2000.

        Returns:
            Optional[Tuple[str, Dict[str, Any]]]: Tuple of (message_id, parsed_payload_dict), or None if queue is empty.
        """
        messages = self.r.xreadgroup(
            groupname=group,
            consumername=consumer_name,
            streams={stream: ">"},
            count=1,
            block=block_ms,
        )
        if not messages:
            return None

        _, stream_messages = messages[0]
        if not stream_messages:
            return None

        msg_id, fields = stream_messages[0]
        payload = json.loads(fields["data"])
        return msg_id, payload

    def ack_task(self, stream: str, group: str, msg_id: str) -> None:
        """Confirms successful task processing to clear the message from the Pending Entries List (PEL).

        Args:
            stream (str): Stream where the task originated.
            group (str): Consumer group that processed the task.
            msg_id (str): Redis message identifier to acknowledge.
        """
        self.r.xack(stream, group, msg_id)

    # -------------------------------------------------------------------------
    # BROADCAST EVENT STREAM (Dispatcher & Dashboard UI)
    # -------------------------------------------------------------------------

    def emit_event(
        self,
        log_id: str,
        status: LogStatus,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Publishes a document lifecycle or chunk event to the broadcast event stream.

        Args:
            log_id (str): Target well log identifier.
            status (LogStatus): Operational status reached.
            details (Optional[Dict[str, Any]], optional): Optional supplementary context dictionary. Defaults to None.

        Returns:
            str: Generated event entry ID in stream:events.
        """
        payload: Dict[str, Any] = {
            "log_id": log_id,
            "status": status.value,
        }
        if details:
            payload.update(details)
        return self.r.xadd(self.STREAM_EVENTS, {"data": json.dumps(payload)})

    def get_latest_event_id(self) -> str:
        """Fetches the newest message ID from stream:events, or '$' if empty.

        Returns:
            str: Stream ID string or '$' to target future incoming events.
        """
        last_entry = self.r.xrevrange(self.STREAM_EVENTS, "+", "-", count=1)
        return last_entry[0][0] if last_entry else "$"

    def listen_events(
        self,
        since_id: str = "$",
        block_ms: int = 2000,
    ) -> Generator[Tuple[str, Dict[str, Any]], None, None]:
        """Streams lifecycle events continuously as a generator from stream:events.

        Args:
            since_id (str, optional): Start streaming entries occurring strictly after this ID. Defaults to "$".
            block_ms (int, optional): Milliseconds to block kernel socket waiting for events. Defaults to 2000.

        Yields:
            Tuple[str, Dict[str, Any]]: Tuple of (event_id, parsed_payload_dict) on event arrival.
        """
        current_id = since_id
        while True:
            response = self.r.xread(
                streams={self.STREAM_EVENTS: current_id},
                count=10,
                block=block_ms,
            )
            if not response:
                continue

            _, entries = response[0]
            for entry_id, fields in entries:
                current_id = entry_id
                payload = json.loads(fields["data"])
                yield entry_id, payload

    # -------------------------------------------------------------------------
    # INTERMEDIATE ML DATA STORAGE & RETRIEVAL
    # -------------------------------------------------------------------------

    def save_depth_points(self, log_id: str, y_to_depth_map: Dict[int, float]) -> None:
        """Stores detected OCR depth numbers mapped to their global vertical pixel positions.

        Args:
            log_id (str): Target well log identifier.
            y_to_depth_map (Dict[int, float]): Dictionary mapping global Y pixel coordinates to measured depth values.
        """
        if y_to_depth_map:
            str_map = {str(k): str(v) for k, v in y_to_depth_map.items()}
            self.r.hset(f"log:{log_id}:depths", mapping=str_map)

    def get_depth_points(self, log_id: str) -> Dict[int, float]:
        """Retrieves all detected depth-to-pixel coordinate mappings, sorted by ascending pixel position.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            Dict[int, float]: Ordered mapping of global Y pixel coordinates to measured depth values.
        """
        raw_map = self.r.hgetall(f"log:{log_id}:depths")
        if not raw_map:
            return {}
        return {
            int(k): float(v)
            for k, v in sorted(raw_map.items(), key=lambda item: int(item[0]))
        }

    def append_raw_interval(self, log_id: str, interval: Dict[str, Any]) -> None:
        """Appends a single segmented lithological boundary interval to the document's list.

        Args:
            log_id (str): Target well log identifier.
            interval (Dict[str, Any]): Dictionary containing interval properties (top_y, bottom_y, cluster_id, confidence).
        """
        self.r.rpush(f"log:{log_id}:raw_intervals", json.dumps(interval))

    def append_raw_intervals(
        self,
        log_id: str,
        intervals: List[Dict[str, Any]],
    ) -> None:
        """Batch-appends multiple segmented lithological boundary intervals to Redis.

        Args:
            log_id (str): Target well log identifier.
            intervals (List[Dict[str, Any]]): List of interval dictionaries to append.
        """
        if not intervals:
            return
        serialized = [json.dumps(inv) for inv in intervals]
        self.r.rpush(f"log:{log_id}:raw_intervals", *serialized)

    def get_raw_intervals(self, log_id: str) -> List[Dict[str, Any]]:
        """Retrieves all segmented lithological intervals for a document, sorted by ascending top_y coordinate.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            List[Dict[str, Any]]: Chronologically sorted list of interval definitions.
        """
        raw_items = self.r.lrange(f"log:{log_id}:raw_intervals", 0, -1)
        intervals = [json.loads(item) for item in raw_items]
        intervals.sort(key=lambda x: x["top_y"])
        return intervals

    def save_legend_map(self, log_id: str, legend_mapping: Dict[str, str]) -> None:
        """Stores cluster identifier to geological rock classification mappings.

        Args:
            log_id (str): Target well log identifier.
            legend_mapping (Dict[str, str]): Mapping of cluster keys (e.g. 'cluster_0') to rock names (e.g. 'Sandstone').
        """
        if legend_mapping:
            self.r.hset(f"log:{log_id}:legend", mapping=legend_mapping)

    def get_legend_map(self, log_id: str) -> Dict[str, str]:
        """Retrieves the cluster-to-lithology classification mapping for a document.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            Dict[str, str]: Mapping of cluster identifiers to resolved rock names.
        """
        return self.r.hgetall(f"log:{log_id}:legend")

    # -------------------------------------------------------------------------
    # LIFECYCLE, METADATA & INTEGRITY CHECKING
    # -------------------------------------------------------------------------

    def set_document_meta(
        self,
        log_id: str,
        total_depth: int,
        total_lith: int,
        image_name: str,
        image_sha256: str,
        json_sha256: str,
        status: LogStatus = LogStatus.PROCESSING,
    ) -> None:
        """Initializes metadata and target chunk completion goals for a dispatched document.

        Args:
            log_id (str): Target well log identifier.
            total_depth (int): Total depth OCR slices scheduled for this file.
            total_lith (int): Total lithology segmentation slices scheduled for this file.
            image_name (str): Original filename of the TIFF image.
            image_sha256 (str): SHA-256 cryptographic digest of the TIFF image file.
            json_sha256 (str): SHA-256 cryptographic digest of the annotation JSON file.
            status (LogStatus, optional): Initial status of the log. Defaults to LogStatus.PROCESSING.
        """
        self.r.hset(
            f"log:{log_id}:meta",
            mapping={
                "image_name": image_name,
                "image_sha256": image_sha256,
                "json_sha256": json_sha256,
                "total_depth_chunks": total_depth,
                "completed_depth_chunks": 0,
                "total_lith_chunks": total_lith,
                "completed_lith_chunks": 0,
                "status": status.value,
            },
        )
        self.emit_event(log_id, status)

    def get_document_meta(self, log_id: str) -> Dict[str, Any]:
        """Fetches metadata, progress counters, and processing status for a document.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            Dict[str, Any]: Parsed metadata dictionary containing chunk totals and active LogStatus.
        """
        meta = self.r.hgetall(f"log:{log_id}:meta")
        if not meta:
            return {}
        return {
            "image_name": meta.get("image_name"),
            "image_sha256": meta.get("image_sha256"),
            "json_sha256": meta.get("json_sha256"),
            "total_depth_chunks": int(meta.get("total_depth_chunks", 0)),
            "completed_depth_chunks": int(meta.get("completed_depth_chunks", 0)),
            "total_lith_chunks": int(meta.get("total_lith_chunks", 0)),
            "completed_lith_chunks": int(meta.get("completed_lith_chunks", 0)),
            "status": LogStatus(meta.get("status", LogStatus.PENDING.value)),
        }

    def validate_data_integrity(
        self,
        log_id: str,
        current_image_sha: str,
        current_json_sha: str,
    ) -> Tuple[bool, str]:
        """Validates that recorded file fingerprints and counter bounds match Redis metadata.

        Args:
            log_id (str): Target well log identifier.
            current_image_sha (str): Computed SHA-256 hash of the TIFF image on disk.
            current_json_sha (str): Computed SHA-256 hash of the annotation JSON on disk.

        Returns:
            Tuple[bool, str]: (True, "OK") if intact, or (False, reason) if corruption or divergence is found.
        """
        meta = self.get_document_meta(log_id)
        if not meta:
            return False, "No metadata record found in Redis"

        recorded_img = meta.get("image_sha256")
        recorded_json = meta.get("json_sha256")

        if recorded_img and recorded_img != current_image_sha:
            return (
                False,
                f"Image SHA256 mismatch: recorded={recorded_img[:8]} current={current_image_sha[:8]}",
            )

        if recorded_json and recorded_json != current_json_sha:
            return (
                False,
                f"Annotation JSON SHA256 mismatch: recorded={recorded_json[:8]} current={current_json_sha[:8]}",
            )

        d_done = meta["completed_depth_chunks"]
        d_total = meta["total_depth_chunks"]
        l_done = meta["completed_lith_chunks"]
        l_total = meta["total_lith_chunks"]

        if d_done > d_total or l_done > l_total:
            return (
                False,
                f"Chunk bound overflow: depth={d_done}/{d_total}, lith={l_done}/{l_total}",
            )

        return True, "OK"

    def set_document_status(self, log_id: str, status: LogStatus) -> None:
        """Updates the operational processing lifecycle stage of a document and broadcasts an event.

        Args:
            log_id (str): Target well log identifier.
            status (LogStatus): New lifecycle status to assign.
        """
        self.r.hset(f"log:{log_id}:meta", "status", status.value)
        self.emit_event(log_id, status)

    def get_document_status(self, log_id: str) -> Optional[LogStatus]:
        """Reads and validates the current processing status of a document.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            Optional[LogStatus]: Parsed LogStatus enum value, or None if the document does not exist.
        """
        raw_status = self.r.hget(f"log:{log_id}:meta", "status")
        return LogStatus(raw_status) if raw_status else None

    def get_multiple_document_status(
        self,
        log_ids: List[str],
    ) -> Dict[str, Optional[LogStatus]]:
        """Fetches statuses for multiple documents in a single pipelined roundtrip.

        Args:
            log_ids (List[str]): List of target well log identifiers.

        Returns:
            Dict[str, Optional[LogStatus]]: Mapping of log_id to LogStatus or None if not found.
        """
        if not log_ids:
            return {}
        pipe = self.r.pipeline()
        for log_id in log_ids:
            pipe.hget(f"log:{log_id}:meta", "status")
        statuses = pipe.execute()
        return {
            log_id: LogStatus(status) if status else None
            for log_id, status in zip(log_ids, statuses)
        }

    def get_all_tracked_logs(self) -> Dict[str, Dict[str, Any]]:
        """Recovers all document metadata records stored in Redis in one pipelined call.

        Returns:
            Dict[str, Dict[str, Any]]: Mapping of log_id to parsed document metadata dictionaries.
        """
        meta_keys = self.r.keys("log:*:meta")
        if not meta_keys:
            return {}

        pipe = self.r.pipeline()
        for key in meta_keys:
            pipe.hgetall(key)
        all_meta = pipe.execute()

        results = {}
        for key, meta in zip(meta_keys, all_meta):
            parts = key.split(":")
            log_id = ":".join(parts[1:-1])
            results[log_id] = {
                "image_name": meta.get("image_name"),
                "image_sha256": meta.get("image_sha256"),
                "json_sha256": meta.get("json_sha256"),
                "total_depth_chunks": int(meta.get("total_depth_chunks", 0)),
                "completed_depth_chunks": int(meta.get("completed_depth_chunks", 0)),
                "total_lith_chunks": int(meta.get("total_lith_chunks", 0)),
                "completed_lith_chunks": int(meta.get("completed_lith_chunks", 0)),
                "status": LogStatus(meta.get("status", LogStatus.PENDING.value)),
            }
        return results

    # -------------------------------------------------------------------------
    # ATOMIC COMPLETION & BARRIER TRIGGER
    # -------------------------------------------------------------------------

    def complete_depth_chunk(
        self,
        log_id: str,
        msg_id: str,
        image_path: str | Path,
        column_bbox: List[int],
        slice_bounds: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Acknowledges a depth task, records slice completion idempotently, and triggers merge if all chunks finished.

        Args:
            log_id (str): Target well log identifier.
            msg_id (str): Redis message identifier to acknowledge.
            image_path (str | Path): Path to the source TIFF image.
            column_bbox (List[int]): Lithology bounding box required for final merging visualization.
            slice_bounds (Optional[Tuple[int, int]], optional): Coordinate bounds (y_start, y_end) of the slice. Defaults to None.

        Returns:
            bool: True if this chunk completed the barrier and triggered the merge step, False otherwise.
        """
        self.ack_task(self.STREAM_DEPTH, self.GROUP_DEPTH, msg_id)

        if slice_bounds:
            token = f"{slice_bounds[0]}:{slice_bounds[1]}"
            self.r.sadd(f"log:{log_id}:slices:depth:completed", token)
            completed = self.r.scard(f"log:{log_id}:slices:depth:completed")
            self.r.hset(f"log:{log_id}:meta", "completed_depth_chunks", completed)
        else:
            self.r.hincrby(f"log:{log_id}:meta", "completed_depth_chunks", 1)

        return self._check_and_trigger_merge(log_id, image_path, column_bbox)

    def complete_lith_chunk(
        self,
        log_id: str,
        msg_id: str,
        image_path: str | Path,
        column_bbox: List[int],
        slice_bounds: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Acknowledges a lithology task, records slice completion idempotently, and triggers merge if all chunks finished.

        Args:
            log_id (str): Target well log identifier.
            msg_id (str): Redis message identifier to acknowledge.
            image_path (str | Path): Path to the source TIFF image.
            column_bbox (List[int]): Lithology bounding box required for final merging visualization.
            slice_bounds (Optional[Tuple[int, int]], optional): Coordinate bounds (y_start, y_end) of the slice. Defaults to None.

        Returns:
            bool: True if this chunk completed the barrier and triggered the merge step, False otherwise.
        """
        self.ack_task(self.STREAM_LITH, self.GROUP_LITH, msg_id)

        if slice_bounds:
            token = f"{slice_bounds[0]}:{slice_bounds[1]}"
            self.r.sadd(f"log:{log_id}:slices:lith:completed", token)
            completed = self.r.scard(f"log:{log_id}:slices:lith:completed")
            self.r.hset(f"log:{log_id}:meta", "completed_lith_chunks", completed)
        else:
            self.r.hincrby(f"log:{log_id}:meta", "completed_lith_chunks", 1)

        return self._check_and_trigger_merge(log_id, image_path, column_bbox)

    def _check_and_trigger_merge(
        self,
        log_id: str,
        image_path: str | Path,
        column_bbox: List[int],
    ) -> bool:
        """Atomically checks if all chunks are done across both disciplines and triggers merge.

        Args:
            log_id (str): Target well log identifier.
            image_path (str | Path): Path to the source TIFF image.
            column_bbox (List[int]): Lithology bounding box required for final merging visualization.

        Returns:
            bool: True if this call triggered the merge task, False otherwise.
        """
        meta = self.r.hgetall(f"log:{log_id}:meta")
        if not meta:
            return False

        depth_done = int(meta.get("completed_depth_chunks", 0))
        depth_total = int(meta.get("total_depth_chunks", -1))
        lith_done = int(meta.get("completed_lith_chunks", 0))
        lith_total = int(meta.get("total_lith_chunks", -2))

        if depth_done >= depth_total and lith_done >= lith_total:
            current_status = meta.get("status")
            if current_status == LogStatus.PROCESSING.value:
                self.set_document_status(log_id, LogStatus.MERGING)
                self.push_merge_task(
                    log_id=log_id,
                    image_path=str(image_path),
                    column_bbox=column_bbox,
                )
                return True
        return False

    def is_log_finished(self, log_id: str) -> bool:
        """Checks if a log has finished all chunk processing stages.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            bool: True if document status is COMPLETED, False otherwise.
        """
        status = self.get_document_status(log_id)
        return status == LogStatus.COMPLETED

    def flush_all(self) -> None:
        """Flushes the active Redis database, recreates required consumer groups, and resets orchestrator state."""
        self.r.flushdb()
        self.init_consumer_groups()
        self.clear_pipeline_state()
