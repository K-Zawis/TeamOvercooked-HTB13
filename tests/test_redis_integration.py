import json
from pathlib import Path

import fakeredis
import numpy as np
import pytest
import tifffile

from src.dispatcher import LogDispatcher
from src.redis_client import LogStatus, PipelineRedisClient
from worker_depth import DepthWorker


class Observations:
    def extract_observations(self, image):
        return {"y_to_depth_map": {20: 4100.0}}


@pytest.fixture
def setup_pipeline(tmp_path):
    raw = fakeredis.FakeRedis(decode_responses=True)
    client = PipelineRedisClient(connection=raw)
    dataset = tmp_path / "dataset"
    (dataset / "images").mkdir(parents=True)
    (dataset / "bbox_annotation").mkdir()
    def annotation(stem):
        image = dataset / "images" / f"{stem}.tif"
        tifffile.imwrite(image, np.full((500, 80, 3), 255, np.uint8), photometric="rgb")
        path = dataset / "bbox_annotation" / f"{stem}.json"
        path.write_text(json.dumps({"image_name": image.name, "bounding_boxes": {
            "depth_column": [5, 100, 75, 500],
            "interpreted_lithology_column": [5, 100, 75, 500],
            "lithology_legend": [0, 0, 10, 10],
        }}))
        return path
    return client, dataset, annotation


def test_dispatcher_worker_schema_groups_and_results(setup_pipeline):
    client, dataset, annotation = setup_pipeline
    dispatcher = LogDispatcher(data_dir=dataset, slice_height=200, client=client)
    result = dispatcher.dispatch_single_log(annotation("0000000009_log01"))
    log_id = result["log_id"]
    assert log_id == "0000000009_log01"
    assert result["depth_chunks"] == result["lith_chunks"] == 2
    worker = DepthWorker(client.r, Observations(), context_rows=0)
    assert worker.group == client.GROUP_DEPTH
    assert worker.run_once(block_ms=1) and worker.run_once(block_ms=1)
    meta = client.r.hgetall(f"log:{log_id}:meta")
    assert meta["completed_depth_chunks"] == meta["total_depth_chunks"] == "2"
    assert meta["completed_depth"] == "2"
    assert meta["completed_lith_chunks"] == "0"
    assert client.r.hgetall(f"log:{log_id}:depths") == {"120": "4100.0", "320": "4100.0"}
    assert client.r.xpending(client.STREAM_DEPTH, client.GROUP_DEPTH)["pending"] == 0
    assert client.get_document_status(log_id) == LogStatus.PROCESSING


def test_colliding_prefix_documents_stay_separate(setup_pipeline):
    client, dataset, annotation = setup_pipeline
    dispatcher = LogDispatcher(data_dir=dataset, slice_height=400, client=client)
    one = dispatcher.dispatch_single_log(annotation("0000000009_log01"))
    two = dispatcher.dispatch_single_log(annotation("0000000009_log02"))
    assert one["log_id"] != two["log_id"]
    worker = DepthWorker(client.r, Observations(), context_rows=0)
    assert worker.run_once(block_ms=1) and worker.run_once(block_ms=1)
    for log_id in (one["log_id"], two["log_id"]):
        assert client.r.hget(f"log:{log_id}:meta", "completed_depth_chunks") == "1"
        assert client.r.hgetall(f"log:{log_id}:depths") == {"120": "4100.0"}


def test_fresh_dispatch_clears_stale_results_and_completion_markers(setup_pipeline):
    client, dataset, annotation = setup_pipeline
    dispatcher = LogDispatcher(data_dir=dataset, slice_height=400, client=client)
    path = annotation("0000000009_log01")
    result = dispatcher.dispatch_single_log(path)
    prefix = f"log:{result['log_id']}"
    worker = DepthWorker(client.r, Observations())
    assert worker.run_once(block_ms=1)
    client.r.rpush(f"{prefix}:raw_intervals", "old")
    client.r.hset(f"{prefix}:legend", mapping={"old": "Shale"})
    assert client.r.scard(f"{prefix}:depth_completed_tasks") == 1
    dispatcher.dispatch_single_log(path)
    assert not client.r.exists(f"{prefix}:depths", f"{prefix}:depth_completed_tasks",
                               f"{prefix}:raw_intervals", f"{prefix}:legend")
    assert client.r.hget(f"{prefix}:meta", "completed_depth_chunks") == "0"
    assert worker.run_once(block_ms=1)
    assert client.r.hget(f"{prefix}:meta", "completed_depth_chunks") == "1"


def test_client_retries_own_pending_messages_before_new_work(setup_pipeline):
    client, _, _ = setup_pipeline
    client.init_consumer_groups()
    first = client.push_depth_task("a", "/data/a.tif", [0, 0, 10, 100], 0, 100)
    second = client.push_depth_task("b", "/data/b.tif", [0, 0, 10, 100], 0, 100)
    args = client.STREAM_DEPTH, client.GROUP_DEPTH, "worker"
    assert client.read_task(*args, block_ms=1)[0] == first
    assert client.read_task(*args, block_ms=1)[0] == first
    client.ack_task(client.STREAM_DEPTH, client.GROUP_DEPTH, first)
    assert client.read_task(*args, block_ms=1)[0] == second


def test_dispatcher_emits_absolute_paths(setup_pipeline, monkeypatch):
    client, dataset, annotation = setup_pipeline
    annotation("a")
    monkeypatch.chdir(dataset.parent)
    dispatcher = LogDispatcher(data_dir="dataset", slice_height=400, client=client)
    dispatcher.dispatch_single_log(Path("dataset/bbox_annotation/a.json"))
    _, task = client.read_task(client.STREAM_DEPTH, client.GROUP_DEPTH, "worker", block_ms=1)
    assert Path(task["image_path"]).is_absolute()
    assert Path(task["image_path"]).is_file()


@pytest.mark.parametrize("height", [0, -10])
def test_invalid_slice_height_rejected(setup_pipeline, height):
    client, dataset, _ = setup_pipeline
    with pytest.raises(ValueError, match="positive"):
        LogDispatcher(data_dir=dataset, slice_height=height, client=client)
