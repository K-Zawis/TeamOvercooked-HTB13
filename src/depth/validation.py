"""Windowed dataset validation; reference depths never enter the extractor.

uv run python -m src.depth.validation --dataset ../dataset --output /tmp/depth-validation.json
uv run python -m src.depth.validation --dataset ../dataset --all --max-strips 3 --heights 2000
"""

import argparse
import json
from pathlib import Path
import time
import warnings

lazy import numpy as np

lazy from .extraction import TesseractOCR, DepthExtractor


def read_rgb(source, box):
    """Read only a bounded window, expanding indexed TIFF colours correctly."""
    from rasterio.windows import Window
    x0, y0, x1, y1 = box
    bands = source.read(window=Window(x0, y0, x1-x0, y1-y0))
    if bands.dtype != np.uint8:
        raise ValueError(f"unsupported dataset pixel type: {bands.dtype}")
    if len(bands) == 1:
        try:
            palette = source.colormap(1)
        except ValueError:
            return np.repeat(bands[0, :, :, None], 3, axis=2)
        lut = np.zeros((256, 3), dtype=np.uint8)
        for index, color in palette.items():
            lut[index] = color[:3]
        return lut[bands[0]]
    if len(bands) >= 3:
        return np.moveaxis(bands[:3], 0, -1).copy()
    raise ValueError("unsupported dataset band count")


def metrics(anchors, reference, masked):
    lo, hi = reference['review_window']
    held = reference['held_out']
    expected = [(y, d) for y, d in reference['labels']
                if not masked or y != held['y']]
    observed = [(y, d) for y, d in anchors.items() if lo <= y <= hi]
    matches = [(y, d, next(((oy, od) for oy, od in observed
                if abs(oy-y) <= 5 and od == d), None)) for y, d in expected]
    found = [match for match in matches if match[2] is not None]
    false = [(y, d) for y, d in observed if not any(
        abs(y-ey) <= 5 and d == ed for ey, ed in expected)]
    return {
        'reviewed_labels': len(expected), 'matched_labels': len(found),
        'anchor_recall': len(found)/len(expected) if expected else None,
        'false_detections_in_review_window': false,
        'mean_absolute_pixel_error': (sum(abs(y-m[0]) for y, _, m in found)/len(found)
                                      if found else None),
        'missing_labels': [[y, d] for y, d, m in matches if m is None],
    }


def run(dataset, annotation, height, backend, reference=None, max_strips=None, masked=False):
    import rasterio
    metadata = json.loads(annotation.read_text())
    box = metadata['bounding_boxes'].get('depth_column')
    if not box:
        return {'annotation': annotation.name, 'status': 'no_depth_column'}
    extractor = DepthExtractor(backend)
    anchors, held_prediction = {}, None
    started = time.monotonic()
    x0, top, x1, bottom = box
    count = 0
    with rasterio.Env(GDAL_CACHEMAX=32*1024*1024), rasterio.open(dataset/'images'/metadata['image_name']) as source:
        for offset in range(0, bottom-top, height):
            if max_strips is not None and count >= max_strips:
                break
            pixels = read_rgb(source, [x0, top+offset, x1, min(bottom, top+offset+height)])
            if masked:
                first, last = reference['held_out']['mask_rows']
                first, last = max(0, first-top-offset), min(len(pixels), last-top-offset)
                if first < last:
                    pixels[first:last] = 255
            result = extractor.extract(pixels)['y_to_depth_map']
            anchors.update({y+top+offset: d for y, d in result.items()})
            count += 1
            if masked:
                value = extractor.depth_at(reference['held_out']['y'] - top)
                if value is not None:
                    held_prediction = value
    result = {
        'annotation': annotation.name, 'strip_height': height,
        'masked_held_out_label': masked, 'status': 'success',
        'strips': count, 'anchors': len(anchors),
        'seconds': round(time.monotonic()-started, 2),
        'retained_candidates': len(extractor._candidates),
        'emitted_anchors': sorted(anchors.items()),
    }
    if reference:
        result.update(metrics(anchors, reference, masked))
    if masked:
        result.update(held_out_depth=held_prediction,
                      held_out_absolute_depth_error=(abs(held_prediction-reference['held_out']['depth'])
                                                     if held_prediction is not None else None))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=Path('../dataset'))
    parser.add_argument('--reference', type=Path, default=Path(__file__).resolve().parents[2]/'tests/depth_reference.json')
    parser.add_argument('--output', type=Path, default=Path('/tmp/depth-validation.json'))
    parser.add_argument('--heights', type=int, nargs='+', default=[1000, 2000, 3000])
    parser.add_argument('--all', action='store_true', help='Include every dataset annotation')
    parser.add_argument('--max-strips', type=int)
    parser.add_argument('--tesseract', default='tesseract')
    args = parser.parse_args()
    if any(h <= 0 for h in args.heights) or (args.max_strips is not None and args.max_strips <= 0):
        parser.error('heights and max-strips must be positive')
    from rasterio.errors import NotGeoreferencedWarning
    warnings.filterwarnings('ignore', category=NotGeoreferencedWarning)
    if not args.reference.exists() and not args.all:
        parser.error('reference file is required unless --all is used')
    references = ({r['annotation']: r for r in json.loads(args.reference.read_text())['logs']}
                  if args.reference.exists() else {})
    paths = sorted((args.dataset/'bbox_annotation').glob('*.json')) if args.all else [
        args.dataset/'bbox_annotation'/name for name in references]
    if not paths:
        parser.error('no annotations found')
    backend = TesseractOCR(args.tesseract)
    report = {'description': 'Only manually reviewed windows have accuracy metrics. Remaining anchors are unverified.',
              'runs': []}
    for path in paths:
        reference = references.get(path.name)
        for height in args.heights:
            for masked in ([False, True] if reference else [False]):
                try:
                    # Held-out checks need only the early reviewed region, not the full log.
                    limit = min(args.max_strips or 10**9, (3000+height-1)//height) if masked else args.max_strips
                    result = run(args.dataset, path, height, backend, reference, limit, masked)
                except Exception as exc:
                    result = {'annotation': path.name, 'strip_height': height,
                              'masked_held_out_label': masked, 'status': 'error',
                              'error': str(exc)}
                report['runs'].append(result)
                print(json.dumps({k:v for k,v in result.items() if k != 'emitted_anchors'}), flush=True)
                args.output.write_text(json.dumps(report, indent=2)+'\n')
    return int(any(r['status'] == 'error' for r in report['runs']))


if __name__ == '__main__':
    raise SystemExit(main())
