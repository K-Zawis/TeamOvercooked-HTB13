import json
import weakref
from unittest.mock import Mock, patch

import numpy as np
import pytest
import tifffile

from src.depth import DepthExtractionError, OCRToken, DepthExtractor
from src.redis_client import PipelineRedisClient
from src.depth.worker import DepthTask, DepthWorker, TASK_STREAM, iter_depth_strips, read_depth_strip


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
def test_rasterio_region_decoding(tmp_path, layout):
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
        strips = list(iter_depth_strips(task, strip_height=10, context_rows=3))
    np.testing.assert_array_equal(pixels, expected[20:43, 7:41])
    assert pixels.dtype == np.uint8 and pixels.flags.c_contiguous
    assert [(s.y_start, s.y_end) for s in strips] == [(20, 30), (30, 40), (40, 43)]
    for strip in strips:
        assert len(strip.pixels) <= 16
        assert strip.pixels.dtype == np.uint8 and strip.pixels.flags.c_contiguous
        np.testing.assert_array_equal(strip.pixels, expected[
            strip.y_offset:strip.y_offset+len(strip.pixels), 7:41])


def test_out_of_image_bbox_rejected_before_decoding(image_path):
    task = DepthTask.from_fields(payload(image_path, bbox=[5, 100, 100, 500]))
    with pytest.raises(ValueError, match="outside"):
        read_depth_strip(task)


@pytest.mark.parametrize("layout", ["stripped", "tiled", "planar"])
def test_only_bounded_windows_are_read(tmp_path, opened_depth_resources, layout):
    path = tmp_path / f"blocks-{layout}.tif"
    rgb = np.random.default_rng(17).integers(0, 256, (256, 128, 3), dtype=np.uint8)
    options = dict(photometric="rgb", compression="lzw")
    if layout == "tiled":
        options["tile"] = (16, 16)
    else:
        options["rowsperstrip"] = 16
    if layout == "planar":
        options["planarconfig"] = "separate"
    tifffile.imwrite(path, np.moveaxis(rgb, -1, 0) if layout == "planar" else rgb,
                     **options)
    task = DepthTask.from_fields(payload(path, start=100, end=124,
                                         bbox=[40, 80, 60, 160]))
    strips = iter_depth_strips(task, strip_height=8, context_rows=0)
    try:
        for start in (100, 108, 116):
            strip = next(strips)
            np.testing.assert_array_equal(strip.pixels, rgb[start:start+8, 40:60])
            call = opened_depth_resources[0].read.call_args
            window = call.kwargs["window"]
            assert (window.col_off, window.row_off, window.width, window.height) == (40, start, 20, 8)
            del strip
        with pytest.raises(StopIteration):
            next(strips)
    finally:
        strips.close()
    assert len(opened_depth_resources) == 1
    assert opened_depth_resources[0].read.call_count == 3


def test_unsupported_high_bit_depth_rejected(tmp_path):
    path = tmp_path / "16bit.tif"
    tifffile.imwrite(path, np.zeros((500, 80), np.uint16))
    with pytest.raises(ValueError, match="sample type"):
        read_depth_strip(DepthTask.from_fields(payload(path)))


def test_dark_eight_bit_grayscale_is_not_scaled_as_bilevel(tmp_path):
    path = tmp_path / "dark.tif"
    gray = np.indices((500, 80))[0].astype(np.uint8) % 2
    tifffile.imwrite(path, gray, photometric="minisblack")
    pixels = read_depth_strip(DepthTask.from_fields(payload(path)))
    np.testing.assert_array_equal(pixels, np.repeat(gray[100:300, 5:75, None], 3, axis=2))


