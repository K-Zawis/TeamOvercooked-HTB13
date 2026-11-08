import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from src.redis_client import LogStatus, PipelineRedisClient


class DispatcherMode(str, Enum):
    """Execution modes for the task dispatcher service."""

    IDLE = "IDLE"
    LIVE = "LIVE"
    BATCH = "BATCH"


def compute_sha256(path: Path) -> str:
    """Computes the SHA-256 cryptographic digest of a local filesystem file in 64KB blocks."""
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class LogDispatcher:
    """Orchestrates well log slicing, atomic streaming into Redis, state recovery, and batch backpressure."""

    def __init__(self) -> None:
        """Initializes the LogDispatcher service, binds Redis connections, and restores state."""
        self.data_dir = Path(os.getenv("DATA_DIR", "./dataset"))
        self.images_dir = self.data_dir / "images"
        self.annotations_dir = self.data_dir / "bbox_annotation"
        self.slice_height = int(os.getenv("SLICE_HEIGHT", 2000))
        self.overlap_ratio = float(
            os.getenv("SLICE_OVERLAP_RATIO", 0.10)
        )  # 10% overlap
        self.default_batch_size = int(os.getenv("BATCH_SIZE", 5))

        self.demo_file = os.getenv(
            "DEMO_FILE",
            "60807402860000_gb506#2_md_mudlog.json",
        )

        self.redis_host = os.getenv("REDIS_HOST", "localhost")
        self.redis_port = int(os.getenv("REDIS_PORT", 6379))

        self.client = PipelineRedisClient(
            host=self.redis_host,
            port=self.redis_port,
        )
        self.client.init_consumer_groups()

        self.is_running: bool = False
        self.mode: DispatcherMode = DispatcherMode.IDLE
        self.current_batch_index: int = 0
        self.total_batches: int = 0
        self.active_log_ids: List[str] = []
        self.completed_log_ids: List[str] = []

        self.recover_and_auto_resume()

    def _resolve_annotation_path(self, target_identifier: str) -> Optional[Path]:
        """Resolves target annotation path regardless of whether passed with .tif, .json, or quotes."""
        clean_target = target_identifier.strip("'\" \t\r\n")
        stem = Path(clean_target).stem

        search_dirs = [self.annotations_dir, self.data_dir]
        for directory in search_dirs:
            if not directory.exists():
                continue

            direct_json = directory / f"{stem}.json"
            if direct_json.is_file():
                return direct_json

            matches = list(directory.glob(f"*{stem}*.json"))
            if matches:
                return matches[0]

        return None

    def _resolve_image_path(self, image_name: str) -> Path:
        """Resolves source image location across structured or flat directory layouts."""
        clean_name = image_name.strip("'\" \t\r\n")
        in_images = self.images_dir / clean_name
        if in_images.is_file():
            return in_images

        in_data = self.data_dir / clean_name
        if in_data.is_file():
            return in_data

        return in_images

    def _generate_slices(self, start_y: int, end_y: int) -> List[Tuple[int, int]]:
        """Partitions a vertical pixel coordinate interval into chunks with 10% overlap.

        For a 2000 px slice height, overlap is 200 px (10%), giving a step of 1800 px.
        Slice 1: [0, 2000]
        Slice 2: [1800, 3800]
        Slice 3: [3600, 5600], etc.

        Args:
            start_y (int): Top global pixel boundary of the column.
            end_y (int): Bottom global pixel boundary of the column.

        Returns:
            List[Tuple[int, int]]: Ordered list of (y_start, y_end) coordinate pairs with 10% overlap.
        """
        overlap_px = int(self.slice_height * self.overlap_ratio)
        step = max(1, self.slice_height - overlap_px)

        slices = []
        for y in range(start_y, end_y, step):
            slice_end = min(y + self.slice_height, end_y)
            slices.append((y, slice_end))
            # Break if this slice already reaches the bottom of the column
            if y + self.slice_height >= end_y:
                break

        return slices

    def recover_and_auto_resume(self) -> None:
        """Inspects Redis on startup, executes automatic queue reconciliation, and resumes runs in background."""
        tracked = self.client.get_all_tracked_logs()
        for log_id, meta in tracked.items():
            status = meta["status"]
            if status == LogStatus.COMPLETED:
                if log_id not in self.completed_log_ids:
                    self.completed_log_ids.append(log_id)
            elif status in (LogStatus.PROCESSING, LogStatus.MERGING):
                if log_id not in self.active_log_ids:
                    self.active_log_ids.append(log_id)

        # ---------------------------------------------------------------------
        # AUTOMATIC BOOT-TIME STREAM & QUEUE RECONCILIATION
        # ---------------------------------------------------------------------
        if self.active_log_ids:
            print(
                f"[RECOVERY] Auditing {len(self.active_log_ids)} in-flight logs for missing tasks or barriers..."
            )
            for active_id in list(self.active_log_ids):
                ann_path = self._resolve_annotation_path(active_id)
                if ann_path:
                    try:
                        audit_res = self.dispatch_single_log(ann_path, enqueue=False)
                        action = audit_res.get("action")
                        if action == "TRIGGERED_MERGE":
                            print(
                                f"[RECOVERY] Self-healed stuck barrier for {active_id} -> MERGING"
                            )
                        elif action == "RECONCILED":
                            print(
                                f"[RECOVERY] Re-enqueued missing stream slices for {active_id}: {audit_res.get('re_enqueued')}"
                            )
                    except Exception as err:
                        print(
                            f"[RECOVERY ERROR] Failed auditing active log {active_id}: {err}"
                        )

        pipe_state = self.client.get_pipeline_state()
        saved_mode = pipe_state["mode"]
        was_running = pipe_state["is_running"]

        if was_running and saved_mode in (
            DispatcherMode.LIVE.value,
            DispatcherMode.BATCH.value,
        ):
            print(
                f"[RECOVERY] Disruption detected! Auto-resuming {saved_mode} pipeline "
                f"at batch {pipe_state['current_batch']}/{pipe_state['total_batches']}..."
            )
            self.mode = DispatcherMode(saved_mode)
            self.current_batch_index = pipe_state["current_batch"]
            self.total_batches = pipe_state["total_batches"]

            if self.mode == DispatcherMode.BATCH:
                thread = threading.Thread(
                    target=self.run_batch_pipeline,
                    kwargs={
                        "batch_size": pipe_state["batch_size"],
                        "resume_from_batch": pipe_state["current_batch"],
                    },
                    daemon=True,
                )
                thread.start()
            elif self.mode == DispatcherMode.LIVE:
                thread = threading.Thread(
                    target=self.run_live_stream,
                    kwargs={"target_file": pipe_state["target_file"]},
                    daemon=True,
                )
                thread.start()
        else:
            if tracked:
                print(
                    f"[RECOVERY] Restored stationary state from Redis: {len(self.completed_log_ids)} completed, "
                    f"{len(self.active_log_ids)} in-flight."
                )

    def _sync_pipeline_state(self, target_file: Optional[str] = None) -> None:
        """Persists the current dispatcher tracking state to the Redis pipeline state key.

        Args:
            target_file (Optional[str], optional): Target file identifier if executing in LIVE single-log mode. Defaults to None.
        """
        self.client.save_pipeline_state(
            mode=self.mode.value,
            is_running=self.is_running,
            current_batch=self.current_batch_index,
            total_batches=self.total_batches,
            batch_size=self.default_batch_size,
            target_file=target_file,
        )

    def dispatch_single_log(
        self,
        json_path: Path,
        enqueue: bool = True,
    ) -> Dict[str, Any]:
        """Validates file integrity, reconciles missing chunks, or executes atomic multi-slice task dispatching."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        image_name = data["image_name"]
        image_path = self._resolve_image_path(image_name)
        log_id = json_path.stem

        img_hash = compute_sha256(image_path)
        json_hash = compute_sha256(json_path)

        bboxes = data.get("bounding_boxes", {})
        depth_bbox = bboxes.get("depth_column")
        lith_bbox = bboxes.get("interpreted_lithology_column")
        legend_bbox = bboxes.get("lithology_legend")

        if not depth_bbox or not lith_bbox:
            raise ValueError(f"Missing required columns in {json_path.name}")

        depth_slices = self._generate_slices(depth_bbox[1], depth_bbox[3])
        lith_slices = self._generate_slices(lith_bbox[1], lith_bbox[3])

        existing_meta = self.client.get_document_meta(log_id)
        if existing_meta:
            is_valid, reason = self.client.validate_data_integrity(
                log_id, img_hash, json_hash
            )
            if not is_valid:
                if existing_meta["status"] != LogStatus.COMPLETED:
                    self.client.r.hset(
                        f"log:{log_id}:meta",
                        mapping={"image_sha256": img_hash, "json_sha256": json_hash},
                    )
                else:
                    self.client.set_document_status(log_id, LogStatus.CORRUPTED)
                    raise ValueError(f"Data integrity failed for {log_id}: {reason}")

            status = existing_meta["status"]

            if status == LogStatus.COMPLETED:
                return {
                    "log_id": log_id,
                    "image_name": existing_meta["image_name"],
                    "depth_chunks": existing_meta["total_depth_chunks"],
                    "lith_chunks": existing_meta["total_lith_chunks"],
                    "status": status.value,
                    "action": "SKIPPED_COMPLETED",
                }

            if status == LogStatus.MERGING:
                return {
                    "log_id": log_id,
                    "image_name": existing_meta["image_name"],
                    "depth_chunks": existing_meta["total_depth_chunks"],
                    "lith_chunks": existing_meta["total_lith_chunks"],
                    "status": status.value,
                    "action": "SKIPPED_MERGING",
                }

            if status == LogStatus.PROCESSING:
                missing = self.client.get_missing_slices(
                    log_id, depth_slices, lith_slices
                )
                missing_d = missing["missing_depth"]
                missing_l = missing["missing_lith"]

                if not missing_d and not missing_l:
                    print(
                        f"Log {log_id} all slices are completed! Self-healing merge barrier trigger..."
                    )
                    self.client._check_and_trigger_merge(log_id, image_path, lith_bbox)
                    return {
                        "log_id": log_id,
                        "image_name": existing_meta["image_name"],
                        "status": LogStatus.MERGING.value,
                        "action": "TRIGGERED_MERGE",
                    }

                depth_info = self.client.get_stream_group_backlog(
                    self.client.STREAM_DEPTH, self.client.GROUP_DEPTH
                )
                lith_info = self.client.get_stream_group_backlog(
                    self.client.STREAM_LITH, self.client.GROUP_LITH
                )

                depth_stalled = (
                    depth_info["lag"] == 0 and depth_info["pending"] == 0 and missing_d
                )
                lith_stalled = (
                    lith_info["lag"] == 0 and lith_info["pending"] == 0 and missing_l
                )

                if depth_stalled or lith_stalled:
                    print(
                        f"[RECONCILE] Log {log_id} re-enqueuing {len(missing_d)} depth and {len(missing_l)} lith slices."
                    )
                    res = self.client.re_enqueue_missing_slices(
                        log_id=log_id,
                        image_path=image_path,
                        depth_bbox=depth_bbox,
                        lith_bbox=lith_bbox,
                        legend_bbox=legend_bbox,
                        missing_depth=missing_d if depth_stalled else [],
                        missing_lith=missing_l if lith_stalled else [],
                    )
                    return {
                        "log_id": log_id,
                        "image_name": existing_meta["image_name"],
                        "depth_chunks": len(depth_slices),
                        "lith_chunks": len(lith_slices),
                        "status": status.value,
                        "action": "RECONCILED",
                        "re_enqueued": res,
                    }

                return {
                    "log_id": log_id,
                    "image_name": existing_meta["image_name"],
                    "depth_chunks": existing_meta["total_depth_chunks"],
                    "lith_chunks": existing_meta["total_lith_chunks"],
                    "status": status.value,
                    "action": "IN_FLIGHT",
                }

        if not enqueue:
            return {
                "log_id": log_id,
                "image_name": image_name,
                "depth_chunks": len(depth_slices),
                "lith_chunks": len(lith_slices),
                "status": LogStatus.PENDING.value,
                "action": "UNQUEUED",
            }

        self.client.dispatch_log_atomic(
            log_id=log_id,
            image_name=image_name,
            image_path=image_path,
            image_sha256=img_hash,
            json_sha256=json_hash,
            depth_bbox=depth_bbox,
            lith_bbox=lith_bbox,
            legend_bbox=legend_bbox,
            depth_slices=depth_slices,
            lith_slices=lith_slices,
        )

        return {
            "log_id": log_id,
            "image_name": image_name,
            "depth_chunks": len(depth_slices),
            "lith_chunks": len(lith_slices),
            "status": LogStatus.PROCESSING.value,
            "action": "DISPATCHED_ATOMIC",
        }

    def run_live_stream(self, target_file: Optional[str] = None) -> None:
        """Executes the single-log live demo flow, enqueuing slices and monitoring completion events."""
        self.is_running = True
        self.mode = DispatcherMode.LIVE
        self.total_batches = 1
        self.current_batch_index = 1
        self._sync_pipeline_state(target_file=target_file)

        try:
            chosen = target_file or self.demo_file
            target_path = self._resolve_annotation_path(chosen)
            if not target_path:
                print(
                    f"Error: Target annotation matching '{chosen}' not found in "
                    f"'{self.annotations_dir}' or '{self.data_dir}'."
                )
                return

            stream_bookmark = self.client.get_latest_event_id()
            print(
                f"[LIVE MODE] Streaming tasks for single well log: {target_path.name}"
            )
            res = self.dispatch_single_log(target_path, enqueue=True)
            self.active_log_ids = [res["log_id"]]
            self.wait_for_completion([res["log_id"]], since_id=stream_bookmark)
            print(f"[LIVE MODE] Well log {res['log_id']} processing completed.")
        except Exception as e:
            print(f"[LIVE MODE ERROR] {e}")
        finally:
            self.is_running = False
            self.mode = DispatcherMode.IDLE
            self.active_log_ids = []
            self._sync_pipeline_state()

    def run_batch_pipeline(
        self,
        batch_size: Optional[int] = None,
        resume_from_batch: Optional[int] = None,
    ) -> None:
        """Executes full-dataset ingestion in sequential batches with backpressure barriers."""
        self.is_running = True
        self.mode = DispatcherMode.BATCH
        eff_batch_size = self.default_batch_size if batch_size is None else batch_size

        try:
            json_files = sorted(list(self.annotations_dir.glob("*.json")))
            if not json_files:
                json_files = sorted(list(self.data_dir.glob("*.json")))

            if not json_files:
                print(f"No JSON files found in {self.annotations_dir}")
                return

            total_files = len(json_files)

            if eff_batch_size <= 0:
                self.total_batches = 1
                self.current_batch_index = 1
                self._sync_pipeline_state()

                stream_bookmark = self.client.get_latest_event_id()
                all_ids = []
                for p in json_files:
                    try:
                        info = self.dispatch_single_log(p, enqueue=True)
                        all_ids.append(info["log_id"])
                    except Exception as err:
                        print(f"[BATCH ERROR] Failed dispatching {p.name}: {err}")
                self.active_log_ids = all_ids
                self.wait_for_completion(all_ids, since_id=stream_bookmark)
                return

            batches = [
                json_files[i : i + eff_batch_size]
                for i in range(0, total_files, eff_batch_size)
            ]
            self.total_batches = len(batches)

            start_batch = (
                resume_from_batch if resume_from_batch and resume_from_batch > 0 else 1
            )

            terminal_statuses = (
                LogStatus.COMPLETED,
                LogStatus.FAILED,
                LogStatus.CORRUPTED,
            )

            for idx in range(start_batch, self.total_batches + 1):
                if not self.is_running:
                    break

                batch = batches[idx - 1]
                self.current_batch_index = idx
                self._sync_pipeline_state()

                batch_log_ids = [p.stem for p in batch]
                statuses = self.client.get_multiple_document_status(batch_log_ids)

                if statuses and all(
                    statuses.get(lid) in terminal_statuses for lid in batch_log_ids
                ):
                    print(
                        f"--- Skipping Batch {idx}/{self.total_batches} (All logs finished) ---"
                    )
                    for lid, st in statuses.items():
                        if (
                            st == LogStatus.COMPLETED
                            and lid not in self.completed_log_ids
                        ):
                            self.completed_log_ids.append(lid)
                    continue

                print(
                    f"\n--- Starting Batch {idx}/{self.total_batches} ({len(batch)} logs) ---"
                )
                stream_bookmark = self.client.get_latest_event_id()

                dispatched_ids = []
                for p in batch:
                    try:
                        info = self.dispatch_single_log(p, enqueue=True)
                        dispatched_ids.append(info["log_id"])
                    except Exception as err:
                        print(f"[BATCH ERROR] Skipping faulty log {p.name}: {err}")

                self.active_log_ids = dispatched_ids
                if dispatched_ids:
                    self.wait_for_completion(dispatched_ids, since_id=stream_bookmark)

            print("\n[BATCH MODE] All dataset batches completed successfully.")
        finally:
            self.is_running = False
            self.mode = DispatcherMode.IDLE
            self.active_log_ids = []
            self._sync_pipeline_state()

    def wait_for_completion(self, log_ids: List[str], since_id: str) -> None:
        """Blocks kernel execution until all specified log IDs broadcast a terminal status on stream:events."""
        pending = set(log_ids)
        statuses = self.client.get_multiple_document_status(list(pending))
        for log_id, status in statuses.items():
            if status in (LogStatus.COMPLETED, LogStatus.FAILED, LogStatus.CORRUPTED):
                if (
                    status == LogStatus.COMPLETED
                    and log_id not in self.completed_log_ids
                ):
                    self.completed_log_ids.append(log_id)
                pending.discard(log_id)

        self.active_log_ids = list(pending)
        if not pending:
            return

        for event_id, payload in self.client.listen_events(
            since_id=since_id, block_ms=500
        ):
            if not self.is_running or not pending:
                break

            log_id = payload.get("log_id")
            status = payload.get("status")

            if log_id in pending and status in (
                LogStatus.COMPLETED.value,
                LogStatus.FAILED.value,
                LogStatus.CORRUPTED.value,
            ):
                print(f"Log {log_id} finalized processing with status: {status}")
                if (
                    status == LogStatus.COMPLETED.value
                    and log_id not in self.completed_log_ids
                ):
                    self.completed_log_ids.append(log_id)
                pending.discard(log_id)
                self.active_log_ids = list(pending)

                if not pending:
                    break

    def reconcile_all_logs(self) -> Dict[str, Any]:
        """Audits all dataset logs and repairs stalled barriers or dropped slices across the entire pipeline."""
        json_files = sorted(list(self.annotations_dir.glob("*.json")))
        if not json_files:
            json_files = sorted(list(self.data_dir.glob("*.json")))
        results = []

        for p in json_files:
            try:
                info = self.dispatch_single_log(p, enqueue=False)
                results.append(info)
            except Exception as e:
                results.append({"log_id": p.stem, "error": str(e)})

        return {"audited_count": len(json_files), "details": results}


# -------------------------------------------------------------------------
# LIFESPAN & FASTAPI CONTROL PLANE GATEWAY
# -------------------------------------------------------------------------

dispatcher = LogDispatcher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles graceful startup and immediate termination on SIGTERM without 10s container lag."""
    yield
    dispatcher.is_running = False


