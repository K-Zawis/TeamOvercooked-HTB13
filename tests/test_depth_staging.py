from concurrent.futures import ThreadPoolExecutor

import pytest

from depth_staging import commit_depth_result, staging_keys
from redis_stub import StubRedisClient


@pytest.fixture(params=["stub", "lua"])
def client(request):
    if request.param == "stub":
        return StubRedisClient()
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(decode_responses=True)


def hgetall(client, name):
    return dict(client.hashes.get(name, {})) if isinstance(client, StubRedisClient) else client.hgetall(name)


def test_results_counter_and_dedup_committed_together(client):
    depths, meta, done = staging_keys("0000000009")
    client.hset(meta, mapping={"total_depth": "2", "completed_lith": "1"})
    assert commit_depth_result(client, "0000000009", "task-a", {17200: 4100.0, 25480: 4200.0})
    assert hgetall(client, depths) == {"17200": "4100.0", "25480": "4200.0"}
    assert hgetall(client, meta) == {"total_depth": "2", "completed_lith": "1",
                                   "completed_depth": "1", "completed_depth_chunks": "1"}
    assert client.sismember(done, "task-a")
    # A second dispatch/retry cannot replace results or increment the counter.
    assert not commit_depth_result(client, "0000000009", "task-a", {17200: 9999})
    assert hgetall(client, depths)["17200"] == "4100.0"
    assert hgetall(client, meta)["completed_depth"] == "1"


def test_empty_success_counts_and_records_completion(client):
    assert commit_depth_result(client, "a", "empty", {})
    assert not commit_depth_result(client, "a", "empty", {})
    depths, meta, done = staging_keys("a")
    assert hgetall(client, depths) == {}
    assert hgetall(client, meta)["completed_depth"] == "1"
    assert client.sismember(done, "empty")


def test_incoming_schema_counter_and_legacy_alias_are_synchronized(client):
    _, meta, _ = staging_keys("a")
    client.hset(meta, mapping={"completed_depth_chunks": "3", "total_depth_chunks": "5"})
    commit_depth_result(client, "a", "next", {})
    assert hgetall(client, meta) == {"completed_depth_chunks": "4", "completed_depth": "4",
                                   "total_depth_chunks": "5"}


def test_disagreeing_counter_aliases_rejected_before_writes(client):
    depths, meta, done = staging_keys("a")
    client.hset(meta, mapping={"completed_depth": "1", "completed_depth_chunks": "2"})
    with pytest.raises(Exception):
        commit_depth_result(client, "a", "next", {20: 100})
    assert hgetall(client, depths) == {}
    assert not client.sismember(done, "next")


@pytest.mark.parametrize("counter", ["bad", "1.5", "-1", "01", "", "9223372036854775807", "9"*100])
def test_bad_counter_rejected_before_any_write(client, counter):
    depths, meta, done = staging_keys("a")
    client.hset(meta, mapping={"completed_depth": counter})
    with pytest.raises(Exception):
        commit_depth_result(client, "a", "new", {10: 100})
    assert hgetall(client, depths) == {}
    assert hgetall(client, meta)["completed_depth"] == counter
    assert not client.sismember(done, "new")


def test_large_integer_counter_keeps_exact_precision(client):
    _, meta, _ = staging_keys("a")
    client.hset(meta, mapping={"completed_depth": "9007199254740992"})
    commit_depth_result(client, "a", "large", {})
    assert hgetall(client, meta)["completed_depth"] == "9007199254740993"


def test_many_anchors_fit_bounded_lua_unpack_calls(client):
    observations = {i: float(i*10) for i in range(600)}
    assert commit_depth_result(client, "a", "many", observations)
    assert len(hgetall(client, staging_keys("a")[0])) == 600


def test_concurrent_repeated_commits_count_each_logical_task_once(client):
    tasks = [str(i % 5) for i in range(40)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        applied = list(pool.map(lambda task: commit_depth_result(client, "a", task, {}), tasks))
    assert sum(applied) == 5
    assert hgetall(client, staging_keys("a")[1])["completed_depth"] == "5"


@pytest.mark.parametrize("key_index", [0, 1, 2])
def test_wrong_key_type_rejected_before_writes(client, key_index):
    depths, meta, done = keys = staging_keys("a")
    client.xadd(keys[key_index], {"data": "wrong type"})
    with pytest.raises(Exception):
        commit_depth_result(client, "a", "new", {10: 100})
    if key_index != 0:
        assert hgetall(client, depths) == {}
    if key_index != 1:
        assert hgetall(client, meta) == {}
    if key_index != 2:
        assert not client.sismember(done, "new")