@pytest.mark.parametrize("associated", [False, True])
def test_transparent_rgba_composited_onto_white(tmp_path, associated):
    path = tmp_path / "alpha.tif"
    rgba = np.full((500, 80, 4), 128, np.uint8)
    rgba[:, :, :3] = [50, 25, 0] if associated else [100, 50, 0]
    tifffile.imwrite(path, rgba, photometric="rgb",
                     extrasamples="assocalpha" if associated else "unassalpha")
    rgb = rgba[:, :, :3].astype(np.float32)
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255
    expected = ((rgb if associated else rgb * alpha) + 255 * (1-alpha)).astype(np.uint8)
    pixels = read_depth_strip(DepthTask.from_fields(payload(path)))
    np.testing.assert_array_equal(pixels, expected[100:300, 5:75])


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
    for variable in ("REDIS_HOST", "REDIS_PORT", "REDIS_DB", "CONSUMER_NAME", "SLICE_HEIGHT"):
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
    assert running[0].strip_height == 2000
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
    monkeypatch.setenv("SLICE_HEIGHT", "512")
    worker_depth.main()
    assert configured == [{"host": "redis", "port": 6380, "db": 2}]
    assert running[0].client is client
    assert running[0].consumer_name == "depth-1"
    assert running[0].group == client.GROUP_DEPTH
    assert running[0].strip_height == 512


@pytest.fixture
def large_depth_task(tmp_path):
    path = tmp_path / "large.tif"
    pixels = np.full((6000, 80, 3), 255, np.uint8)
    # A marker exactly at each internal boundary and near the last strip's end.
    for y in (150, 2100, 4100, 5850):
        pixels[y, 20] = [1, 2, 3]
    tifffile.imwrite(path, pixels, photometric="rgb", compression="lzw", rowsperstrip=16)
    return DepthTask.from_fields(payload(path, start=100, end=5900, bbox=[5, 100, 75, 5900]))