app = FastAPI(
    title="Well Log Dispatcher & Pipeline Gateway",
    description="Control plane for streaming tasks to Redis and querying real-time document states.",
    version="2.7.0",
    lifespan=lifespan,
)


class LiveRequest(BaseModel):
    """Request payload for triggering a single-log live demo execution."""

    file: Optional[str] = Field(
        default=None,
        description="Optional file name or log ID. Defaults to DEMO_FILE environment variable.",
    )


class BatchRequest(BaseModel):
    """Request payload for triggering full-dataset batch processing."""

    batch_size: Optional[int] = Field(
        default=None,
        description="Number of logs to process per batch. Set to 0 to dispatch all at once. Defaults to BATCH_SIZE env.",
    )


class ResetRequest(BaseModel):
    """Request payload for flushing Redis database and reinitializing orchestrator state."""

    force: bool = Field(
        default=True,
        description="Forcefully abort running pipeline and reset Redis.",
    )


@app.get("/health")
def health_check() -> Dict[str, str]:
    """Evaluates Redis connectivity and overall service health status."""
    try:
        redis_ok = dispatcher.client.r.ping()
        return {
            "status": "healthy",
            "redis": "connected" if redis_ok else "disconnected",
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unreachable: {str(e)}")


@app.get("/status")
@app.get("/api/status")
def get_status() -> Dict[str, Any]:
    """Retrieves operational execution state, active mode, and completed document lists."""
    return {
        "is_running": dispatcher.is_running,
        "mode": dispatcher.mode.value,
        "current_batch": dispatcher.current_batch_index,
        "total_batches": dispatcher.total_batches,
        "active_logs": dispatcher.active_log_ids,
        "completed_logs": dispatcher.completed_log_ids,
        "total_completed": len(dispatcher.completed_log_ids),
    }


@app.post("/live/start")
def start_live_processing(req: LiveRequest = LiveRequest()) -> Dict[str, Any]:
    """Triggers single-log live processing as an unmanaged daemon thread.

    Args:
        req (LiveRequest, optional): Payload containing target file. Defaults to LiveRequest().

    Raises:
        HTTPException: If the pipeline is already actively executing in LIVE or BATCH mode.

    Returns:
        Dict[str, Any]: Confirmation containing dispatch mode and target file.
    """
    if dispatcher.is_running or dispatcher.mode != DispatcherMode.IDLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pipeline is already busy in mode '{dispatcher.mode.value}' on batch "
                f"{dispatcher.current_batch_index}/{dispatcher.total_batches}. Active logs: {dispatcher.active_log_ids}."
            ),
        )

    target = req.file or dispatcher.demo_file
    thread = threading.Thread(
        target=dispatcher.run_live_stream,
        kwargs={"target_file": target},
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "mode": DispatcherMode.LIVE.value,
        "target_file": target,
        "message": "Live single-log streaming started.",
    }


