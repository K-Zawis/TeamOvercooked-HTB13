# Streaming depth extraction

`models.DepthExtractor` consumes contiguous RGB strips from a cropped depth-scale
column. It reads numeric labels, rejects inconsistent OCR, and interpolates
between trusted observations. Rock segmentation and naming remain separate.

## Setup

Use Python 3.10+ and `python -m pip install -r requirements.txt`. Install the
Tesseract executable and English language data through your system package
manager (for example, `tesseract-ocr` and `tesseract-ocr-eng` on Debian/Ubuntu).
No network requests or model downloads occur during extraction.

```python
from models import DepthExtractor

extractor = DepthExtractor()
extractor.reset()  # required at the start of EVERY document
offset = 0
anchors = {}
for rgb_strip in strips:  # H x W x 3 uint8; default caller height is 2000
    result = extractor.extract(rgb_strip)
    anchors.update({y + offset: d for y, d in result["y_to_depth_map"].items()})
    # A query uses pixels relative to the start of this document's depth crop.
    depth = extractor.depth_at(offset + len(rgb_strip) // 2)
    offset += len(rgb_strip)
```

Strip heights can vary, including a short final strip. Width must remain fixed.
Add the depth crop's original-image top coordinate to accumulated anchor rows
when mapping them back to the source image. Depth units remain those printed on
the diagram; no unit conversion or filename-derived calibration is performed.

## OCR replacement and failures

```python
from depth_extraction import OCRToken, TesseractOCR

class AlternativeOCR:
    def recognize(self, rgb_image):
        # Return OCRToken(text, confidence, (left, top, right, bottom)).
        # Confidence is 0..1; boxes use this input's original pixel dimensions.
        # Input may be rotated; the extractor reverses that transformation.
        return []

extractor = DepthExtractor(ocr_backend=AlternativeOCR())
# Or customize local Tesseract:
extractor = DepthExtractor(TesseractOCR(executable="tesseract", timeout=30, scale=2))
```

Backend calls are synchronous and must not mutate the input array. The extractor
tries upright and both quarter-turn orientations. Tesseract applies contrast
enhancement, bounded enlargement, padding, and straight-frame cleanup.

Invalid image inputs raise `ValueError`. Missing Tesseract, invalid TSV, timeouts,
and nonzero OCR exits raise `DepthExtractionError`. A failed extraction does not
advance stream state; retry that same strip. A valid strip with no usable labels
returns an empty map and advances normally. A replacement backend should raise
`DepthExtractionError` for its execution failures.

## Coordinates, calibration, and limits

- Output contains newly trusted observed anchors, not an entry for every row.
  At least three numeric anchors must agree on a positive linear scale within
  five pixels (configurable `residual_pixels`), with at least 60% support.
  `min_confidence` defaults to 0.5. Conflicting readings at the same row are
  excluded. Pending labels can become trusted after subsequent strips arrive.
- Delayed labels have **negative strip-local rows**, including bootstrap labels
  older than the seam tail. Always add the current strip offset, even for negative
  keys. Do not discard them. Previously emitted rows are not retracted.
- `depth_at(y)` uses piecewise linear interpolation between retained trusted
  observations and returns `None` outside their support. It never extrapolates.
  Its coordinate origin is the start of the stream, not the current strip or image.
- Retained state is limited to 128 RGB rows, 256 recent numeric candidates, and
  bounded calibration/emission metadata. Old support eventually expires from
  `depth_at`; callers may keep emitted anchors separately. Processing memory is
  proportional to the supplied strip, not the full log height.
- Labels clipped at the strip edge are deferred to the next strip. A glyph
  extending more than 128 rows above a seam may not be recoverable. A clipped
  label at the end of the document is discarded. Text centres are approximate
  depth positions; isolated nearby ticks are preferred when available.
- Strong scale changes, curved/skewed scans, abbreviated labels, sparse scales,
  and systematic OCR errors can defeat consensus. A positive fit is not proof of
  geological correctness. Fewer emitted anchors indicate missing evidence, not
  permission to fill unsupported depths.

**Integration still required:** `worker.py` is unchanged. It reuses its extractor
across documents without calling `reset()`, and `calculate_depth` remains a stub.
Callers must add the document reset and implement downstream interpolation before
using that worker for complete multi-document depth results. Without reset, state
from separate logs can mix; the extractor cannot infer document boundaries.

## Tests and dataset validation

```bash
python -m pip install -r requirements-validation.txt
python -m pytest -q
python validate_depth.py --dataset ../dataset --output /tmp/depth-selected.json
python validate_depth.py --dataset ../dataset --all --max-strips 3 --heights 2000 --output /tmp/depth-all.json
```

Set `TESSERACT_BIN` for tests, or `--tesseract` for validation, to use a nonstandard
executable path. OCR integration tests skip when Tesseract is unavailable. Unit
tests use a replaceable fake engine and require no OCR installation.

The validator uses rasterio windows, including palette expansion, rather than
decoding entire TIFFs. It runs four selected logs at heights 1000, 2000, and 3000;
`--all` includes all annotations. `--max-strips` bounds a smoke run. It writes a
JSON report after each run, records OCR errors, and exits nonzero if any run fails.
Successful execution does not imply accurate extraction or nonempty results.

`tests/depth_reference.json` contains small manual transcriptions and reviewed
windows. Only those windows have recall, false-detection, and pixel-error metrics.
Each selected log also has one held-out label: its entire text region is masked
before OCR, then its reference depth is compared with `depth_at`. Reference
depths are never supplied to the extractor. Original dataset files are unchanged
and are not included in repository fixtures. See `DEPTH_VALIDATION.md` for measured
results and remaining limitations.
