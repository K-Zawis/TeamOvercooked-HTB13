"""Deterministic color and texture descriptors for lithology patterns."""

lazy import numpy as np

from .types import FeatureWindow, PatternFeatures


def _rgb(image) -> np.ndarray:
    pixels = np.asarray(image)
    if (
        pixels.ndim != 3
        or pixels.shape[2] != 3
        or pixels.shape[0] < 3
        or pixels.shape[1] < 3
        or pixels.dtype != np.uint8
    ):
        raise ValueError("expected an H x W x 3 uint8 RGB image at least 3 x 3")
    return pixels


def _normalize(values) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    norm = float(np.linalg.norm(values))
    return values / norm if norm else values


def _gray(rgb: np.ndarray) -> np.ndarray:
    # ITU-R BT.601 luminance; retain RGB separately for the color descriptor.
    return (
        rgb[:, :, 0].astype(np.float64) * 0.299
        + rgb[:, :, 1].astype(np.float64) * 0.587
        + rgb[:, :, 2].astype(np.float64) * 0.114
    ) / 255.0


def _lbp(gray: np.ndarray) -> np.ndarray:
    """Ten-bin rotation-tolerant uniform LBP histogram."""
    center = gray[1:-1, 1:-1]
    neighbors = (
        gray[:-2, :-2], gray[:-2, 1:-1], gray[:-2, 2:],
        gray[1:-1, 2:], gray[2:, 2:], gray[2:, 1:-1],
        gray[2:, :-2], gray[1:-1, :-2],
    )
    bits = np.stack([neighbor >= center for neighbor in neighbors], axis=0)
    transitions = np.count_nonzero(bits != np.roll(bits, 1, axis=0), axis=0)
    ones = bits.sum(axis=0)
    codes = np.where(transitions <= 2, ones, 9)
    histogram = np.bincount(codes.ravel(), minlength=10).astype(np.float64)
    return histogram / histogram.sum()


def _color(rgb: np.ndarray) -> np.ndarray:
    values = rgb.astype(np.float64) / 255.0
    high = values.max(axis=2)
    low = values.min(axis=2)
    saturation = np.divide(
        high - low,
        high,
        out=np.zeros_like(high),
        where=high > 0,
    )
    descriptor = np.concatenate(
        [
            values.mean(axis=(0, 1)),
            values.std(axis=(0, 1)),
            np.array([saturation.mean(), saturation.std()]),
        ]
    )
    return _normalize(descriptor)


def _edges(gray: np.ndarray) -> np.ndarray:
    dy, dx = np.gradient(gray)
    magnitude = np.hypot(dx, dy)
    # Edge orientation is unsigned: a dark-to-light and light-to-dark edge have
    # the same structural direction.
    orientation = np.mod(np.arctan2(dy, dx), np.pi)
    histogram, _ = np.histogram(
        orientation,
        bins=8,
        range=(0, np.pi),
        weights=magnitude,
    )
    return _normalize(histogram)


def _density(gray: np.ndarray) -> np.ndarray:
    # A relative threshold handles white, yellowed, or mildly gray paper while
    # keeping the descriptor deterministic for binary scans.
    threshold = min(0.85, float(np.quantile(gray, 0.65)))
    ink = gray < threshold
    rows = ink.mean(axis=1)
    columns = ink.mean(axis=0)
    return _normalize(
        np.array(
            [
                ink.mean(),
                rows.mean(), rows.std(), rows.max(),
                columns.mean(), columns.std(), columns.max(),
            ],
            dtype=np.float64,
        )
    )


def compute_features(image) -> PatternFeatures:
    """Describe one RGB bed or legend swatch using the same feature pipeline."""
    rgb = _rgb(image)
    gray = _gray(rgb)
    lbp = _lbp(gray)
    color = _color(rgb)
    edges = _edges(gray)
    density = _density(gray)
    combined = _normalize(
        np.concatenate(
            [
                lbp,
                color,
                edges,
                density,
            ]
        )
    )
    return PatternFeatures(lbp, color, edges, density, combined)


def _windows(height: int, window_height: int, stride: int) -> list[tuple[int, int]]:
    if height <= window_height:
        return [(0, height)]
    starts = list(range(0, height - window_height + 1, stride))
    final = height - window_height
    if starts[-1] != final:
        starts.append(final)
    return [(start, start + window_height) for start in starts]


def compute_feature_map(
    image,
    *,
    global_y: int = 0,
    window_height: int = 64,
    stride: int = 16,
) -> tuple[FeatureWindow, ...]:
    """Compute overlapping texture descriptors down a lithology track."""
    pixels = _rgb(image)
    if isinstance(global_y, bool) or not isinstance(global_y, int) or global_y < 0:
        raise ValueError("global_y must be a nonnegative integer")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (window_height, stride)):
        raise ValueError("window_height and stride must be positive integers")
    return tuple(
        FeatureWindow(
            global_y + top,
            global_y + bottom,
            compute_features(pixels[top:bottom]),
        )
        for top, bottom in _windows(len(pixels), window_height, stride)
    )
