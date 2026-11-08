import json
from unittest.mock import patch

import numpy as np
import pytest
import tifffile

from src.depth import DepthExtractionError, OCRToken, DepthExtractor
from src.redis_client import PipelineRedisClient
from src.depth.worker import DepthTask, DepthWorker, TASK_STREAM, read_depth_strip


def payload(path, *, log_id="0000000009", start=100, end=300, bbox=None):
    return dict(log_id=log_id, image_path=str(path), bbox=bbox or [5, 100, 75, 500],
                y_start=start, y_end=end)


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "log.tif"
    image = np.full((500, 80, 3), 255, dtype=np.uint8)
    tifffile.imwrite(path, image, photometric="rgb", compression="lzw", rowsperstrip=16)
    return path


class FakeExtractor:
    def extract_observations(self, image):
        return {"y_to_depth_map": {20: 4100.0}}


def make_client(*, decode_responses=True):
    import fakeredis
    client = object.__new__(PipelineRedisClient)
    client.r = fakeredis.FakeRedis(decode_responses=decode_responses)
    return client


def hashes(client):
    return {name: client.r.hgetall(name) for name in client.r.scan_iter()
            if client.r.type(name) == "hash"}


def pending(client, worker):
    return {entry["message_id"]: entry for entry in client.r.xpending_range(
        TASK_STREAM, worker.group, min="-", max="+", count=100)}


def enqueue(client, task):
    return client.r.xadd(TASK_STREAM, {"data": json.dumps(task)})


def test_task_json_bytes_and_flat_redis_fields(image_path):
    fields = payload(image_path)
    task = DepthTask.from_fields({b"data": json.dumps(fields).encode()})
    assert task.log_id == "0000000009" and task.bbox == (5, 100, 75, 500)
    flat = {k.encode(): (json.dumps(v) if isinstance(v, list) else str(v)).encode()
            for k, v in fields.items()}
    assert DepthTask.from_fields(flat) == task


@pytest.mark.parametrize("changes", [
    {"log_id": 9}, {"log_id": ""}, {"image_path": ""}, {"bbox": [5, 100, 5, 500]},
    {"bbox": [5, 100, 75]}, {"y_start": 99}, {"y_end": 501}, {"y_end": 100},
    {"y_start": True}, {"y_end": 300.5}, {"bbox": [5, -1, 75, 500]},
])
def test_invalid_tasks_rejected(image_path, changes):
    task = payload(image_path)
    task.update(changes)
    with pytest.raises(ValueError):
        DepthTask.from_fields({"data": json.dumps(task)})


@pytest.mark.parametrize("layout", ["rgb", "tiled", "planar", "gray", "white", "palette", "bilevel", "rgba"])
def test_tifffile_region_decoding(tmp_path, layout):
    path = tmp_path / f"{layout}.tif"
    rng = np.random.default_rng(17)
    rgb = rng.integers(0, 256, size=(80, 64, 3), dtype=np.uint8)
    kwargs = dict(compression="lzw")
    if layout in ("gray", "white"):
        data = rgb[:, :, 0]
        kwargs["photometric"] = "miniswhite" if layout == "white" else "minisblack"
        expected = np.repeat((255-data if layout == "white" else data)[:, :, None], 3, axis=2)
    elif layout == "palette":
        data = rgb[:, :, 0]
        cmap = np.stack([np.arange(256), 255-np.arange(256), np.zeros(256)])
        kwargs.update(photometric="palette", colormap=(cmap*256).astype(np.uint16))
        expected = cmap[:, data].transpose(1, 2, 0).astype(np.uint8)
    elif layout == "bilevel":
        data = rgb[:, :, 0] > 128
        kwargs.update(photometric="miniswhite", compression=None)
        expected = np.repeat((~data)[:, :, None], 3, axis=2).astype(np.uint8)*255
    elif layout == "rgba":
        data = np.dstack([rgb, np.full((80, 64), 255, np.uint8)])
        kwargs.update(photometric="rgb", extrasamples="unassalpha")
        expected = rgb
    else:
        data = rgb if layout != "planar" else np.moveaxis(rgb, -1, 0)
        kwargs.update(photometric="rgb")
        if layout == "planar":
            kwargs["planarconfig"] = "separate"
        if layout == "tiled":
            kwargs["tile"] = (16, 16)
        else:
            kwargs["rowsperstrip"] = 8
        expected = rgb
    tifffile.imwrite(path, data, **kwargs)
    task = DepthTask.from_fields(payload(path, start=20, end=43, bbox=[7, 10, 41, 70]))
    # Guard against accidentally decoding a whole page before cropping.
    with patch.object(tifffile.TiffPage, "asarray", side_effect=AssertionError("full-page decode")):
        pixels = read_depth_strip(task)
    np.testing.assert_array_equal(pixels, expected[20:43, 7:41])
    assert pixels.dtype == np.uint8 and pixels.flags.c_contiguous