@pytest.fixture
def opened_depth_resources(monkeypatch):
    """Observe real Rasterio windows; prohibit reads without an explicit ROI."""
    from src.depth import worker as worker_depth
    original_open = worker_depth.rasterio.open
    handles = []

    class Reader:
        def __init__(self, image):
            self.image = image
            self.close = Mock(wraps=image.close)
            self.read = Mock(side_effect=self.read_window)

        def read_window(self, *args, **kwargs):
            assert kwargs.get("window") is not None, "full-image read"
            return self.image.read(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.image, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def open_file(*args, **kwargs):
        handle = Reader(original_open(*args, **kwargs))
        handles.append(handle)
        return handle

    monkeypatch.setattr(worker_depth.rasterio, "open", open_file)
    return handles


def test_iterator_bounds_offsets_and_single_open(large_depth_task, opened_depth_resources):
    from src.depth import worker as worker_depth
    windows = []
    decode = worker_depth._read_rgb_window
    def record(image, left, right, start, end):
        windows.append((start, end))
        return decode(image, left, right, start, end)
    with patch.object(worker_depth, "_read_rgb_window", side_effect=record):
        strips = list(iter_depth_strips(large_depth_task))
    assert [(s.y_start, s.y_end) for s in strips] == [(100, 2100), (2100, 4100), (4100, 5900)]
    assert windows == [(100, 2228), (1972, 4228), (3972, 5900)]
    assert [s.y_offset for s in strips] == [100, 1972, 3972]
    assert max(len(s.pixels) for s in strips) <= 2256
    handles = opened_depth_resources
    assert len(handles) == 1
    assert handles[0].closed
    handles[0].close.assert_called_once_with()


def test_iterator_closes_resources_when_stopped_early(large_depth_task, opened_depth_resources):
    strips = iter_depth_strips(large_depth_task)
    first = next(strips)
    assert first.y_start == 100
    strips.close()
    handles = opened_depth_resources
    assert handles[0].closed
    handles[0].close.assert_called_once_with()


def test_large_task_preserves_boundary_ownership_and_counts_once(large_depth_task):
    class MarkerExtractor:
        sizes = []
        def extract_observations(self, image):
            self.sizes.append(len(image))
            rows = np.flatnonzero(np.all(image[:, 15] == [1, 2, 3], axis=1))
            return {"y_to_depth_map": {int(y): 4100.0 for y in rows}}
    client = make_client()
    extractor = MarkerExtractor()
    worker = DepthWorker(client, extractor)
    task = large_depth_task
    data = payload(task.image_path, start=task.y_start, end=task.y_end, bbox=list(task.bbox))
    enqueue(client, data)
    assert worker.run_once(block_ms=1)
    assert extractor.sizes == [2128, 2256, 1928]
    assert client.r.hgetall(f"log:{task.log_id}:depths") == {
        "150": "4100.0", "2100": "4100.0", "4100": "4100.0", "5850": "4100.0",
    }
    assert client.r.hget(f"log:{task.log_id}:meta", "completed_depth_chunks") == "1"
    assert client.r.smembers(f"log:{task.log_id}:slices:depth:completed") == {"100:5900"}
    enqueue(client, data)
    assert worker.run_once(block_ms=1)
    assert len(extractor.sizes) == 3  # duplicate task never decodes/OCRs again
    assert client.r.hget(f"log:{task.log_id}:meta", "completed_depth_chunks") == "1"


def test_worker_releases_each_pixel_array_before_next_strip(large_depth_task):
    class TrackingExtractor:
        def __init__(self):
            self.images = []
        def extract_observations(self, image):
            if self.images:
                assert self.images[-1]() is None, "previous image strip was retained"
            self.images.append(weakref.ref(image))
            return {"y_to_depth_map": {}}
    extractor = TrackingExtractor()
    DepthWorker(make_client(), extractor).process_task(large_depth_task)
    assert len(extractor.images) == 3
    assert all(image() is None for image in extractor.images)


@pytest.mark.parametrize("failure", ["ocr", "decode"])
def test_later_strip_failure_closes_resources_without_partial_commit(
    large_depth_task, opened_depth_resources, failure,
):
    from src.depth import worker as worker_depth
    class FailingExtractor(FakeExtractor):
        calls = 0
        def extract_observations(self, image):
            self.calls += 1
            if self.calls == 2 and failure == "ocr":
                raise DepthExtractionError("second strip failed")
            return super().extract_observations(image)
    original_decode = worker_depth._read_rgb_window
    decode_calls = []
    def decode(*args):
        decode_calls.append(args[-2:])
        if len(decode_calls) == 2 and failure == "decode":
            raise ValueError("second strip failed")
        return original_decode(*args)
    task = large_depth_task
    client = make_client()
    worker = DepthWorker(client, FailingExtractor(), context_rows=0)
    mid = enqueue(client, payload(task.image_path, start=100, end=5900, bbox=list(task.bbox)))
    with patch.object(worker_depth, "_read_rgb_window", side_effect=decode):
        with pytest.raises((DepthExtractionError, ValueError), match="second strip"):
            worker.run_once(block_ms=1)
    assert not hashes(client)
    assert mid in pending(client, worker)
    assert not client.r.exists(f"log:{task.log_id}:depth_completed_tasks")
    handles = opened_depth_resources
    assert handles[0].closed
    handles[0].close.assert_called_once_with()
    worker.extractor = FakeExtractor()
    assert worker.run_once(block_ms=1)
    assert client.r.hgetall(f"log:{task.log_id}:depths") == {
        "120": "4100.0", "2120": "4100.0", "4120": "4100.0",
    }
    assert client.r.hget(f"log:{task.log_id}:meta", "completed_depth_chunks") == "1"
    assert not pending(client, worker)
    assert len(handles) == 2
    assert all(handle.closed for handle in handles)
    assert all(handle.close.call_count == 1 for handle in handles)


@pytest.mark.parametrize("height", [0, -1, True, 1.5, "2000"])
def test_invalid_strip_height_rejected_before_opening_or_redis(image_path, height):
    task = DepthTask.from_fields(payload(image_path))
    client = make_client()
    with patch("src.depth.worker.rasterio.open", side_effect=AssertionError("opened TIFF")):
        with pytest.raises(ValueError, match="strip_height"):
            list(iter_depth_strips(task, strip_height=height))
        with pytest.raises(ValueError, match="strip_height"):
            DepthWorker(client, FakeExtractor(), strip_height=height)
    assert client.r.dbsize() == 0


def test_run_forever_continues_polling_when_idle_and_backs_off_on_error(caplog):
    worker = DepthWorker(make_client(), FakeExtractor())
    caplog.set_level("INFO", logger="src.depth.worker")
    with patch.object(worker, "run_once", side_effect=[False, False, RuntimeError("Redis unavailable"), True, False, False, KeyboardInterrupt]) as poll:
        with patch("src.depth.worker.time.sleep") as sleep:
            worker.run_forever()
    assert poll.call_count == 7
    sleep.assert_called_once_with(1)
    assert caplog.messages.count("No depth tasks available; waiting for the next batch.") == 2


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
