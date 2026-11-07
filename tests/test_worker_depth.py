import json
from unittest.mock import patch

import numpy as np
import pytest
import tifffile

from depth_extraction import DepthExtractionError, OCRToken
from models import DepthExtractor
from redis_stub import StubRedisClient
from worker_depth import DepthTask, DepthWorker, TASK_STREAM, read_depth_strip


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


def enqueue(client, task):
    return client.xadd(TASK_STREAM, {"data": json.dumps(task)})


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
    class RecordingRedis(StubRedisClient):
        events = None
        def eval(self, *args):
            self.events.append("commit")
            return super().eval(*args)
        def xack(self, *args):
            assert self.hashes["log:0000000009:meta"]["completed_depth"] == "1"
            self.events.append("ack")
            return super().xack(*args)
    client = RecordingRedis()
    client.events = []
    mid = enqueue(client, payload(image_path))
    worker = DepthWorker(client, FakeExtractor(), consumer_name="worker-a")
    assert worker.run_once(block_ms=1)
    assert client.hashes == {"log:0000000009:depths": {"120": "4100.0"},
                             "log:0000000009:meta": {"completed_depth": "1", "completed_depth_chunks": "1"}}
    assert client.events == ["commit", "ack"]
    assert mid not in client.groups[(TASK_STREAM, worker.group)]["pending"]
    assert not worker.run_once(block_ms=1)


def test_sparse_tasks_staged_by_separate_workers_out_of_order(image_path):
    def token(y, depth):
        return OCRToken(str(depth), .95, (10, y-5, 40, y+5))
    class ScriptedOCR:
        def __init__(self, tokens):
            self.calls = iter([tokens, [], []])
        def recognize(self, image):
            return next(self.calls)
    client = StubRedisClient()
    enqueue(client, payload(image_path, start=350, end=450))
    enqueue(client, payload(image_path, start=100, end=350))
    first = DepthWorker(client, DepthExtractor(ScriptedOCR([token(50, 1040)])),
                        consumer_name="a", context_rows=0)
    first.run_once(block_ms=1)
    assert client.hashes["log:0000000009:depths"] == {"400": "1040.0"}
    second = DepthWorker(client, DepthExtractor(ScriptedOCR([
        token(50, 1000), token(150, 1020)])), consumer_name="b", context_rows=0)
    second.run_once(block_ms=1)
    assert client.hashes["log:0000000009:depths"] == {
        "150": "1000.0", "250": "1020.0", "400": "1040.0",
    }
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "2"


def test_interleaving_gaps_and_duplicate_dispatches_are_independent(image_path):
    client = StubRedisClient()
    worker = DepthWorker(client, FakeExtractor(), context_rows=0)
    worker.process_task(DepthTask.from_fields(payload(image_path, start=100, end=200)))
    worker.process_task(DepthTask.from_fields(payload(image_path, start=200, end=300)))
    worker.process_task(DepthTask.from_fields(payload(image_path, start=400, end=500)))
    worker.process_task(DepthTask.from_fields(payload(image_path, log_id="another", start=100, end=200)))
    # Same logical task, including a new stream ID, counts once.
    worker.process_task(DepthTask.from_fields(payload(image_path, log_id="another", start=100, end=200)))
    assert len(client.hashes["log:another:depths"]) == 1
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "3"
    assert client.hashes["log:another:meta"]["completed_depth"] == "1"


@pytest.mark.parametrize("failure", ["eval", "xack"])
def test_redis_failures_leave_pending_without_double_counting(image_path, failure):
    client = StubRedisClient()
    worker = DepthWorker(client, FakeExtractor(), consumer_name="stable-consumer", context_rows=0)
    enqueue(client, payload(image_path, start=100, end=200))
    worker.run_once(block_ms=1)
    mid = enqueue(client, payload(image_path, start=200, end=300))
    with patch.object(client, failure, side_effect=ConnectionError("Redis unavailable")):
        with pytest.raises(ConnectionError):
            worker.run_once(block_ms=1)
    expected_count = "1" if failure == "eval" else "2"
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == expected_count
    assert mid in client.groups[(TASK_STREAM, worker.group)]["pending"]
    worker.run_once(block_ms=1)
    assert client.hashes["log:0000000009:depths"]["220"] == "4100.0"
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "2"
    assert not client.groups[(TASK_STREAM, worker.group)]["pending"]