def test_out_of_image_bbox_rejected_before_decoding(image_path):
    task = DepthTask.from_fields(payload(image_path, bbox=[5, 100, 100, 500]))
    with pytest.raises(ValueError, match="outside"):
        read_depth_strip(task)


def test_unsupported_high_bit_depth_rejected(tmp_path):
    path = tmp_path / "16bit.tif"
    tifffile.imwrite(path, np.zeros((500, 80), np.uint16))
    with pytest.raises(ValueError, match="sample type"):
        read_depth_strip(DepthTask.from_fields(payload(path)))


def test_stream_hash_coordinates_and_ack_order(image_path):
    client = make_client()
    events = []
    original_eval, original_ack = client.r.eval, client.r.xack
    def commit(*args):
        events.append("commit")
        return original_eval(*args)
    def ack(*args):
        assert hashes(client)["log:0000000009:meta"]["completed_depth_chunks"] == "1"
        events.append("ack")
        return original_ack(*args)
    mid = enqueue(client, payload(image_path))
    worker = DepthWorker(client, FakeExtractor(), consumer_name="worker-a")
    with patch.object(client.r, "eval", side_effect=commit), patch.object(client.r, "xack", side_effect=ack):
        assert worker.run_once(block_ms=1)
    assert hashes(client) == {"log:0000000009:depths": {"120": "4100.0"},
                             "log:0000000009:meta": {"completed_depth": "1", "completed_depth_chunks": "1"}}
    assert events == ["commit", "ack"]
    assert mid not in pending(client, worker)
    assert not worker.run_once(block_ms=1)


def test_sparse_tasks_staged_by_separate_workers_out_of_order(image_path):
    def token(y, depth):
        return OCRToken(str(depth), .95, (10, y-5, 40, y+5))
    class ScriptedOCR:
        def __init__(self, tokens):
            self.calls = iter([tokens, [], []])
        def recognize(self, image):
            return next(self.calls)
    client = make_client()
    enqueue(client, payload(image_path, start=350, end=450))
    enqueue(client, payload(image_path, start=100, end=350))
    first = DepthWorker(client, DepthExtractor(ScriptedOCR([token(50, 1040)])),
                        consumer_name="a", context_rows=0)
    first.run_once(block_ms=1)
    assert hashes(client)["log:0000000009:depths"] == {"400": "1040.0"}
    second = DepthWorker(client, DepthExtractor(ScriptedOCR([
        token(50, 1000), token(150, 1020)])), consumer_name="b", context_rows=0)
    second.run_once(block_ms=1)
    assert hashes(client)["log:0000000009:depths"] == {
        "150": "1000.0", "250": "1020.0", "400": "1040.0",
    }
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "2"


def test_interleaving_gaps_and_duplicate_dispatches_are_independent(image_path):
    client = make_client()
    worker = DepthWorker(client, FakeExtractor(), context_rows=0)
    worker.process_task(DepthTask.from_fields(payload(image_path, start=100, end=200)))
    worker.process_task(DepthTask.from_fields(payload(image_path, start=200, end=300)))
    worker.process_task(DepthTask.from_fields(payload(image_path, start=400, end=500)))
    worker.process_task(DepthTask.from_fields(payload(image_path, log_id="another", start=100, end=200)))
    # Same logical task, including a new stream ID, counts once.
    worker.process_task(DepthTask.from_fields(payload(image_path, log_id="another", start=100, end=200)))
    assert len(hashes(client)["log:another:depths"]) == 1
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "3"
    assert hashes(client)["log:another:meta"]["completed_depth"] == "1"


@pytest.mark.parametrize("failure", ["eval", "xack"])
def test_redis_failures_leave_pending_without_double_counting(image_path, failure):
    client = make_client()
    worker = DepthWorker(client, FakeExtractor(), consumer_name="stable-consumer", context_rows=0)
    enqueue(client, payload(image_path, start=100, end=200))
    worker.run_once(block_ms=1)
    mid = enqueue(client, payload(image_path, start=200, end=300))
    with patch.object(client.r, failure, side_effect=ConnectionError("Redis unavailable")):
        with pytest.raises(ConnectionError):
            worker.run_once(block_ms=1)
    expected_count = "1" if failure == "eval" else "2"
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == expected_count
    assert mid in pending(client, worker)
    worker.run_once(block_ms=1)
    assert hashes(client)["log:0000000009:depths"]["220"] == "4100.0"
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "2"
    assert not pending(client, worker)


