"""Offline merge/recovery regressions; no running broker or OCR required."""
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from src.worker_merge import WellLogMergerWorker
from src.redis_client import LogStatus

class Client:
    STREAM_MERGE = "merge"
    GROUP_MERGE = "merger"
    def __init__(self):
        self.status = LogStatus.MERGING
        self.events = []
        self.rows = [dict(top_y=1000,bottom_y=1100,lithology="sand",confidence=.9,cluster_id="c")]
        self.depths = {1000:200., 1050:205., 1100:210.}
    def get_raw_intervals(self, log): return self.rows
    def get_legend_map(self, log): return {}
    def get_depth_points(self, log): return self.depths
    def get_document_status(self, log): return self.status
    def set_document_status(self, log, status):
        self.status = status
        self.events.append("status")
    def ack_task(self, *args): self.events.append("ack")
    def init_consumer_groups(self): pass
    def listen_tasks(self, *args, **kwargs): yield "1", {"log_id":"0001"}

class RecoveryMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Client()
        self.worker = WellLogMergerWorker(self.client,self.tmp.name,consumer_name="audit")
    def test_global_coordinates_and_depth(self):
        self.worker.process_merge_task("1", {"log_id":"0001"})
        with open(Path(self.tmp.name)/"0001.csv") as f: row = next(csv.DictReader(f))
        self.assertEqual((row["top_y"],row["bottom_y"]),("1000","1100"))
        self.assertEqual((row["top_depth"],row["bottom_depth"]),("200.000","210.000"))
        self.assertEqual(self.client.events,["status","ack"])
    def test_missing_single_anchor_completes_with_blank_depths(self):
        for anchors in ({}, {1000:200.}):
            with self.subTest(anchors=anchors):
                self.client.status = LogStatus.MERGING
                self.client.events = []
                self.client.depths = anchors
                self.worker.process_merge_task("1", {"log_id":"0001"})
                with open(Path(self.tmp.name)/"0001.csv") as f:
                    row = next(csv.DictReader(f))
                self.assertEqual((row['top_depth'], row['bottom_depth']), ('',''))
                self.assertEqual(self.client.events, ['status','ack'])

    def test_invalid_observed_depths_fail_and_stay_pending(self):
        for anchors in ({1000:float('nan'),1100:210.}, {1000:210.,1100:200.}):
            with self.subTest(anchors=anchors):
                self.client.status = LogStatus.MERGING
                self.client.events = []
                self.client.depths = anchors
                with self.assertRaises(ValueError):
                    self.worker.process_merge_task("1", {"log_id":"0001"})
                self.assertEqual(self.client.events, [])

    def test_exact_unknown_retries_deduplicated(self):
        self.client.rows[0].update(lithology=None,cluster_id=None)
        self.client.rows *= 2
        self.assertEqual(len(self.worker.reconcile_intervals("0001")),1)
    def test_conflicting_overlap_fails(self):
        self.client.rows.append(dict(top_y=1050,bottom_y=1150,lithology="shale",confidence=.8,cluster_id="s"))
        with self.assertRaises(ValueError): self.worker.reconcile_intervals("0001")
    def test_fractional_coordinate_not_silently_truncated(self):
        self.client.rows[0]["top_y"] = 1000.5
        with self.assertRaises(ValueError): self.worker.reconcile_intervals("0001")
    def test_failed_atomic_publish_keeps_previous_file(self):
        target = Path(self.tmp.name)/"0001.csv"
        target.write_text("previous")
        with patch.object(__import__(WellLogMergerWorker.__module__,fromlist=["os"]).os,"replace",side_effect=OSError("disk")):
            with self.assertRaises(OSError): self.worker.export_merged_document("0001")
        self.assertEqual(target.read_text(),"previous")
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[target])
        self.assertEqual(self.client.events,[])
    def test_ack_loss_retry_does_not_reexport(self):
        with patch.object(self.client,"ack_task",side_effect=OSError("lost")):
            with self.assertRaises(OSError): self.worker.process_merge_task("1",{"log_id":"0001"})
        self.assertEqual(self.client.status,LogStatus.COMPLETED)
        with patch.object(self.worker,"export_merged_document",side_effect=AssertionError("reexport")):
            self.worker.process_merge_task("1",{"log_id":"0001"})
        self.assertEqual(self.client.events[-1],"ack")
    def test_run_failure_leaves_merging_pending(self):
        self.client.depths = {1000:float('nan'),1100:210.}
        self.worker.run()
        self.assertEqual(self.client.status,LogStatus.MERGING)
        self.assertEqual(self.client.events,[])
    def test_missing_completed_file_regenerated(self):
        self.client.status = LogStatus.COMPLETED
        self.worker.process_merge_task("1",{"log_id":"0001"})
        self.assertTrue((Path(self.tmp.name)/"0001.csv").is_file())
    def test_safe_filename(self):
        with self.assertRaises(ValueError): self.worker.export_merged_document("../bad")
    def test_touching_known_seams_stitched(self):
        row = dict(self.client.rows[0]);row.update(top_y=1100,bottom_y=1200)
        self.client.rows.append(row)
        result = self.worker.reconcile_intervals("0001")
        self.assertEqual([(x.top_y,x.bottom_y) for x in result],[(1000,1200)])
    def test_unsupported_global_boundaries_are_blank(self):
        values = self.worker.interpolate_depth_curve(np.array([900.,1200.]),np.array([1000.,1100.]),np.array([200.,210.]))
        self.assertTrue(np.isnan(values).all())

if __name__ == "__main__": unittest.main()