def test_bad_tasks_remain_pending(image_path):
    client = StubRedisClient()
    worker = DepthWorker(client, FakeExtractor())
    mid = client.xadd(TASK_STREAM, {"data": "not JSON"})
    with pytest.raises(ValueError):
        worker.run_once(block_ms=1)
    assert mid in client.groups[(TASK_STREAM, worker.group)]["pending"]
    assert not client.hashes


def test_ocr_failure_does_not_ack(image_path):
    class BrokenExtractor(FakeExtractor):
        def extract_observations(self, pixels):
            raise DepthExtractionError("OCR timeout")
    client = StubRedisClient()
    worker = DepthWorker(client, BrokenExtractor())
    mid = enqueue(client, payload(image_path))
    with pytest.raises(DepthExtractionError):
        worker.run_once(block_ms=1)
    assert not client.hashes
    assert mid in client.groups[(TASK_STREAM, worker.group)]["pending"]


def test_empty_task_counts_before_ack(image_path):
    class EmptyExtractor:
        def extract_observations(self, pixels):
            return {"y_to_depth_map": {}}
    client = StubRedisClient()
    worker = DepthWorker(client, EmptyExtractor())
    enqueue(client, payload(image_path))
    assert worker.run_once(block_ms=1)
    assert "log:0000000009:depths" not in client.hashes
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "1"
    assert not client.groups[(TASK_STREAM, worker.group)]["pending"]


@pytest.mark.parametrize("failure", ["lost_commit_response", "lost_ack"])
def test_restart_after_commit_does_not_repeat_ocr_or_count(image_path, failure):
    client = StubRedisClient()
    worker = DepthWorker(client, FakeExtractor(), consumer_name="restart")
    enqueue(client, payload(image_path))
    original_eval = client.eval
    def lost_response(*args):
        original_eval(*args)
        raise ConnectionError("response lost after server committed")
    method = "eval" if failure == "lost_commit_response" else "xack"
    side_effect = lost_response if method == "eval" else ConnectionError("ack unavailable")
    with patch.object(client, method, side_effect=side_effect):
        with pytest.raises(ConnectionError):
            worker.run_once(block_ms=1)
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "1"
    class NoOCR:
        def extract_observations(self, pixels):
            raise AssertionError("already committed task must skip OCR")
    restarted = DepthWorker(client, NoOCR(), consumer_name="restart")
    assert restarted.run_once(block_ms=1)
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "1"
    assert not client.groups[(TASK_STREAM, worker.group)]["pending"]


def test_duplicate_stream_entries_count_once(image_path):
    client = StubRedisClient()
    task = payload(image_path)
    enqueue(client, task)
    enqueue(client, task)
    worker = DepthWorker(client, FakeExtractor())
    assert worker.run_once(block_ms=1) and worker.run_once(block_ms=1)
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "1"
    assert not client.groups[(TASK_STREAM, worker.group)]["pending"]


def test_seam_context_is_independent_and_one_task_owns_boundary(image_path):
    pixels = np.full((500, 80, 3), 255, np.uint8)
    pixels[300, 20] = [1, 2, 3]
    tifffile.imwrite(image_path, pixels, photometric="rgb")
    class MarkerExtractor:
        def extract_observations(self, image):
            rows = np.flatnonzero(np.all(image[:, 15] == [1, 2, 3], axis=1))
            return {"y_to_depth_map": {int(y): 4100.0 for y in rows}}
    client = StubRedisClient()
    # Process the later slice first in a different worker, including seam context.
    later = DepthWorker(client, MarkerExtractor(), consumer_name="later")
    earlier = DepthWorker(client, MarkerExtractor(), consumer_name="earlier")
    result = later.process_task(DepthTask.from_fields(payload(image_path, start=300, end=500)))
    assert result == {300: 4100.0}
    result = earlier.process_task(DepthTask.from_fields(payload(image_path, start=100, end=300)))
    assert result == {}  # boundary 300 belongs to the later half-open slice
    assert client.hashes["log:0000000009:depths"] == {"300": "4100.0"}
    assert client.hashes["log:0000000009:meta"]["completed_depth"] == "2"


