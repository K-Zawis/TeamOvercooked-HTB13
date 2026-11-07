import os
import json
import uvicorn
from enum import Enum
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Tuple
from fastapi import FastAPI, BackgroundTasks, HTTPException

from src.redis_client import PipelineRedisClient, LogStatus

class DispatcherMode(str, Enum):
    IDLE = "IDLE"
    LIVE = "LIVE"
    BATCH = "BATCH"

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
        log_id = json_path.stem.split("_")[0]

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
        }

    def wait_for_completion(self, log_ids: List[str], since_id: str) -> None:
        pending = set(log_ids)

        for event_id, payload in self.client.listen_events(since_id=since_id):
            if not self.is_running or not pending:
                break

            log_id = payload.get("log_id")
            status = payload.get("status")

            if log_id in pending and status in (
                LogStatus.MERGING.value,
                LogStatus.COMPLETED.value,
            ):
                print(f"Log {log_id} completed processing ({status}).")
                self.completed_log_ids.append(log_id)
                pending.remove(log_id)
                self.active_log_ids = list(pending)

    def run_live_stream(self, target_file: Optional[str] = None) -> None:
        self.is_running = True
        self.mode = DispatcherMode.LIVE
        self.total_batches = 1
        self.current_batch_index = 1

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
        finally:
            self.is_running = False
            self.mode = DispatcherMode.IDLE
            self.active_log_ids = []

    def run_batch_pipeline(self, batch_size: Optional[int] = None) -> None:
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
                print(f"[BATCH MODE] Dispatching all {total_files} logs immediately...")
                self.total_batches = 1
                self.current_batch_index = 1

                stream_bookmark = self.client.get_latest_event_id()
                all_ids = [self.dispatch_single_log(p)["log_id"] for p in json_files]
                self.active_log_ids = all_ids
                self.wait_for_completion(all_ids, since_id=stream_bookmark)
                return

            batches = [
                json_files[i : i + eff_batch_size]
                for i in range(0, total_files, eff_batch_size)
            ]
            self.total_batches = len(batches)
            print(
                f"[BATCH MODE] Dispatching {total_files} logs across {self.total_batches} batches..."
            )

            for idx, batch in enumerate(batches, start=1):
                self.current_batch_index = idx
                print(
                    f"\n--- Starting Batch {idx}/{self.total_batches} ({len(batch)} logs) ---"
                )

                stream_bookmark = self.client.get_latest_event_id()
                current_batch_ids = [
                    self.dispatch_single_log(p)["log_id"] for p in batch
                ]
                self.active_log_ids = current_batch_ids
                self.wait_for_completion(current_batch_ids, since_id=stream_bookmark)

            print("\n[BATCH MODE] All dataset batches completed successfully.")
        finally:
            self.is_running = False
            self.mode = DispatcherMode.IDLE
            self.active_log_ids = []


# -------------------------------------------------------------------------
# FASTAPI CONTROL PLANE & ENDPOINTS
# -------------------------------------------------------------------------

app = FastAPI(
    title="Well Log Dispatcher & Pipeline Gateway",
    description="Control plane for streaming tasks to Redis and querying real-time document states.",
    version="2.0.0",
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


@app.get("/api/dashboard")
def get_dashboard() -> Dict[str, Any]:
    r = dispatcher.client.r
    meta_keys = r.keys("log:*:meta")
    documents = []

    for key in sorted(meta_keys):
        meta = r.hgetall(key)
        log_id = key.split(":")[1]

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
        },
        "queues": {
            "pending_depth_tasks": r.xlen(dispatcher.client.STREAM_DEPTH),
            "pending_lith_tasks": r.xlen(dispatcher.client.STREAM_LITH),
            "pending_merge_tasks": r.xlen(dispatcher.client.STREAM_MERGE),
        },
        "documents": documents,
    }


@app.post("/reset")
def reset_pipeline() -> Dict[str, str]:
    if dispatcher.is_running:
        raise HTTPException(
            status_code=409,
            detail="Cannot reset Redis while a dispatch run is actively executing.",
        )

    dispatcher.client.flush_all()

    dispatcher.mode = DispatcherMode.IDLE
    dispatcher.current_batch_index = 0
    dispatcher.total_batches = 0
    dispatcher.active_log_ids.clear()
    dispatcher.completed_log_ids.clear()

    return {
        "status": "reset",
        "message": "Redis flushed, consumer groups recreated, and dispatcher state reset to IDLE.",
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
