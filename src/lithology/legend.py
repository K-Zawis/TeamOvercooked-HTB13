"""Construct named lithology prototypes from extracted legend swatches."""

from collections import deque
from collections.abc import Mapping

lazy import numpy as np

from .features import compute_features
from .text import TesseractTextRecognizer, TextRecognizer
from .types import LegendPattern, TextToken


Box = tuple[int, int, int, int]


def _rgb(image) -> np.ndarray:
    pixels = np.asarray(image)
    if (
        pixels.ndim != 3
        or pixels.shape[2] != 3
        or not pixels.shape[0]
        or not pixels.shape[1]
        or pixels.dtype != np.uint8
    ):
        raise ValueError("expected a nonempty H x W x 3 uint8 RGB legend image")
    return pixels


def _ink(rgb: np.ndarray) -> np.ndarray:
    gray = (
        rgb[:, :, 0].astype(np.float64) * 0.299
        + rgb[:, :, 1].astype(np.float64) * 0.587
        + rgb[:, :, 2].astype(np.float64) * 0.114
    ) / 255.0
    threshold = min(0.85, float(np.quantile(gray, 0.65)))
    return gray < threshold


def _components(mask: np.ndarray):
    """Yield four-connected foreground component bounds and pixel counts."""
    height, width = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    for start_y, start_x in np.argwhere(mask):
        if seen[start_y, start_x]:
            continue
        queue = deque([(int(start_y), int(start_x))])
        seen[start_y, start_x] = True
        left = right = int(start_x)
        top = bottom = int(start_y)
        count = 0
        while queue:
            y, x = queue.pop()
            count += 1
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            for next_y, next_x in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if (
                    0 <= next_y < height
                    and 0 <= next_x < width
                    and mask[next_y, next_x]
                    and not seen[next_y, next_x]
                ):
                    seen[next_y, next_x] = True
                    queue.append((next_y, next_x))
        yield (left, top, right + 1, bottom + 1), count


def _edge_coverage(mask: np.ndarray, box: Box, band: int = 2) -> float:
    left, top, right, bottom = box
    region = mask[top:bottom, left:right]
    band = min(band, max(1, min(region.shape) // 2))
    edges = (
        region[:band].mean(),
        region[-band:].mean(),
        region[:, :band].mean(),
        region[:, -band:].mean(),
    )
    return float(np.mean(edges))


def find_swatch_boxes(
    image,
    *,
    min_width: int = 12,
    min_height: int = 8,
    max_area_fraction: float = 0.2,
    min_edge_coverage: float = 0.5,
) -> tuple[Box, ...]:
    """Find framed rectangular swatches in a cropped legend image.

    The detector is intentionally based on geometry rather than a fixed legend
    layout. Results are max-exclusive boxes ordered from top-to-bottom and then
    left-to-right.
    """
    rgb = _rgb(image)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (min_width, min_height)):
        raise ValueError("minimum swatch dimensions must be positive integers")
    if not 0 < max_area_fraction <= 1:
        raise ValueError("max_area_fraction must be in (0, 1]")
    if not 0 <= min_edge_coverage <= 1:
        raise ValueError("min_edge_coverage must be in [0, 1]")
    mask = _ink(rgb)
    image_area = mask.size
    boxes = []
    for box, count in _components(mask):
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        area = width * height
        if width < min_width or height < min_height:
            continue
        if area > image_area * max_area_fraction:
            continue
        if not 0.25 <= width / height <= 8:
            continue
        # Reject sparse text components while retaining outlined and solid boxes.
        if count / area < 0.08:
            continue
        if _edge_coverage(mask, box) < min_edge_coverage:
            continue
        boxes.append(box)
    return tuple(sorted(set(boxes), key=lambda box: (box[1], box[0], box[3], box[2])))


def _crop_swatch(rgb: np.ndarray, box: Box, inset: int) -> np.ndarray:
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise ValueError("swatch boxes must be (left, top, right, bottom)")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in box):
        raise ValueError("swatch coordinates must be integers")
    left, top, right, bottom = box
    if not (0 <= left < right <= rgb.shape[1] and 0 <= top < bottom <= rgb.shape[0]):
        raise ValueError("swatch box lies outside the legend image")
    if min(right - left, bottom - top) <= inset * 2 + 2:
        raise ValueError("swatch is too small after removing its frame")
    return np.ascontiguousarray(rgb[top + inset:bottom - inset, left + inset:right - inset])


