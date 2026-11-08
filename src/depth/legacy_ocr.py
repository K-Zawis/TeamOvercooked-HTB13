"""Previous depth OCR adapted to bounded RGB strips, without raster/Redis IO.

No filename calibration, unit guessing, abbreviated-number expansion or extrapolation.
The fit retains the previous positive-slope, inlier, refit and competing-scale gates.
"""
import csv
import io
import math
import os
import re
import subprocess
import numpy as np
from PIL import Image, ImageOps

NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", re.ASCII)


def _long_runs(mask, minimum, axis):
    """Mark straight black runs, including edge runs, without an OpenCV dependency."""
    result = np.zeros_like(mask)
    lines = mask if axis == 1 else mask.T
    output = result if axis == 1 else result.T
    for line, dest in zip(lines, output):
        edges = np.diff(np.pad(line.astype(np.int8), (1, 1)))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        for start, end in zip(starts, ends):
            if end - start >= minimum:
                dest[start:end] = True
    return result


def recognize(image, executable="tesseract", timeout=30, scale=2, clean_rules=False):
    """Return normalized-confidence dictionaries in original input-local pixels.

    Errors propagate to the public adapter, which raises DepthExtractionError.
    Cleanup is a fallback pass, not evidence to union with another OCR pass.
    """
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8 or min(image.shape[:2]) < 1:
        raise ValueError("expected nonempty RGB uint8 image")
    height, width = image.shape[:2]
    if height * width > 12_000_000:
        raise ValueError("depth_tile_pixel_limit_exceeded")
    gray = np.array(ImageOps.autocontrast(Image.fromarray(image).convert("L")))
    if clean_rules:
        ink = gray < 160
        rules = _long_runs(ink, max(80, width // 2), 1)
        rules |= _long_runs(ink, max(80, height // 2), 0)
        gray[rules] = 255
    im = Image.fromarray(gray).resize((width * scale, height * scale), Image.Resampling.LANCZOS)
    border = 12
    im = ImageOps.expand(im, border=border, fill="white")
    buffer = io.BytesIO()
    im.save(buffer, format="PNG")
    proc = subprocess.run([executable, "stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"],
                          input=buffer.getvalue(), capture_output=True, timeout=timeout,
                          env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    if proc.returncode:
        raise RuntimeError("Tesseract failed: " + proc.stderr.decode(errors="replace")[:500])
    rows = csv.DictReader(io.StringIO(proc.stdout.decode()), delimiter="\t", quoting=csv.QUOTE_NONE)
    required = {"text", "conf", "left", "top", "width", "height"}
    if not rows.fieldnames or not required.issubset(rows.fieldnames):
        raise ValueError("missing TSV columns")
    words = []
    for row in rows:
        text = row["text"].strip()
        if not text:
            continue
        confidence = float(row["conf"]) / 100
        x, y = (float(row["left"]) - border) / scale, (float(row["top"]) - border) / scale
        w, h = float(row["width"]) / scale, float(row["height"]) / scale
        if not all(math.isfinite(v) for v in (confidence, x, y, w, h)) or w <= 0 or h <= 0:
            continue
        words.append(dict(text=text, confidence=confidence, box=(x, y, x+w, y+h)))
    return words


def _fit(anchors, height, config):
    minimum = max(3, int(config.get('min_anchors', 3)))
    if len(anchors) < minimum:
        return None, 'insufficient_numeric_anchors'
    # Cap pair hypotheses rather than running an unbounded random RANSAC.
    sample = anchors if len(anchors) <= 32 else [anchors[i] for i in np.linspace(0, len(anchors)-1, 32).astype(int)]
    ys = np.array([a['y'] for a in anchors])
    ds = np.array([a['depth'] for a in anchors])
    best = None
    tol_px = float(config.get('residual_pixels', 5.))
    min_span = float(config.get('min_span_fraction', .35)) * height
    for i, a in enumerate(sample):
        for b in sample[i+1:]:
            if abs(b['y']-a['y']) < max(10, min_span*.3):
                continue
            slope = (b['depth']-a['depth'])/(b['y']-a['y'])
            if not math.isfinite(slope) or slope <= 0:
                continue
            intercept = a['depth']-slope*a['y']
            residual = np.abs(ds-(slope*ys+intercept))/abs(slope)
            mask = residual <= tol_px
            count = int(mask.sum())
            if count < minimum:
                continue
            span = float(np.ptp(ys[mask]))
            score = (count, span, -float(np.median(residual[mask])))
            if best is None or score > best[0]:
                best = (score, mask)
    if best is None:
        return None, 'no_consistent_fit'
    mask = best[1]
    if mask.sum()/len(anchors) < float(config.get('min_inlier_fraction', .6)):
        return None, 'too_many_outliers'
    if np.ptp(ys[mask]) < min_span:
        return None, 'insufficient_vertical_coverage'
    slope, intercept = np.polyfit(ys[mask], ds[mask], 1)
    if not math.isfinite(slope) or slope <= 0:
        return None, 'nonincreasing_depth_scale'
    residual = np.abs(ds-(slope*ys+intercept))/abs(slope)
    # A boundary inlier can move beyond tolerance during refit. Remove it,
    # never increase tolerance; enforce the original support gates again.
    for _ in range(8):
        trimmed = mask & (residual <= tol_px)
        if np.array_equal(trimmed, mask):
            break
        mask = trimmed
        if mask.sum() < minimum or mask.sum()/len(anchors) < float(config.get('min_inlier_fraction', .6)):
            return None, 'refit_insufficient_inliers'
        if np.ptp(ys[mask]) < min_span:
            return None, 'refit_insufficient_coverage'
        slope, intercept = np.polyfit(ys[mask], ds[mask], 1)
        residual = np.abs(ds-(slope*ys+intercept))/abs(slope)
    if not math.isfinite(slope) or slope <= 0 or np.max(residual[mask]) > tol_px:
        return None, 'refit_residual_exceeded'
    for a, ok, r in zip(anchors, mask, residual):
        a['inlier'] = bool(ok)
        a['residual_pixels'] = float(r)
    return {'slope': float(slope), 'intercept': float(intercept),
            'inliers': int(mask.sum()), 'outliers': int((~mask).sum()),
            'rms_pixels': float(np.sqrt(np.mean(residual[mask]**2))),
            'span_pixels': float(np.ptp(ys[mask])),
            'valid_y_range': [float(ys[mask].min()), float(ys[mask].max())],
            'extrapolation_allowed': False}, None



def supported_anchors(anchors, tolerance):
    """Conservative previous-fit gates over retained stream observations.

    Independent broker observations are calibrated globally by the merger. No fitted
    values replace observations; callers retain piecewise, support-only mapping.
    """
    if len(anchors) < 3:
        return []
    rows = [dict(y=a.y, depth=a.depth, text=a.text or str(a.depth), confidence=a.confidence)
            for a in anchors]
    full = [a for a in rows if len(a['text'].lstrip('+-').replace(',', '').split('.')[0]) > 2]
    repeated = {a['text'] for a in rows if sum(b['text'] == a['text'] for b in rows) >= 3}
    usable = [a for a in rows if not (len(full) >= 3 and a not in full and a['text'] in repeated)]
    if len(usable) < 3:
        return []
    height = max(a['y'] for a in usable) - min(a['y'] for a in usable)
    if height <= 0:
        return []
    decimal = [a for a in usable if '.' in a['text']]
    integer = [a for a in usable if '.' not in a['text']]
    if all(len(group) >= 3 and np.ptp([a['y'] for a in group]) >= .35 * height
           for group in (decimal, integer)):
        return []
    fit, _ = _fit(usable, height, dict(residual_pixels=tolerance))
    if not fit:
        return []
    remaining = [dict(a) for a in usable if not a.get('inlier')]
    other, _ = _fit(remaining, height, dict(residual_pixels=tolerance))
    if other:
        return []
    accepted = [a for a, row in zip(anchors, rows) if row.get('inlier')]
    if any(b.depth <= a.depth for a,b in zip(accepted, accepted[1:])):
        return []
    return accepted
