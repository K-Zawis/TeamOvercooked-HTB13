import os
import json
import hashlib
import threading
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from src.redis_client import PipelineRedisClient, LogStatus


class DispatcherMode(str, Enum):
    IDLE = "IDLE"
    LIVE = "LIVE"
    BATCH = "BATCH"


def compute_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class LogDispatcher:
    def __init__(self) -> None:
        self.data_dir = Path(os.getenv("DATA_DIR", "./dataset"))
        self.images_dir = self.data_dir / "images"
        self.annotations_dir = self.data_dir / "bbox_annotation"
        self.slice_height = int(os.getenv("SLICE_HEIGHT", 2000))
        self.default_batch_size = int(os.getenv("BATCH_SIZE", 5))
        self.demo_file = os.getenv(
            "DEMO_FILE",
            "0000000009_d-582_0000020435-0001_log01_fe_1in_md.json",
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

    def recover_and_auto_resume(self) -> None:
        tracked = self.client.get_all_tracked_logs()
        for log_id, meta in tracked.items():
            status = meta["status"]
            if status == LogStatus.COMPLETED:
                if log_id not in self.completed_log_ids:
                    self.completed_log_ids.append(log_id)
            elif status in (LogStatus.PROCESSING, LogStatus.MERGING):
                if log_id not in self.active_log_ids:
                    self.active_log_ids.append(log_id)

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
        self.client.save_pipeline_state(
            mode=self.mode.value,
            is_running=self.is_running,
            current_batch=self.current_batch_index,
            total_batches=self.total_batches,
            batch_size=self.default_batch_size,
            target_file=target_file,
        )

    def _generate_slices(self, start_y: int, end_y: int) -> List[Tuple[int, int]]:
        slices = []
        for y in range(start_y, end_y, self.slice_height):
            slices.append((y, min(y + self.slice_height, end_y)))
        return slices

    def dispatch_single_log(self, json_path: Path) -> Dict[str, Any]:
        with open(json_path, "r") as f:
            data = json.load(f)

        image_name = data["image_name"]
        image_path = self.images_dir / image_name
        log_id = json_path.stem

        img_hash = compute_sha256(image_path)
        json_hash = compute_sha256(json_path)

        existing_meta = self.client.get_document_meta(log_id)
        if existing_meta:
            is_valid, reason = self.client.validate_data_integrity(
                log_id, img_hash, json_hash
            )
            if not is_valid:
                print(
                    f"[DATA CORRUPTION ERROR] Well log {log_id} failed integrity verification: {reason}"
                )
                self.client.set_document_status(log_id, LogStatus.CORRUPTED)
                raise ValueError(f"Data integrity failed for {log_id}: {reason}")

            status = existing_meta["status"]
            if status in (LogStatus.PROCESSING, LogStatus.MERGING, LogStatus.COMPLETED):
                print(
                    f"Log {log_id} verified healthy in Redis state '{status.value}'. Skipping slice enqueue."
                )
                return {
                    "log_id": log_id,
                    "image_name": existing_meta["image_name"],
                    "depth_chunks": existing_meta["total_depth_chunks"],
                    "lith_chunks": existing_meta["total_lith_chunks"],
                    "already_queued": True,
                }

        bboxes = data.get("bounding_boxes", {})
        depth_bbox = bboxes.get("depth_column")
        lith_bbox = bboxes.get("interpreted_lithology_column")
        legend_bbox = bboxes.get("lithology_legend")

        if not depth_bbox or not lith_bbox:
            raise ValueError(f"Missing required columns in {json_path.name}")

        depth_slices = self._generate_slices(depth_bbox[1], depth_bbox[3])
        lith_slices = self._generate_slices(lith_bbox[1], lith_bbox[3])

        self.client.set_document_meta(
            log_id=log_id,
            total_depth=len(depth_slices),
            total_lith=len(lith_slices),
            image_name=image_name,
            image_sha256=img_hash,
            json_sha256=json_hash,
            status=LogStatus.PROCESSING,
        )

        for y_start, y_end in depth_slices:
            self.client.push_depth_task(
                log_id=log_id,
                image_path=image_path,
                bbox=depth_bbox,
                y_start=y_start,
                y_end=y_end,
            )

        for y_start, y_end in lith_slices:
            self.client.push_lith_task(
                log_id=log_id,
                image_path=image_path,
                column_bbox=lith_bbox,
                legend_bbox=legend_bbox,
                y_start=y_start,
                y_end=y_end,
            )

        return {
            "log_id": log_id,
            "image_name": image_name,
            "depth_chunks": len(depth_slices),
            "lith_chunks": len(lith_slices),
            "already_queued": False,
        }

    def wait_for_completion(self, log_ids: List[str], since_id: str) -> None:
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

        for event_id, payload in self.client.listen_events(since_id=since_id):
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

    def run_live_stream(self, target_file: Optional[str] = None) -> None:
        self.is_running = True
        self.mode = DispatcherMode.LIVE
        self.total_batches = 1
        self.current_batch_index = 1
        self._sync_pipeline_state(target_file=target_file)

        try:
            chosen = target_file or self.demo_file
            target_path = self.annotations_dir / chosen
            if not target_path.exists():
                matches = list(self.annotations_dir.glob(f"*{chosen}*"))
                if not matches:
                    print(f"Error: Demo file '{chosen}' not found.")
                    return
                target_path = matches[0]

            stream_bookmark = self.client.get_latest_event_id()

            print(
                f"[LIVE MODE] Streaming tasks for single well log: {target_path.name}"
            )
            res = self.dispatch_single_log(target_path)
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
        self.is_running = True
        self.mode = DispatcherMode.BATCH
        eff_batch_size = self.default_batch_size if batch_size is None else batch_size

        try:
            json_files = sorted(list(self.annotations_dir.glob("*.json")))
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
                        info = self.dispatch_single_log(p)
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
                        info = self.dispatch_single_log(p)
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


# -------------------------------------------------------------------------
# FASTAPI CONTROL PLANE & ENDPOINTS
# -------------------------------------------------------------------------

app = FastAPI(
    title="Well Log Dispatcher & Pipeline Gateway",
    description="Control plane for streaming tasks to Redis and querying real-time document states.",
    version="2.3.0",
)

dispatcher = LogDispatcher()


class LiveRequest(BaseModel):
    file: Optional[str] = Field(
        default=None,
        description="Optional file name or log ID. Defaults to DEMO_FILE environment variable.",
    )


class BatchRequest(BaseModel):
    batch_size: Optional[int] = Field(
        default=None,
        description="Number of logs to process per batch. Set to 0 to dispatch all at once. Defaults to BATCH_SIZE env.",
    )


class ResetRequest(BaseModel):
    force: bool = Field(
        default=True,
        description="Forcefully abort running pipeline and reset Redis.",
    )


@app.get("/health")
def health_check() -> Dict[str, str]:
    try:
        redis_ok = dispatcher.client.r.ping()
        return {
            "status": "healthy",
            "redis": "connected" if redis_ok else "disconnected",
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unreachable: {str(e)}")


@app.get("/status")
def get_status() -> Dict[str, Any]:
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
def start_live_processing(
    req: LiveRequest = LiveRequest(),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> Dict[str, Any]:
    if dispatcher.is_running or dispatcher.mode != DispatcherMode.IDLE:
        raise HTTPException(
            status_code=409,
            detail=f"Pipeline is already busy in mode '{dispatcher.mode.value}'. Check /status.",
        )

    target = req.file or dispatcher.demo_file
    background_tasks.add_task(dispatcher.run_live_stream, target_file=target)

    return {
        "status": "started",
        "mode": DispatcherMode.LIVE.value,
        "target_file": target,
        "message": "Live single-log streaming started.",
    }


@app.post("/batch/start")
def start_batch_processing(
    req: BatchRequest = BatchRequest(),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> Dict[str, Any]:
    if dispatcher.is_running or dispatcher.mode != DispatcherMode.IDLE:
        raise HTTPException(
            status_code=409,
            detail=f"Pipeline is already busy in mode '{dispatcher.mode.value}'. Check /status.",
        )

    eff_batch = (
        dispatcher.default_batch_size if req.batch_size is None else req.batch_size
    )
    background_tasks.add_task(dispatcher.run_batch_pipeline, batch_size=eff_batch)

    return {
        "status": "started",
        "mode": DispatcherMode.BATCH.value,
        "batch_size": eff_batch if eff_batch > 0 else "ALL (unbounded)",
        "message": "Full-dataset batch pipeline initiated.",
    }


@app.post("/reset")
def reset_pipeline(req: ResetRequest = ResetRequest()) -> Dict[str, str]:
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
    r = dispatcher.client.r
    meta_keys = r.keys("log:*:meta")
    documents = []

    pipe_state = dispatcher.client.get_pipeline_state()

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
            "pending_depth_tasks": r.xlen(dispatcher.client.STREAM_DEPTH),
            "pending_lith_tasks": r.xlen(dispatcher.client.STREAM_LITH),
            "pending_merge_tasks": r.xlen(dispatcher.client.STREAM_MERGE),
        },
        "documents": documents,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
