"""Run: python -m unittest discover -s tests -p test_pipeline_contract.py -v.
No xfails: known unsafe behavior deliberately fails acceptance.
PIPELINE_CONTRACT_COMPAT=1 permits a diagnostic Python 3.11 run: source is
loaded in memory after stripping `lazy `, rasterio/redis are import-only stubs.
This is not a native Python 3.15, Redis, TIFF or OCR acceptance run.
"""
import csv
import importlib
import importlib.abc
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
COMPAT = os.getenv("PIPELINE_CONTRACT_COMPAT") == "1"
if COMPAT:
    class Loader(importlib.abc.SourceLoader):
        def __init__(self, path): self.path = path
        def get_filename(self, name): return str(self.path)
        def get_data(self, path): return Path(path).read_bytes()
        def source_to_code(self, data, path, *, _optimize=-1):
            return compile(data.decode().replace("lazy import ", "import ").replace(
                "lazy from ", "from "), path, "exec")
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if not name.startswith("src."): return None
            candidate = ROOT.joinpath(*name.split("."))
            package = candidate.is_dir()
            candidate = candidate / "__init__.py" if package else candidate.with_suffix(".py")
            if candidate.exists():
                return importlib.util.spec_from_file_location(name, candidate,
                    loader=Loader(candidate),
                    submodule_search_locations=[str(candidate.parent)] if package else None)
    sys.meta_path.insert(0, Finder())
    for name in ("redis", "rasterio", "rasterio.enums", "rasterio.errors", "rasterio.windows"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["rasterio.enums"].ColorInterp = types.SimpleNamespace(palette=2, alpha=6)
    sys.modules["rasterio.errors"].NotGeoreferencedWarning = type("NotGeoreferencedWarning", (Warning,), {})
    sys.modules["rasterio.windows"].Window = lambda *a: a

from src.lithology.types import LithologyInterval, TextToken
from src.lithology.features import compute_feature_map
from src.lithology.intervals import merge_intervals, restrict_intervals
from src.lithology.export import write_intervals, FIELDS
from src.lithology.pipeline import interpret_lithology
from src.lithology.task import LithologyTask, process_lithology_task
from src.worker_lithology import handle_task
from src.worker_merge import WellLogMergerWorker
from src.redis_client import PipelineRedisClient

class MemoryClient:
    """Interface double, not Redis and not a durability simulation."""
    def __init__(self): self.rows=[]; self.legend={}; self.done=set(); self.depths={100:1000., 164:1032., 228:1064.}
    def save_legend_map(self, log_id, legend): self.legend.update(legend)
    def append_raw_intervals(self, log_id, rows): self.rows.extend(rows)
    def complete_lith_chunk(self, log_id, msg_id, image_path, column_bbox=None,
                            slice_bounds=None, *, intervals=None, legend=None):
        if intervals is None:
            raise ValueError("atomic publication requires intervals")
        key = (log_id, tuple(slice_bounds))
        fresh = key not in self.done
        if fresh:
            self.rows.extend(intervals)
            self.legend.update(legend or {})
            self.done.add(key)
        return fresh
    def get_raw_intervals(self, log_id): return self.rows
    def get_legend_map(self, log_id): return self.legend
    def get_depth_points(self, log_id): return self.depths

class Reader:
    def recognize(self, image): return [TextToken("Sand", .99, (10, 45, 48, 57))]

def images():
    legend=np.full((90,160,3),255,np.uint8)
    legend[10:40,10:50]=0
    legend[12:38,12:48]=(220,180,40)
    track=np.empty((128,40,3),np.uint8);track[:]=(220,180,40)
    return track,legend

def payload():
    return dict(log_id="0009",image_path="/data/example.tif",
        column_bbox=[10,100,50,228],legend_bbox=[60,0,220,90],y_start=100,y_end=228)

class PipelineContract(unittest.TestCase):
    def test_global_half_open_feature_and_interval_bounds(self):
        track,_=images()
        windows=compute_feature_map(track,global_y=100,window_height=64,stride=16)
        self.assertEqual((windows[0].top_y,windows[-1].bottom_y),(100,228))
        self.assertTrue(all(a.top_y < b.top_y < a.bottom_y for a,b in zip(windows,windows[1:])))
        rows=restrict_intervals([LithologyInterval(90,240,"Sand",.9,"cluster_0")],100,228)
        self.assertEqual((rows[0].top_y, rows[0].bottom_y),(100,228))

    def test_payload_leading_zero_and_invalid_bounds(self):
        self.assertEqual(LithologyTask.from_payload(payload()).log_id,"0009")
        for value in (True,99,229):
            p=payload();p['y_start']=value
            with self.assertRaises(ValueError): LithologyTask.from_payload(p)

    def test_real_swatch_features_classifier_injected_labels(self):
        track,legend=images()
        result=interpret_lithology(track,legend,global_y=100,recognizer=Reader())
        self.assertEqual([x.lithology for x in result.intervals],["Sand"])
        self.assertEqual((result.intervals[0].top_y,result.intervals[-1].bottom_y),(100,228))

    def test_task_to_worker_to_csv_with_injected_io(self):
        client=MemoryClient();track,legend=images()
        with patch('src.lithology.task.read_task_images',return_value=(track,legend)):
            result=handle_task(client,'1-0',payload(),recognizer=Reader())
        self.assertEqual(set(result.to_dict()),{'log_id','y_start','y_end','legend','intervals'})
        self.assertEqual(set(client.rows[0]),set(FIELDS))
        self.assertNotIn('top_depth',client.rows[0])
        with tempfile.TemporaryDirectory() as d:
            with patch('src.worker_merge.socket.gethostname', return_value='contract'):
                path=WellLogMergerWorker(client,output_dir=d).export_merged_document('0009')
            rows=list(csv.DictReader(path.open()))
            self.assertEqual(tuple(rows[0]),WellLogMergerWorker.CSV_FIELDS)
            self.assertEqual((rows[0]['top_depth'],rows[0]['bottom_depth']),('1000.000','1064.000'))
            write_intervals(Path(d)/'pixels.csv',result.intervals)
            self.assertEqual(tuple(next(csv.reader((Path(d)/'pixels.csv').open()))),FIELDS)

    def test_identical_overlap_merges_and_conflicting_overlap_rejects(self):
        a=LithologyInterval(100,200,'Sand',.9,'cluster_0')
        b=LithologyInterval(180,280,'Sand',.8,'cluster_0')
        self.assertEqual(merge_intervals([b,a])[0].bottom_y,280)
        with self.assertRaises(ValueError):
            merge_intervals([a,LithologyInterval(180,280,'Clay',.9,'cluster_1')])

    def test_no_anchors_must_not_become_pixel_depths(self):
        worker=WellLogMergerWorker.__new__(WellLogMergerWorker)
        with self.assertRaises(ValueError):
            worker.interpolate_depth_curve(np.array([100.,200.]),np.array([]),np.array([]))

    def test_one_anchor_must_not_invent_pitch(self):
        worker=WellLogMergerWorker.__new__(WellLogMergerWorker)
        with self.assertRaises(ValueError):
            worker.interpolate_depth_curve(np.array([100.,200.]),np.array([100.]),np.array([1000.]))

    def test_unbracketed_boundaries_must_not_extrapolate(self):
        worker=WellLogMergerWorker.__new__(WellLogMergerWorker)
        result = worker.interpolate_depth_curve(np.array([0.,100.,150.,200.,400.]),np.array([100.,200.]),np.array([1000.,1050.]))
        self.assertTrue(np.isnan(result[[0,4]]).all())
        np.testing.assert_allclose(result[1:4], [1000.,1025.,1050.])

    def test_nonmonotonic_short_span_anchors_must_reject(self):
        worker=WellLogMergerWorker.__new__(WellLogMergerWorker)
        with self.assertRaises(ValueError): worker.filter_monotonic_depth_points({100:1000.,200:900.})

    def test_replayed_lith_task_must_not_append_again(self):
        client=MemoryClient();track,legend=images()
        with patch('src.lithology.task.read_task_images',return_value=(track,legend)):
            handle_task(client,'1-0',payload(),recognizer=Reader())
            count=len(client.rows)
            handle_task(client,'2-0',payload(),recognizer=Reader())
        self.assertEqual(len(client.rows),count,'publication is not idempotent although slice count is')

    def test_depth_container_copy_sources_exist(self):
        text=(ROOT/'build/depth/Dockerfile').read_text()
        for line in text.splitlines():
            if line.startswith('COPY '):
                self.assertTrue((ROOT/line.split()[1]).exists(),line)

    def test_depth_dependency_uses_actual_distribution_name(self):
        requirements=(ROOT/'build/depth/requirements.txt').read_text().splitlines()
        self.assertIn('tifffile',requirements)
        self.assertNotIn('tiffile',requirements)

    def test_startup_recovery_still_called(self):
        # Structural guard only, not a restart test.
        text=(ROOT/'src/dispatcher.py').read_text()
        self.assertIn('self.recover_and_auto_resume()',text)
        self.assertIn('enqueue=False',text)

    def test_real_tiff_window_roundtrip(self):
        if COMPAT: self.skipTest('rasterio unavailable; compatibility stubs are not TIFF evidence')
        import rasterio
        track,legend=images(); full=np.full((250,240,3),255,np.uint8)
        full[100:228,10:50]=track;full[:90,60:220]=legend
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'fixture.tif'
            with rasterio.open(path,'w',driver='GTiff',height=250,width=240,count=3,dtype='uint8') as ds:
                ds.write(np.moveaxis(full,-1,0))
            p=payload();p['image_path']=str(path)
            result=process_lithology_task(p,recognizer=Reader())
            self.assertEqual((result.intervals[0].top_y,result.intervals[-1].bottom_y),(100,228))

if __name__ == '__main__': unittest.main()
