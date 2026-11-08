"""Synthetic RGB -> real lithology + integrated depth -> real atomic CSV.

PIPELINE_CONTRACT_COMPAT=1 uses the contract loader for older Python.
Only TIFF IO, label OCR, depth OCR and the broker are injected in that mode.
A separate live OCR smoke runs only when tesseract is installed.
"""
import ast
import csv
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from test_pipeline_contract import MemoryClient, Reader, images, ROOT
# Emulate lazy package imports only in compatibility execution: do not eagerly
# import the TIFF worker when only the RGB extractor is used.
if os.getenv('PIPELINE_CONTRACT_COMPAT') == '1':
    import sys, types
    package=types.ModuleType('src.depth')
    package.__path__=[str(ROOT/'src/depth')]
    sys.modules['src.depth']=package
from src.depth.extraction import DepthExtractor, OCRToken, TesseractOCR
from src.lithology.task import process_lithology_task, LithologyTask, owned_task_range
from src.lithology.types import LithologyInterval
from src.worker_merge import WellLogMergerWorker

class DepthLabels:
    def __init__(self): self.calls = 0
    def recognize(self, image):
        self.calls += 1
        return ([OCRToken(str(1000+y), .99, (10,y-4,45,y+4))
                 for y in (50,150,250)] if self.calls % 3 == 1 else [])

class EndToEnd(unittest.TestCase):
    def slices(self, start, end):
        # Execute the actual dispatcher method without starting FastAPI/Redis.
        tree=ast.parse((ROOT/'src/dispatcher.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LogDispatcher')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_generate_slices')
        module=ast.Module(body=[fn],type_ignores=[])
        ns={'List':list,'Tuple':tuple};exec(compile(module,'dispatcher.py','exec'),ns)
        obj=type('Config',(),{'slice_height':128,'overlap_ratio':.25})()
        return ns['_generate_slices'](obj,start,end)

    def flow(self, top, bottom):
        client=MemoryClient(); client.depths={}
        track,legend=images()
        full=np.full((300,300,3),255,np.uint8)
        full[:,10:50]=(220,180,40); full[:90,100:260]=legend
        depth_image=np.full((300,80,3),255,np.uint8)
        # Actual synthetic image content with ticks, not fabricated depth rows.
        for y in (50,150,250): depth_image[y,50:75]=0
        extractor=DepthExtractor(DepthLabels())
        client.depths.update(extractor.extract(depth_image)['y_to_depth_map'])
        self.assertEqual(client.depths,{50:1050.,150:1150.,250:1250.})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.png';Image.fromarray(full).save(path)
            def read(task,include_legend=True):
                with Image.open(task.image_path) as im:
                    return (np.array(im.crop((10,task.y_start,50,task.y_end))),
                            np.array(im.crop(task.legend_bbox)) if include_legend else None)
            for start,end in self.slices(top,bottom):
                payload=dict(log_id='0009',image_path=str(path),column_bbox=[10,top,50,bottom],
                             legend_bbox=[100,0,260,90],y_start=start,y_end=end)
                with patch('src.lithology.task.read_task_images',side_effect=read):
                    result=process_lithology_task(payload,recognizer=Reader())
                client.rows.extend(result.to_dict()['intervals'])
            ordered=sorted(client.rows,key=lambda r:r['top_y'])
            self.assertEqual((ordered[0]['top_y'],ordered[-1]['bottom_y']),(top,bottom))
            self.assertTrue(all(a['bottom_y']==b['top_y'] for a,b in zip(ordered,ordered[1:])))
            output=WellLogMergerWorker(client,output_dir=directory,consumer_name='synthetic').export_merged_document('0009')
            with output.open() as f: rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['lithology'],'Sand')
            destination = os.getenv('SYNTHETIC_OUTPUT_DIR')
            if destination:
                target = Path(destination)
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(output, target / f'synthetic-{top}-{bottom}.csv')
            return rows[0]

    @patch.dict(os.environ,{'SLICE_HEIGHT':'128','SLICE_OVERLAP_RATIO':'.25'})
    def test_image_lithology_integrated_depth_csv(self):
        row=self.flow(50,250)
        self.assertEqual((row['top_depth'],row['bottom_depth']),('1050.000','1250.000'))

    @patch.dict(os.environ,{'SLICE_HEIGHT':'128','SLICE_OVERLAP_RATIO':'.25'})
    def test_image_csv_unsupported_endpoints_empty(self):
        row=self.flow(0,300)
        self.assertEqual((row['top_depth'],row['bottom_depth']),('',''))

    def test_empty_single_anchor_csv(self):
        for anchors in ({},{100:1000.}):
            client=MemoryClient();client.depths=anchors
            client.rows=[LithologyInterval(100,200,'Sand',.9,'cluster_0').to_dict()]
            with tempfile.TemporaryDirectory() as d:
                path=WellLogMergerWorker(client,output_dir=d,consumer_name='synthetic').export_merged_document('0009')
                with path.open() as f: row=next(csv.DictReader(f))
                self.assertEqual((row['top_depth'],row['bottom_depth']),('',''))

    def test_finite_short_anchor_validation(self):
        worker=WellLogMergerWorker.__new__(WellLogMergerWorker)
        for anchors in ({100:float('nan')},{float('inf'):1000.},{100:1000.,200:900.}):
            with self.assertRaises(ValueError):worker.filter_monotonic_depth_points(anchors)

    @patch.dict(os.environ,{'SLICE_HEIGHT':'128','SLICE_OVERLAP_RATIO':'.25'})
    def test_exact_dispatcher_ownership(self):
        for top,bottom in ((0,300),(50,250),(0,129),(0,128),(100,101)):
            owned=[]
            for start,end in self.slices(top,bottom):
                task=LithologyTask('x','x',(0,top,40,bottom),(50,0,100,90),start,end)
                owned.append(owned_task_range(task))
            self.assertEqual(owned[0][0],top);self.assertEqual(owned[-1][1],bottom)
            self.assertTrue(all(a[1]==b[0] for a,b in zip(owned,owned[1:])))

    @unittest.skipUnless(shutil.which('tesseract'),'Tesseract executable unavailable')
    def test_live_ocr(self):
        image=Image.new('RGB',(300,400),'white'); draw=ImageDraw.Draw(image)
        font=ImageFont.truetype('DejaVuSans.ttf',28)
        for y,d in ((60,1000),(180,1100),(300,1200)):draw.text((40,y),str(d),fill='black',font=font)
        result=DepthExtractor(TesseractOCR()).extract(np.array(image))['y_to_depth_map']
        self.assertEqual(set(result.values()),{1000.,1100.,1200.})

if __name__=='__main__':unittest.main()