def build_legend(swatches: Mapping[str, object]) -> tuple[LegendPattern, ...]:
    """Compute one pattern prototype for every named legend swatch.

    Locating swatches and recognizing their adjacent names are separate image
    parsing concerns. This function is the stable boundary between that parsing
    and lithology classification.
    """
    if not isinstance(swatches, Mapping) or not swatches:
        raise ValueError("swatches must be a nonempty mapping of names to RGB images")
    patterns = []
    for name, image in swatches.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("swatch names must be nonempty strings")
        patterns.append(LegendPattern(name, compute_features(image)))
    return tuple(patterns)


def build_legend_from_boxes(
    image,
    entries: Mapping[str, Box],
    *,
    inset: int = 2,
) -> tuple[LegendPattern, ...]:
    """Build named prototypes from swatch coordinates in a legend crop."""
    rgb = _rgb(image)
    if isinstance(inset, bool) or not isinstance(inset, int) or inset < 0:
        raise ValueError("inset must be a nonnegative integer")
    if not isinstance(entries, Mapping) or not entries:
        raise ValueError("entries must be a nonempty mapping of names to boxes")
    return build_legend({
        name: _crop_swatch(rgb, box, inset)
        for name, box in entries.items()
    })


def _text_lines(
    tokens: tuple[TextToken, ...],
    min_confidence: float,
) -> list[tuple[str, Box, float]]:
    accepted = [token for token in tokens if token.confidence >= min_confidence]
    accepted.sort(key=lambda token: ((token.box[1] + token.box[3]) / 2, token.box[0]))
    lines: list[list[TextToken]] = []
    for token in accepted:
        center = (token.box[1] + token.box[3]) / 2
        for line in lines:
            line_top = min(item.box[1] for item in line)
            line_bottom = max(item.box[3] for item in line)
            tolerance = max(2, (line_bottom - line_top) * 0.6)
            if line_top - tolerance <= center <= line_bottom + tolerance:
                line.append(token)
                break
        else:
            lines.append([token])
    output = []
    for row in lines:
        row.sort(key=lambda token: token.box[0])
        groups: list[list[TextToken]] = []
        for token in row:
            if groups:
                previous = groups[-1][-1]
                gap = token.box[0] - previous.box[2]
                text_height = max(
                    previous.box[3] - previous.box[1],
                    token.box[3] - token.box[1],
                )
                if gap <= max(4, text_height * 1.5):
                    groups[-1].append(token)
                    continue
            groups.append([token])
        for line in groups:
            box = (
                min(token.box[0] for token in line),
                min(token.box[1] for token in line),
                max(token.box[2] for token in line),
                max(token.box[3] for token in line),
            )
            output.append(
                (" ".join(token.text for token in line), box,
                 float(np.mean([token.confidence for token in line])))
            )
    return output


def match_labels(
    boxes: tuple[Box, ...] | list[Box],
    tokens: tuple[TextToken, ...] | list[TextToken],
    *,
    min_confidence: float = 0.4,
    max_vertical_gap: float = 1.5,
) -> dict[str, Box]:
    """Associate text lines immediately below their legend swatches."""
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be in [0, 1]")
    if not np.isfinite(max_vertical_gap) or max_vertical_gap <= 0:
        raise ValueError("max_vertical_gap must be positive and finite")
    lines = _text_lines(tuple(tokens), min_confidence)
    available = set(range(len(lines)))
    entries = {}
    for box in sorted(boxes, key=lambda value: (value[1], value[0])):
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        center_x = (left + right) / 2
        candidates = []
        for index in available:
            text, text_box, confidence = lines[index]
            text_left, text_top, text_right, _ = text_box
            text_center = (text_left + text_right) / 2
            gap = text_top - bottom
            if gap < -2 or gap > height * max_vertical_gap:
                continue
            if abs(text_center - center_x) > max(width, text_right - text_left):
                continue
            score = gap / max(height, 1) + abs(text_center - center_x) / max(width, 1)
            candidates.append((score, -confidence, index, text))
        if not candidates:
            continue
        _, _, index, text = min(candidates)
        if text in entries:
            continue
        entries[text] = box
        available.remove(index)
    return entries


def read_legend(
    image,
    recognizer: TextRecognizer | None = None,
    *,
    inset: int = 2,
    min_confidence: float = 0.4,
) -> tuple[LegendPattern, ...]:
    """Detect swatches, read their labels, and build named pattern prototypes."""
    rgb = _rgb(image)
    boxes = find_swatch_boxes(rgb)
    if not boxes:
        raise ValueError("no framed legend swatches were detected")
    reader = recognizer if recognizer is not None else TesseractTextRecognizer()
    entries = match_labels(
        boxes,
        tuple(reader.recognize(rgb)),
        min_confidence=min_confidence,
    )
    if not entries:
        raise ValueError("no legend labels could be associated with detected swatches")
    return build_legend_from_boxes(rgb, entries, inset=inset)