@app.post("/batch/start")
def start_batch_processing(req: BatchRequest = BatchRequest()) -> Dict[str, Any]:
    if dispatcher.is_running or dispatcher.mode != DispatcherMode.IDLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pipeline is already running in mode '{dispatcher.mode.value}' on batch "
                f"{dispatcher.current_batch_index}/{dispatcher.total_batches}. Active logs: {dispatcher.active_log_ids}."
            ),
        )

    eff_batch = (
        dispatcher.default_batch_size if req.batch_size is None else req.batch_size
    )
    thread = threading.Thread(
        target=dispatcher.run_batch_pipeline,
        kwargs={"batch_size": eff_batch},
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "mode": DispatcherMode.BATCH.value,
        "batch_size": eff_batch if eff_batch > 0 else "ALL (unbounded)",
        "message": "Full-dataset batch pipeline initiated.",
    }


@app.api_route("/batch/reconcile", methods=["GET", "POST"])
def reconcile_pipeline() -> Dict[str, Any]:
    """Audits existing tracked tasks in Redis, re-enqueues dropped slices, and clears stuck barriers."""
    return dispatcher.reconcile_all_logs()


@app.post("/reset")
def reset_pipeline(req: ResetRequest = ResetRequest()) -> Dict[str, str]:
    """Flushes Redis database, re-creates consumer groups, and resets orchestrator memory state."""
    if dispatcher.is_running and not req.force:
        raise HTTPException(
            status_code=409,
            detail="Cannot reset Redis while pipeline is running. Pass force=true to override.",
        )

    dispatcher.is_running = False
    dispatcher.client.flush_all()
    dispatcher.mode = DispatcherMode.IDLE
    dispatcher.current_batch_index = 0
    dispatcher.total_batches = 0
    dispatcher.active_log_ids.clear()
    dispatcher.completed_log_ids.clear()

    return {
        "status": "reset",
        "message": "Pipeline halted, Redis flushed, consumer groups recreated, state reset to IDLE.",
    }