def test_existing_group_two_consumers_share_work(image_path):
    client = StubRedisClient()
    first = DepthWorker(client, FakeExtractor(), consumer_name="a")
    second = DepthWorker(client, FakeExtractor(), consumer_name="b")
    enqueue(client, payload(image_path, log_id="first"))
    enqueue(client, payload(image_path, log_id="second"))
    assert first.run_once(block_ms=1) and second.run_once(block_ms=1)
    assert set(client.hashes) == {"log:first:depths", "log:second:depths",
                                  "log:first:meta", "log:second:meta"}


@pytest.mark.parametrize("decode_responses", [True, False])
def test_redis_py_wire_replies_with_actual_lua(image_path, decode_responses):
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis(decode_responses=decode_responses)
    task = payload(image_path)
    enqueue(client, task)
    enqueue(client, task)  # different stream entry, same logical chunk
    worker = DepthWorker(client, FakeExtractor(), consumer_name="wire")
    # Real pending reads return [[stream_name, []]], not an empty outer list.
    assert worker.run_once(block_ms=1)
    assert worker.run_once(block_ms=1)
    expected = (lambda s: s) if decode_responses else (lambda s: s.encode())
    assert client.hgetall("log:0000000009:depths") == {expected("120"): expected("4100.0")}
    assert client.hget("log:0000000009:meta", "completed_depth") == expected("1")
    assert client.xpending(TASK_STREAM, worker.group)["pending"] == 0
    assert not worker.run_once(block_ms=1)


def test_cli_seeds_and_drains_stub(image_path, tmp_path, monkeypatch, capsys):
    import worker_depth
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([payload(image_path)]))
    monkeypatch.setattr(worker_depth, "DepthExtractor", FakeExtractor)
    monkeypatch.setattr("sys.argv", ["worker_depth.py", "--tasks", str(tasks)])
    worker_depth.main()
    assert json.loads(capsys.readouterr().out) == {
        "log:0000000009:depths": {"120": "4100.0"},
        "log:0000000009:meta": {"completed_depth": "1", "completed_depth_chunks": "1"},
    }


def test_cli_explicit_stub_starts_local_listener(monkeypatch):
    import worker_depth
    called = []
    monkeypatch.setattr(worker_depth.DepthWorker, "run_forever", lambda self: called.append(self))
    monkeypatch.setattr("sys.argv", ["worker_depth.py", "--stub"])
    worker_depth.main()
    assert len(called) == 1
    assert isinstance(called[0].redis, StubRedisClient)


def test_cli_defaults_to_pipeline_redis_client(monkeypatch):
    import worker_depth
    client = StubRedisClient()
    called = []
    class FakePipelineClient:
        def __init__(self, **kwargs):
            called.append(kwargs)
            self.r = client
    running = []
    monkeypatch.setattr(worker_depth, "PipelineRedisClient", FakePipelineClient)
    monkeypatch.setattr(worker_depth.DepthWorker, "run_forever", lambda self: running.append(self))
    monkeypatch.setattr("sys.argv", ["worker_depth.py", "--redis-host", "redis", "--redis-port", "6380",
                                      "--redis-db", "2", "--consumer", "depth-1"])
    worker_depth.main()
    assert called == [{"host": "redis", "port": 6380, "db": 2}]
    assert running[0].redis is client and running[0].consumer_name == "depth-1"
    assert running[0].group == "group:depth_workers"
