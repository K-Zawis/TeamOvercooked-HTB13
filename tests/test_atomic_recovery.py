"""Broker contract tests plus opt-in real Redis integration.

python -m unittest discover -s tests -p test_atomic_recovery.py -v
ATOMIC_REDIS_URL=redis://localhost:6379/15 enables real Redis Lua tests.
Integration uses unique log IDs and deletes only its own keys/entries.
The isolated import strips Python 3.15 lazy syntax on older Python without
changing production source; mocks below test calls, NOT Redis atomicity.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
try:
    import redis as real_redis
except ImportError:
    real_redis = None

# Isolated module avoids contaminating another suite's redis/compat imports.
source = (ROOT / "src/redis_client.py").read_text().replace("lazy import ", "import ")
broker_module = types.ModuleType("atomic_broker_under_test")
redis_import = real_redis if real_redis is not None and hasattr(real_redis, "exceptions") else types.SimpleNamespace(
    exceptions=types.SimpleNamespace(ResponseError=type("ResponseError", (Exception,), {})))
with patch.dict(sys.modules, {"redis": redis_import}):
    exec(compile(source, str(ROOT / "src/redis_client.py"), "exec"), broker_module.__dict__)
Client = broker_module.PipelineRedisClient

class CallContracts(unittest.TestCase):
    def setUp(self):
        self.client = Client.__new__(Client)
        self.client.r = Mock()

    def test_publication_is_one_eval_not_separate_ack(self):
        self.client.r.eval.return_value = 1
        self.assertTrue(self.client.complete_lith_chunk(
            "0001", "1-0", "image.tif", [1, 2, 3, 4], (2, 4),
            intervals=[{"top_y": 2, "bottom_y": 4}], legend={"c": "Sand"}))
        self.assertEqual([x[0] for x in self.client.r.method_calls], ["eval"])
        args = self.client.r.eval.call_args.args
        self.assertEqual(args[1], 8)
        self.assertEqual(args[2], "log:0001:meta")
        self.assertEqual(args[10:14], ("lith", "2:4", Client.GROUP_LITH, "1-0"))
        self.assertEqual(json.loads(json.loads(args[14])[0]), {"top_y": 2, "bottom_y": 4})

    def test_eval_failure_never_acknowledges_separately(self):
        self.client.r.eval.side_effect = OSError("connection lost")
        with self.assertRaises(OSError):
            self.client.complete_lith_chunk("x", "1-0", "x", None, (0, 10), intervals=[])
        self.client.r.xack.assert_not_called()

    def test_reject_missing_publication_and_invalid_json(self):
        for kw in ({}, {"intervals": [{"confidence": float("nan")}]},
                   {"intervals": [], "legend": {"c": {"not": "string"}}}):
            with self.assertRaises(ValueError):
                self.client.complete_lith_chunk("x", "1-0", "x", None, (0, 10), **kw)
        self.client.r.eval.assert_not_called()

    def test_own_pending_then_claim_then_new_and_retry(self):
        row = ("1-0", {"data": '{"log_id":"0001"}'})
        self.client.r.xreadgroup.side_effect = [[("s", [row])], [], [("s", [("3-0", row[1])])]]
        self.client.r.xautoclaim.return_value = ["0-0", [("2-0", row[1])], []]
        self.assertEqual(self.client.read_task("s", "g", "c")[0], "1-0")
        self.assertEqual(self.client.read_task("s", "g", "c")[0], "2-0")
        self.client.r.xautoclaim.return_value = ["0-0", []]
        self.assertEqual(self.client.read_task("s", "g", "c")[0], "3-0")
        self.assertEqual([call.args[2] for call in self.client.r.xreadgroup.call_args_list],
                         [{"s": "0-0"}, {"s": "1-0"}, {"s": ">"}])
        self.client.r.xack.assert_not_called()

    def test_claim_cursor_and_redis62_response(self):
        self.client.r.xreadgroup.side_effect = [[], [], []]
        self.client.r.xautoclaim.side_effect = [["12-0", []], ["0-0", []]]
        self.assertIsNone(self.client.read_task("s", "g", "c"))
        self.assertIsNone(self.client.read_task("s", "g", "c"))
        calls = self.client.r.xautoclaim.call_args_list
        self.assertEqual([c.kwargs["start_id"] for c in calls], ["0-0", "12-0"])

    def test_nogroup_recreates_and_restarts_pending_recovery(self):
        error = broker_module.redis.exceptions.ResponseError("NOGROUP")
        self.client.r.xreadgroup.side_effect = [error, [("s", [("1-0", {"data": "{}"})])]]
        self.client.init_consumer_groups = Mock()
        self.assertEqual(next(self.client.listen_tasks("s", "g", "c")), ("1-0", {}))
        self.client.init_consumer_groups.assert_called_once()

    def test_lith_worker_passes_rows_to_single_publication(self):
        # AST isolates the adapter function, not image/OCR dependencies.
        import ast
        tree = ast.parse((ROOT / "src/worker_lithology.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handle_task")
        task = types.SimpleNamespace(image_path="x", column_bbox=(0, 0, 10, 20))
        result = types.SimpleNamespace(log_id="0001", y_start=0, y_end=20,
            to_dict=lambda: {"intervals": [], "legend": {"c": "Sand"}})
        namespace = dict(PipelineRedisClient=Client, LithologyTask=Mock(from_payload=Mock(return_value=task)),
                         process_lithology_task=Mock(return_value=result), TextRecognizer=object,
                         LithologyTaskResult=object, LegendPattern=object)
        exec(compile(ast.Module([function], type_ignores=[]), "adapter", "exec"), namespace)
        namespace["handle_task"](self.client, "1-0", {})
        self.client.complete_lith_chunk if False else None
        self.assertEqual([x[0] for x in self.client.r.method_calls], ["eval"])

@unittest.skipUnless(real_redis is not None and os.getenv("ATOMIC_REDIS_URL"),
                     "real Redis not configured; mocks are not Lua execution evidence")
class RealRedisAtomicity(unittest.TestCase):
    def setUp(self):
        self.client = Client.__new__(Client)
        self.client.r = real_redis.Redis.from_url(os.environ["ATOMIC_REDIS_URL"], decode_responses=True)
        self.client.r.ping()  # Explicit URL errors fail, not skip.
        self.log = "atomic-test-" + uuid.uuid4().hex
        self.prefix = "log:" + self.log
        # Isolate stream keys too: no FLUSHDB, shared groups or streams touched.
        for name in ("STREAM_DEPTH", "STREAM_LITH", "STREAM_MERGE", "STREAM_EVENTS"):
            setattr(self.client, name, self.prefix + ":" + name)
        self.client.init_consumer_groups()
        self.client.r.hset(self.prefix + ":meta", mapping=dict(
            total_depth_chunks=1, total_lith_chunks=1, status="PROCESSING"))
        self.client.r.sadd(self.prefix + ":slices:depth:all", "0:10")
        self.client.r.sadd(self.prefix + ":slices:lith:all", "0:10")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        keys = list(self.client.r.scan_iter(match=self.prefix + ":*"))
        if keys: self.client.r.delete(*keys)

    def pending(self, mode):
        stream = getattr(self.client, "STREAM_" + mode.upper())
        group = getattr(self.client, "GROUP_" + mode.upper())
        mid = self.client.r.xadd(stream, {"data": json.dumps({"log_id": self.log})})
        self.client.r.xreadgroup(group, "dead", {stream: ">"}, count=1)
        return mid

    def stage_depth(self):
        self.client.r.hset(self.prefix + ":depths", mapping={"0": "100", "10": "110"})
        self.client.r.sadd(self.prefix + ":slices:depth:completed", "0:10")

    def lith(self, mid):
        return self.client.complete_lith_chunk(self.log, mid, "image", [1, 0, 3, 10], (0, 10),
            intervals=[dict(top_y=0, bottom_y=10, lithology="Sand")], legend={"c": "Sand"})

    def test_replay_and_depth_last_barrier_and_ack(self):
        mid = self.pending("lith")
        self.assertFalse(self.lith(mid))
        self.assertFalse(self.lith(self.pending("lith")))
        self.assertEqual(len(self.client.get_raw_intervals(self.log)), 1)
        self.assertEqual(self.client.r.xpending(self.client.STREAM_LITH, self.client.GROUP_LITH)["pending"], 0)
        self.stage_depth()
        dm = self.pending("depth")
        self.assertTrue(self.client.complete_depth_chunk(self.log, dm, "image", None, (0, 10)))
        self.assertFalse(self.client.complete_depth_chunk(self.log, dm, "image", None, (0, 10)))
        self.assertEqual(self.client.r.xlen(self.client.STREAM_MERGE), 1)
        payload = json.loads(self.client.r.xrange(self.client.STREAM_MERGE)[0][1]["data"])
        self.assertEqual(payload["column_bbox"], [1, 0, 3, 10])
        self.assertEqual(self.client.r.hget(self.prefix + ":meta", "completed_depth"), "1")
        self.assertEqual(self.client.get_document_status(self.log).value, "MERGING")

    def test_lith_last_and_concurrent_replay(self):
        from concurrent.futures import ThreadPoolExecutor
        self.stage_depth()
        mids = [self.pending("lith") for _ in range(8)]
        with ThreadPoolExecutor(8) as pool: results = list(pool.map(self.lith, mids))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.client.get_raw_intervals(self.log)), 1)
        self.assertEqual(self.client.r.xlen(self.client.STREAM_MERGE), 1)

    def test_bad_key_type_has_no_partial_write_or_ack(self):
        mid = self.pending("lith")
        self.client.r.set(self.client.STREAM_MERGE, "wrong type")
        with self.assertRaises(real_redis.exceptions.ResponseError): self.lith(mid)
        self.assertEqual(self.client.get_raw_intervals(self.log), [])
        self.assertEqual(self.client.r.scard(self.prefix + ":slices:lith:completed"), 0)
        self.assertEqual(self.client.r.xpending(self.client.STREAM_LITH, self.client.GROUP_LITH)["pending"], 1)

    def test_unstaged_depth_and_missing_totals_remain_pending(self):
        dm = self.pending("depth")
        with self.assertRaises(real_redis.exceptions.ResponseError):
            self.client.complete_depth_chunk(self.log, dm, "image", None, (0, 10))
        lm = self.pending("lith")
        self.client.r.hdel(self.prefix + ":meta", "total_depth_chunks")
        with self.assertRaises(real_redis.exceptions.ResponseError): self.lith(lm)
        self.assertEqual(self.client.get_raw_intervals(self.log), [])

    def test_completed_replay_does_not_regress_state(self):
        self.stage_depth()
        self.lith(self.pending("lith"))
        self.client.r.hset(self.prefix + ":meta", "status", "COMPLETED")
        self.assertFalse(self.lith(self.pending("lith")))
        self.assertEqual(self.client.get_document_status(self.log).value, "COMPLETED")
        self.assertEqual(self.client.r.xlen(self.client.STREAM_MERGE), 1)

    def test_claim_abandoned_and_retry_pending(self):
        mid = self.pending("lith")
        with patch.dict(os.environ, {"TASK_CLAIM_IDLE_MS": "1"}):
            import time
            time.sleep(.01)
            self.assertEqual(self.client.read_task(self.client.STREAM_LITH, self.client.GROUP_LITH, "new")[0], mid)
            time.sleep(.01)
            self.assertEqual(self.client.read_task(self.client.STREAM_LITH, self.client.GROUP_LITH, "new")[0], mid)

if __name__ == "__main__": unittest.main()