@app.get("/api/dashboard")
def get_dashboard() -> Dict[str, Any]:
    """Fetches real-time dashboard data including stream queue depths, persisted states, and per-document progress.

    Returns:
        Dict[str, Any]: Consolidated dictionary of pipeline state, active backlog across consumer groups, and tracked documents.
    """
    r = dispatcher.client.r
    meta_keys = r.keys("log:*:meta")
    documents = []

    pipe_state = dispatcher.client.get_pipeline_state()
    depth_backlog = dispatcher.client.get_stream_group_backlog(
        dispatcher.client.STREAM_DEPTH, dispatcher.client.GROUP_DEPTH
    )
    lith_backlog = dispatcher.client.get_stream_group_backlog(
        dispatcher.client.STREAM_LITH, dispatcher.client.GROUP_LITH
    )
    merge_backlog = dispatcher.client.get_stream_group_backlog(
        dispatcher.client.STREAM_MERGE, dispatcher.client.GROUP_MERGE
    )

    for key in sorted(meta_keys):
        meta = r.hgetall(key)
        parts = key.split(":")
        log_id = ":".join(parts[1:-1])

        d_total = int(meta.get("total_depth_chunks", 0))
        d_done = int(meta.get("completed_depth_chunks", 0))
        l_total = int(meta.get("total_lith_chunks", 0))
        l_done = int(meta.get("completed_lith_chunks", 0))

        total_chunks = d_total + l_total
        done_chunks = d_done + l_done
        pct = round((done_chunks / total_chunks * 100), 1) if total_chunks > 0 else 0.0

        documents.append(
            {
                "log_id": log_id,
                "image_name": meta.get("image_name"),
                "status": meta.get("status"),
                "progress_percent": pct,
                "depth_progress": f"{d_done}/{d_total}",
                "lith_progress": f"{l_done}/{l_total}",
            }
        )

    return {
        "pipeline_state": {
            "is_running": dispatcher.is_running,
            "mode": dispatcher.mode.value,
            "current_batch": f"{dispatcher.current_batch_index}/{dispatcher.total_batches}",
            "persisted_state": pipe_state,
        },
        "queues": {
            "pending_depth_tasks": depth_backlog["lag"] + depth_backlog["pending"],
            "pending_lith_tasks": lith_backlog["lag"] + lith_backlog["pending"],
            "pending_merge_tasks": merge_backlog["lag"] + merge_backlog["pending"],
        },
        "queue_breakdown": {
            "depth": depth_backlog,
            "lithology": lith_backlog,
            "merge": merge_backlog,
        },
        "documents": documents,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
