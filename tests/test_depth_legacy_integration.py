"""Depth adapter regression tests. Run python -m pytest tests/test_depth_legacy_integration.py.

Fake OCR tests validate contracts, not real OCR accuracy. Tesseract smoke is
explicitly skipped when the executable is absent.
"""
import importlib.util
from pathlib import Path
import shutil
import sys
import types
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Load only the owned modules. Python <=3.14 cannot parse 3.15 lazy imports;
# normalize ONLY the in-memory test copy; production source remains unchanged.
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "depth_adapter_test_subject"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "depth")]
sys.modules[PACKAGE] = package
for name in ("legacy_ocr", "extraction"):
    mod = types.ModuleType(PACKAGE + "." + name)
    mod.__package__ = PACKAGE
    sys.modules[mod.__name__] = mod
    source = (ROOT / "src" / "depth" / (name + ".py")).read_text()
    source = source.replace("lazy import ", "import ").replace("lazy from ", "from ")
    exec(compile(source, name + ".py", "exec"), mod.__dict__)
e = sys.modules[PACKAGE + ".extraction"]
l = sys.modules[PACKAGE + ".legacy_ocr"]

class Backend:
    def __init__(self, tokens): self.tokens = tokens; self.calls = 0
    def recognize(self, image):
        self.calls += 1
        return self.tokens if self.calls % 3 == 1 else []

def token(depth, y):
    return e.OCRToken(str(depth), .95, (10, y-4, 45, y+4))

class Integration(unittest.TestCase):
    def image(self, h=300): return np.full((h,80,3),255,np.uint8)
    def test_stateless_single_label_and_no_state_change(self):
        obj=e.DepthExtractor(Backend([token(1200,50)]))
        self.assertEqual(obj.extract_observations(self.image()), {'y_to_depth_map':{50:1200.0}})
        self.assertEqual(obj._offset,0); self.assertIsNone(obj.depth_at(50))
    def test_stream_support_no_extrapolation_reset(self):
        obj=e.DepthExtractor(Backend([token(1200,50),token(1300,150),token(1400,250)]))
        self.assertEqual(len(obj.extract(self.image())['y_to_depth_map']),3)
        self.assertAlmostEqual(obj.depth_at(100),1250)
        self.assertIsNone(obj.depth_at(49)); self.assertIsNone(obj.depth_at(251))
        obj.reset(); self.assertIsNone(obj.depth_at(100))
    def test_conflicts_and_clipping(self):
        obj=e.DepthExtractor(Backend([token(1000,50),token(2000,50),token(3000,2)]))
        self.assertEqual(obj.extract_observations(self.image())['y_to_depth_map'],{})
    def test_atomic_failure(self):
        class Broken:
            def recognize(self,image): raise e.DepthExtractionError('retry')
        obj=e.DepthExtractor(Broken())
        with self.assertRaises(e.DepthExtractionError): obj.extract(self.image())
        self.assertEqual(obj._offset,0); self.assertIsNone(obj._tail)
    def test_rotation_coordinates(self):
        self.assertEqual(e._original_box((10,20,30,40),1,100,200),(60,10,80,30))
        self.assertEqual(e._original_box((10,20,30,40),3,100,200),(20,170,40,190))
    def test_mixed_numeric_scale_rejected(self):
        anchors=[e._Anchor(y, d,.9,str(d)) for y,d in [(10,1000),(60,10.5),(110,1100),(160,11.5),(210,1200),(260,12.5)]]
        self.assertEqual(l.supported_anchors(anchors,5),[])
    def test_competing_scales_rejected(self):
        anchors=[e._Anchor(y,1000+y,.9,str(1000+y)) for y in (10,50,90,130,170,210)]
        anchors += [e._Anchor(y,9000+2*y,.9,str(9000+2*y)) for y in (20,110,200)]
        self.assertEqual(l.supported_anchors(sorted(anchors,key=lambda a:a.y),5),[])
    def test_abbreviated_repetitions_not_expanded(self):
        anchors=[e._Anchor(y,d,.9,str(d)) for y,d in [(10,1000),(40,20),(110,1100),(140,20),(210,1200),(240,20)]]
        self.assertEqual([a.depth for a in l.supported_anchors(anchors,5)],[1000,1100,1200])
    def test_declining_and_insufficient_support(self):
        self.assertEqual(l.supported_anchors([e._Anchor(y,1000-y,.9) for y in (10,100,200)],5),[])
        self.assertEqual(l.supported_anchors([e._Anchor(10,1000,.9)],5),[])
    def test_tsv_scale_padding_confidence(self):
        response=types.SimpleNamespace(returncode=0,stderr=b'',stdout=b'text\tconf\tleft\ttop\twidth\theight\n1200\t90\t32\t52\t40\t20\n')
        with patch.object(l.subprocess,'run',return_value=response) as run:
            words=l.recognize(self.image(),scale=2)
        self.assertEqual(words[0]['box'],(10,20,30,30)); self.assertEqual(words[0]['confidence'],.9)
        self.assertEqual(run.call_args.kwargs['env']['OMP_THREAD_LIMIT'],'1')
    def test_missing_and_malformed_ocr_wrapped(self):
        with patch.object(l.subprocess,'run',side_effect=FileNotFoundError):
            with self.assertRaises(e.DepthExtractionError): e.TesseractOCR().recognize(self.image())
        with patch.object(l.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stderr=b'',stdout=b'invalid')):
            with self.assertRaises(e.DepthExtractionError): e.TesseractOCR().recognize(self.image())
    def test_straight_rule_cleanup_preserves_short_glyphs(self):
        ink=np.zeros((100,100),bool);ink[20,5:95]=True;ink[40,5:15]=True
        runs=l._long_runs(ink,80,1)
        self.assertTrue(runs[20,50]);self.assertFalse(runs[40,10])
    def test_pixel_limit_before_subprocess(self):
        with patch.object(l.subprocess,'run') as run:
            with self.assertRaises(ValueError): l.recognize(np.zeros((4001,3000,3),np.uint8))
            run.assert_not_called()
    def test_bounded_stream_and_width_guard(self):
        obj=e.DepthExtractor(Backend([]))
        for _ in range(75): obj.extract(self.image())
        self.assertEqual(obj._offset,22500);self.assertLessEqual(len(obj._tail),128)
        with self.assertRaises(ValueError): obj.extract(np.zeros((300,90,3),np.uint8))
    @unittest.skipUnless(shutil.which('tesseract'), 'Tesseract executable not installed')
    def test_real_tesseract_rendered_scale(self):
        image=Image.new('RGB',(180,400),'white');draw=ImageDraw.Draw(image)
        try: font=ImageFont.truetype('DejaVuSans.ttf',28)
        except OSError: self.skipTest('DejaVuSans.ttf unavailable')
        for y,d in [(40,'1000'),(160,'1100'),(280,'1200')]: draw.text((30,y),d,font=font,fill='black')
        result=e.DepthExtractor().extract(np.array(image))['y_to_depth_map']
        self.assertGreaterEqual(len(result),3)

if __name__=='__main__': unittest.main()
