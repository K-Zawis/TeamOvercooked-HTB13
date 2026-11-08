import pytest

from src.lithology import LithologyTaskResult
from src.worker_lithology import consume_tasks, handle_task


class FakeClient:
    STREAM_LITH = "stream:lith_tasks"
    GROUP_LITH = "group:lith_workers"

    def __init__(self, messages=()):
        self.messages = tuple(messages)
        self.listen_args = None
        self.legend = []
        self.intervals = []
        self.completed = []

    def listen_tasks(self, stream, group, consumer_name, block_ms=2000):
        self.listen_args = (stream, group, consumer_name, block_ms)
        yield from self.messages

    def save_legend_map(self, log_id, mapping):
        self.legend.append((log_id, mapping))

    def append_raw_intervals(self, log_id, intervals):
        self.intervals.append((log_id, intervals))

    def complete_lith_chunk(self, *args):
        self.completed.append(args)


def payload():
    return {
        "log_id": "0000000009",
        "image_path": "/data/images/log.tif",
        "column_bbox": [70, 80, 110, 240],
        "legend_bbox": [5, 5, 65, 70],
        "y_start": 80,
        "y_end": 240,
    }


def result():
    from src.lithology import LegendPattern, LithologyInterval, compute_features
    import numpy as np

    features = compute_features(np.full((20, 20, 3), 255, np.uint8))
    return LithologyTaskResult(
        "0000000009",
        80,
        240,
        (LegendPattern("Shale", features),),
        (LithologyInterval(80, 240, "Shale", 0.9, "cluster_0"),),
    )


def test_handle_task_uses_only_public_redis_methods(monkeypatch):
    client = FakeClient()
    expected = result()
    monkeypatch.setattr(
        "src.worker_lithology.process_lithology_task",
        lambda *args, **kwargs: expected,
    )
    assert handle_task(client, "1-0", payload()) is expected
    assert client.legend == [("0000000009", {"cluster_0": "Shale"})]
    assert client.intervals == [("0000000009", [{
        "top_y": 80,
        "bottom_y": 240,
        "lithology": "Shale",
        "cluster_id": "cluster_0",
        "confidence": 0.9,
    }])]
    assert client.completed == [(
        "0000000009",
        "1-0",
        "/data/images/log.tif",
        [70, 80, 110, 240],
    )]


def test_consume_tasks_uses_exposed_listener(monkeypatch):
    client = FakeClient([("1-0", payload())])
    handled = []
    monkeypatch.setattr(
        "src.worker_lithology.handle_task",
        lambda *args, **kwargs: handled.append((args, kwargs)) or result(),
    )
    consume_tasks(client, consumer_name="lith-1", block_ms=25)
    assert client.listen_args == (
        client.STREAM_LITH,
        client.GROUP_LITH,
        "lith-1",
        25,
    )
    assert len(handled) == 1


def test_failed_processing_does_not_publish_or_complete(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        "src.worker_lithology.process_lithology_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    with pytest.raises(RuntimeError, match="failed"):
        handle_task(client, "1-0", payload())
    assert client.legend == []
    assert client.intervals == []
    assert client.completed == []