def test_bad_tasks_remain_pending(image_path):
    client = make_client()
    worker = DepthWorker(client, FakeExtractor())
    mid = client.r.xadd(TASK_STREAM, {"data": "not JSON"})
    with pytest.raises(ValueError):
        worker.run_once(block_ms=1)
    assert mid in pending(client, worker)
    assert not hashes(client)


def test_ocr_failure_does_not_ack(image_path):
    class BrokenExtractor(FakeExtractor):
        def extract_observations(self, pixels):
            raise DepthExtractionError("OCR timeout")
    client = make_client()
    worker = DepthWorker(client, BrokenExtractor())
    mid = enqueue(client, payload(image_path))
    with pytest.raises(DepthExtractionError):
        worker.run_once(block_ms=1)
    assert not hashes(client)
    assert mid in pending(client, worker)


def test_empty_task_counts_before_ack(image_path):
    class EmptyExtractor:
        def extract_observations(self, pixels):
            return {"y_to_depth_map": {}}
    client = make_client()
    worker = DepthWorker(client, EmptyExtractor())
    enqueue(client, payload(image_path))
    assert worker.run_once(block_ms=1)
    assert "log:0000000009:depths" not in hashes(client)
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "1"
    assert not pending(client, worker)


@pytest.mark.parametrize("failure", ["lost_commit_response", "lost_ack"])
def test_restart_after_commit_does_not_repeat_ocr_or_count(image_path, failure):
    client = make_client()
    worker = DepthWorker(client, FakeExtractor(), consumer_name="restart")
    enqueue(client, payload(image_path))
    original_eval = client.r.eval
    def lost_response(*args):
        original_eval(*args)
        raise ConnectionError("response lost after server committed")
    method = "eval" if failure == "lost_commit_response" else "xack"
    side_effect = lost_response if method == "eval" else ConnectionError("ack unavailable")
    with patch.object(client.r, method, side_effect=side_effect):
        with pytest.raises(ConnectionError):
            worker.run_once(block_ms=1)
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "1"
    class NoOCR:
        def extract_observations(self, pixels):
            raise AssertionError("already committed task must skip OCR")
    restarted = DepthWorker(client, NoOCR(), consumer_name="restart")
    assert restarted.run_once(block_ms=1)
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "1"
    assert not pending(client, worker)


def test_duplicate_stream_entries_count_once(image_path):
    client = make_client()
    task = payload(image_path)
    enqueue(client, task)
    enqueue(client, task)
    worker = DepthWorker(client, FakeExtractor())
    assert worker.run_once(block_ms=1) and worker.run_once(block_ms=1)
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "1"
    assert not pending(client, worker)


def test_seam_context_is_independent_and_one_task_owns_boundary(image_path):
    pixels = np.full((500, 80, 3), 255, np.uint8)
    pixels[300, 20] = [1, 2, 3]
    tifffile.imwrite(image_path, pixels, photometric="rgb")
    class MarkerExtractor:
        def extract_observations(self, image):
            rows = np.flatnonzero(np.all(image[:, 15] == [1, 2, 3], axis=1))
            return {"y_to_depth_map": {int(y): 4100.0 for y in rows}}
    client = make_client()
    # Process the later slice first in a different worker, including seam context.
    later = DepthWorker(client, MarkerExtractor(), consumer_name="later")
    earlier = DepthWorker(client, MarkerExtractor(), consumer_name="earlier")
    result = later.process_task(DepthTask.from_fields(payload(image_path, start=300, end=500)))
    assert result == {300: 4100.0}
    result = earlier.process_task(DepthTask.from_fields(payload(image_path, start=100, end=300)))
    assert result == {}  # boundary 300 belongs to the later half-open slice
    assert hashes(client)["log:0000000009:depths"] == {"300": "4100.0"}
    assert hashes(client)["log:0000000009:meta"]["completed_depth"] == "2"


def test_existing_group_two_consumers_share_work(image_path):
    client = make_client()
    first = DepthWorker(client, FakeExtractor(), consumer_name="a")
    second = DepthWorker(client, FakeExtractor(), consumer_name="b")
    enqueue(client, payload(image_path, log_id="first"))
    enqueue(client, payload(image_path, log_id="second"))
    assert first.run_once(block_ms=1) and second.run_once(block_ms=1)
    assert set(hashes(client)) == {"log:first:depths", "log:second:depths",
                                  "log:first:meta", "log:second:meta"}


