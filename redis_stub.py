"""In-memory Redis subset for the depth worker; no network connection is made.

Replace this object with the supplied Redis implementation. Method signatures
and stream replies follow redis-py. This stub is single-process and non-durable.
"""

from collections import defaultdict
import threading
import time
from typing import Mapping, Protocol


class RedisClient(Protocol):
    def xgroup_create(self, name, groupname, id="0-0", mkstream=False): ...
    def xreadgroup(self, groupname, consumername, streams, count=1, block=None): ...
    def hset(self, name, *, mapping: Mapping[str, str]): ...
    def sismember(self, name, value): ...
    def eval(self, script, numkeys, *keys_and_args): ...
    def xack(self, name, groupname, *ids): ...


class StubRedisClient:
    def __init__(self):
        self.streams = defaultdict(list)
        self.hashes = defaultdict(dict)
        self.sets = defaultdict(set)
        self.groups = {}
        self._condition = threading.Condition()

    def xadd(self, name, fields):
        with self._condition:
            message_id = f"{len(self.streams[name]) + 1}-0"
            self.streams[name].append((message_id, dict(fields)))
            self._condition.notify_all()
            return message_id

    def xgroup_create(self, name, groupname, id="0-0", mkstream=False):
        with self._condition:
            key = (name, groupname)
            if key in self.groups:
                raise RuntimeError("BUSYGROUP Consumer Group name already exists")
            if name not in self.streams and not mkstream:
                raise RuntimeError("stream does not exist")
            if id not in ("0", "0-0", "$"):
                raise NotImplementedError("stub supports group start 0-0 or $")
            self.groups[key] = {
                "cursor": len(self.streams[name]) if id == "$" else 0,
                "pending": {},
            }
            return True

    def xreadgroup(self, groupname, consumername, streams, count=1, block=None):
        if len(streams) != 1:
            raise NotImplementedError("stub reads one stream at a time")
        name, position = next(iter(streams.items()))
        if position not in ("0", "0-0", ">"):
            raise NotImplementedError("stub reads pending (0) or new (>) entries")
        deadline = None if block == 0 else time.monotonic() + (block or 0) / 1000
        with self._condition:
            group = self.groups[(name, groupname)]
            while True:
                if position == ">":
                    entries = self.streams[name][group["cursor"]:group["cursor"] + count]
                    group["cursor"] += len(entries)
                    for message_id, fields in entries:
                        group["pending"][message_id] = (consumername, fields)
                else:
                    entries = [(mid, fields) for mid, (consumer, fields) in group["pending"].items()
                               if consumer == consumername][:count]
                if entries:
                    return [(name, [(mid, dict(fields)) for mid, fields in entries])]
                if position != ">":
                    return [(name, [])]
                if block is None:
                    return []
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def hset(self, name, *, mapping):
        with self._condition:
            new = sum(str(field) not in self.hashes[name] for field in mapping)
            self.hashes[name].update({str(field): str(value) for field, value in mapping.items()})
            return new

    def xack(self, name, groupname, *ids):
        with self._condition:
            pending = self.groups[(name, groupname)]["pending"]
            removed = 0
            for message_id in ids:
                if message_id in pending:
                    del pending[message_id]
                    removed += 1
            return removed

    def sismember(self, name, value):
        with self._condition:
            if name in self.hashes or name in self.streams:
                raise RuntimeError("WRONGTYPE completed-task key must be a set")
            return value in self.sets.get(name, set())

    def eval(self, script, numkeys, *keys_and_args):
        """Emulate only the worker's commit script, atomically under one lock."""
        from depth_staging import COMMIT_DEPTH_RESULT
        if script != COMMIT_DEPTH_RESULT or numkeys != 3:
            raise NotImplementedError("stub only supports the depth completion script")
        if len(keys_and_args) < 4 or len(keys_and_args[3:]) % 2 != 1:
            raise ValueError("invalid depth completion arguments")
        depths, meta, completed, task_id, *pairs = keys_and_args
        with self._condition:
            if self.sismember(completed, task_id):
                return 0
            for name in (depths, meta):
                if name in self.sets or name in self.streams:
                    raise RuntimeError("WRONGTYPE depth results and metadata must be hashes")
            existing_meta = self.hashes.get(meta, {})
            canonical = existing_meta.get("completed_depth_chunks")
            legacy = existing_meta.get("completed_depth")
            if canonical is not None and legacy is not None and canonical != legacy:
                raise ValueError("depth completion counters disagree")
            counter = canonical if canonical is not None else (legacy if legacy is not None else "0")
            if (not counter.isascii() or not counter.isdecimal()
                    or (len(counter) > 1 and counter.startswith("0"))):
                raise ValueError("completed_depth must be a nonnegative integer")
            if len(counter) > 19 or int(counter) >= 9223372036854775807:
                raise ValueError("completed_depth would overflow")
            # Prepare all new containers before publishing any mutations.
            depth_values = dict(self.hashes.get(depths, {}))
            depth_values.update(zip(pairs[::2], pairs[1::2]))
            meta_values = dict(self.hashes.get(meta, {}))
            meta_values["completed_depth"] = str(int(counter) + 1)
            meta_values["completed_depth_chunks"] = meta_values["completed_depth"]
            completed_values = self.sets.get(completed, set()) | {task_id}
            if pairs:
                self.hashes[depths] = depth_values
            self.hashes[meta] = meta_values
            self.sets[completed] = completed_values
            return 1
