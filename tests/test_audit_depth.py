"""Audit regressions. Static Lua assertions are not execution evidence."""
import unittest
import numpy as np
from src.worker_merge import WellLogMergerWorker
import os, sys, types
from pathlib import Path
if os.getenv('PIPELINE_CONTRACT_COMPAT') == '1':
    package=types.ModuleType('src.depth')
    package.__path__=[str(Path(__file__).resolve().parents[1]/'src/depth')]
    sys.modules['src.depth']=package
from src.depth.staging import COMMIT_DEPTH_RESULT
import test_atomic_recovery as atomic
real_redis=atomic.real_redis

class GlobalSupport(unittest.TestCase):
    def setUp(self): self.w=WellLogMergerWorker.__new__(WellLogMergerWorker)
    def test_two_increasing_points_are_not_calibration(self):
        y,d=self.w.filter_monotonic_depth_points({0:100.,100:200.})
        self.assertEqual(len(y),0)
    def test_three_anchors_and_unit_independent_slope(self):
        for scale in (0.001,10000.):
            y,d=self.w.filter_monotonic_depth_points({0:100.,100:100.+scale*100,200:100.+scale*200})
            self.assertEqual(len(y),3)
    def test_unsupported_increasing_points_rejected(self):
        y,d=self.w.filter_monotonic_depth_points({0:100.,100:200.,200:700.})
        self.assertEqual(len(y),0)
    def test_competing_scales_rejected(self):
        points={y:1000+y for y in (100,500,900,1300,1700,2100)}
        points.update({y:9000+2*y for y in (200,1100,2000)})
        y,d=self.w.filter_monotonic_depth_points(points)
        self.assertEqual(len(y),0)
    def test_lua_guard_order_static_only(self):
        s=COMMIT_DEPTH_RESULT
        first_write=s.index("redis.call('HSET'")
        for guard in ("'PROCESSING'", "'unexpected depth slice'", "'depth slice already staged under another fingerprint'"):
            self.assertLess(s.index(guard), first_write)
        self.assertLess(s.index('return 0'),s.index("'PROCESSING'"))

# Use the same explicit opt-in and isolated fixtures as the existing live suite.
@unittest.skipUnless(__import__('os').getenv('ATOMIC_REDIS_URL') and real_redis is not None and hasattr(real_redis,'Redis'),
                     'real Redis not configured; static guards are not Lua execution evidence')
class LiveDepthStaging(unittest.TestCase):
    setUp = atomic.RealRedisAtomicity.setUp
    cleanup = atomic.RealRedisAtomicity.cleanup
    def test_depth_status_membership_and_exact_replay(self):
        from src.depth.staging import commit_depth_result
        r=self.client.r
        def commit(fingerprint='fp',bounds=(0,10)):
            return commit_depth_result(r,self.log,fingerprint,{1:101.},slice_bounds=bounds)
        for status in ('MERGING','COMPLETED'):
            r.hset(self.prefix+':meta','status',status)
            with self.assertRaises(real_redis.exceptions.ResponseError):commit()
            self.assertFalse(r.exists(self.prefix+':depths'))
            self.assertFalse(r.exists(self.prefix+':depth_completed_tasks'))
        r.hset(self.prefix+':meta','status','PROCESSING')
        with self.assertRaises(real_redis.exceptions.ResponseError):commit(bounds=(10,20))
        self.assertFalse(r.exists(self.prefix+':depths'))
        self.assertTrue(commit())
        snapshot=r.hgetall(self.prefix+':meta')
        for status in ('MERGING','COMPLETED'):
            r.hset(self.prefix+':meta','status',status)
            self.assertFalse(commit())
            with self.assertRaises(real_redis.exceptions.ResponseError):commit('changed')
            self.assertEqual(r.hget(self.prefix+':meta','completed_depth'), '1')
            self.assertEqual(r.hgetall(self.prefix+':depths'), {'1':'101.0'})