@pytest.mark.parametrize("decode_responses", [True, False])
def test_redis_py_wire_replies_with_actual_lua(image_path, decode_responses):
    fakeredis = pytest.importorskip("fakeredis")
    client = make_client(decode_responses=decode_responses)
    task = payload(image_path)
    enqueue(client, task)
    enqueue(client, task)  # different stream entry, same logical chunk
    worker = DepthWorker(client, FakeExtractor(), consumer_name="wire")
    # Real pending reads return [[stream_name, []]], not an empty outer list.
    assert worker.run_once(block_ms=1)
    assert worker.run_once(block_ms=1)
    expected = (lambda s: s) if decode_responses else (lambda s: s.encode())
    assert client.r.hgetall("log:0000000009:depths") == {expected("120"): expected("4100.0")}
    assert client.r.hget("log:0000000009:meta", "completed_depth") == expected("1")
    assert client.r.xpending(TASK_STREAM, worker.group)["pending"] == 0
    assert not worker.run_once(block_ms=1)


def test_main_defaults_only_poll_dispatched_tasks(monkeypatch):
    from src.depth import worker as worker_depth
    client = make_client()
    configured, running = [], []
    for variable in ("REDIS_HOST", "REDIS_PORT", "REDIS_DB", "CONSUMER_NAME"):
        monkeypatch.delenv(variable, raising=False)
    def connect(**kwargs):
        configured.append(kwargs)
        return client
    def no_dispatch(*args, **kwargs):
        raise AssertionError("depth workers must not dispatch tasks")
    monkeypatch.setattr(client, "push_depth_task", no_dispatch)
    monkeypatch.setattr(worker_depth, "PipelineRedisClient", connect)
    monkeypatch.setattr(worker_depth.DepthWorker, "run_forever", lambda self: running.append(self))
    worker_depth.main()
    assert configured == [{"host": "172.28.0.2", "port": 6379, "db": 0}]
    assert len(running) == 1
    assert running[0].client is client
    assert running[0].consumer_name == "DepthWorker"
    assert client.r.xlen(client.STREAM_DEPTH) == 0


def test_main_uses_environment_variables(monkeypatch):
    from src.depth import worker as worker_depth
    client = make_client()
    configured, running = [], []
    def connect(**kwargs):
        configured.append(kwargs)
        return client
    monkeypatch.setattr(worker_depth, "PipelineRedisClient", connect)
    monkeypatch.setattr(worker_depth.DepthWorker, "run_forever", lambda self: running.append(self))
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "2")
    monkeypatch.setenv("CONSUMER_NAME", "depth-1")
    worker_depth.main()
    assert configured == [{"host": "redis", "port": 6380, "db": 2}]
    assert running[0].client is client
    assert running[0].consumer_name == "depth-1"
    assert running[0].group == client.GROUP_DEPTH


def test_run_forever_continues_polling_when_idle_and_backs_off_on_error():
    worker = DepthWorker(make_client(), FakeExtractor())
    with patch.object(worker, "run_once", side_effect=[False, RuntimeError("Redis unavailable"), True, KeyboardInterrupt]) as poll:
        with patch("src.depth.worker.time.sleep") as sleep:
            worker.run_forever()
    assert poll.call_count == 4
    sleep.assert_called_once_with(1)


def test_pipeline_completion_records_recovery_slice_and_triggers_merge(image_path):
    from src.redis_client import LogStatus
    client = make_client()
    client.set_document_meta("0000000009", 1, 1, image_path.name, "image-sha", "json-sha")
    client.r.hset("log:0000000009:meta", "completed_lith_chunks", "1")
    worker = DepthWorker(client, FakeExtractor())
    enqueue(client, payload(image_path))
    assert worker.run_once(block_ms=1)
    meta = client.get_document_meta("0000000009")
    assert meta["completed_depth_chunks"] == meta["total_depth_chunks"] == 1
    assert meta["status"] == LogStatus.MERGING
    assert client.get_missing_slices("0000000009", [(100, 300)], []) == {
        "missing_depth": [], "missing_lith": [],
    }
    assert client.r.xlen(client.STREAM_MERGE) == 1
    enqueue(client, payload(image_path))
    assert worker.run_once(block_ms=1)
    assert client.r.xlen(client.STREAM_MERGE) == 1
    assert client.r.hget("log:0000000009:meta", "completed_depth_chunks") == "1"
