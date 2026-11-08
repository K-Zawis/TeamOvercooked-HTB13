"""Bounded, document-local depth OCR. No lithology or whole-image loading."""

import csv
import io
import math
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable, Protocol

lazy import numpy as np
lazy from PIL import Image, ImageOps


@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float  # Normalized to [0, 1].
    box: tuple[float, float, float, float]  # xyxy in backend input pixels.


class OCRBackend(Protocol):
    def recognize(self, image: np.ndarray) -> Iterable[OCRToken]:
        """Recognize RGB pixels; return input-local boxes, regardless of resizing."""
        ...


class DepthExtractionError(RuntimeError):
    """An OCR failure. The same strip can be retried without resetting."""


class TesseractOCR:
    """CPU backend; Tesseract and its English language data must be installed."""

    def __init__(
        self, executable: str = "tesseract", timeout: float = 30, scale: int = 2
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if isinstance(scale, bool) or scale not in (1, 2, 3):
            raise ValueError("scale must be 1, 2, or 3")
        self.executable, self.timeout, self.scale = executable, timeout, scale

    def recognize(self, image: np.ndarray) -> list[OCRToken]:
        from .legacy_ocr import recognize, NUMBER
        try:
            words = recognize(image, self.executable, self.timeout, self.scale)
            # Re-run only weak passes; do not merge competing preprocessing results.
            numeric = [w for w in words if NUMBER.fullmatch(w["text"]) and w["confidence"] >= .5]
            if len(numeric) < 3:
                cleaned = recognize(image, self.executable, self.timeout, self.scale, clean_rules=True)
                clean_numeric = [w for w in cleaned if NUMBER.fullmatch(w["text"]) and w["confidence"] >= .5]
                if len(clean_numeric) > len(numeric):
                    words = cleaned
        except FileNotFoundError as exc:
            raise DepthExtractionError("Tesseract is unavailable: install tesseract and English language data") from exc
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise DepthExtractionError(f"Tesseract execution/output failed: {exc}") from exc
        return [OCRToken(w["text"], w["confidence"], w["box"]) for w in words]


@dataclass(frozen=True)
class _Anchor:
    y: int
    depth: float
    confidence: float
    text: str = ""


_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", re.ASCII)


def _number(text: str) -> float | None:
    text = text.strip().replace("\N{MINUS SIGN}", "-")
    if not _NUMBER.fullmatch(text):
        return None
    value = float(text.replace(",", ""))
    return value if math.isfinite(value) else None


def _original_box(box, rotation, width, height):
    x0, y0, x1, y1 = box
    if rotation == 1:  # np.rot90: counterclockwise
        return width - y1, x0, width - y0, x1
    if rotation == 3:
        return y0, height - x1, y1, height - x0
    return x0, y0, x1, y1


def _consensus(anchors, tolerance):
    from .legacy_ocr import supported_anchors
    return supported_anchors(anchors, tolerance)


class DepthExtractor:
    """Ordered contiguous RGB strips, with no implicit document-boundary guessing.

    extract() returns newly trusted anchors in current-strip-local coordinates.
    Pending labels can be emitted later with negative local y; adding the current
    strip's offset still gives their correct image position. depth_at() instead
    accepts y relative to the start of the stream. Call reset() for every log.
    """

    def __init__(
        self,
        ocr_backend: OCRBackend | None = None,
        *,
        min_confidence: float = 0.5,
        residual_pixels: float = 5,
    ):
        if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be in [0, 1]")
        if not math.isfinite(residual_pixels) or residual_pixels <= 0:
            raise ValueError("residual_pixels must be positive and finite")
        self.ocr_backend = ocr_backend if ocr_backend is not None else TesseractOCR()
        self.min_confidence = min_confidence
        self.residual_pixels = residual_pixels
        self.reset()

    def reset(self):
        """Start a new document, discarding calibration and seam pixels."""
        self._offset = 0
        self._width = None
        self._tail = None
        self._candidates = []
        self._trusted = []
        self._emitted = {}

    @staticmethod
    def _merge(anchors, limit=256):
        groups = []
        for anchor in sorted(anchors, key=lambda a: a.y):
            if groups and anchor.y - groups[-1][0].y <= 3:
                groups[-1].append(anchor)
            else:
                groups.append([anchor])
        merged = []
        for group in groups:
            readings = {}
            for anchor in group:
                if (
                    anchor.depth not in readings
                    or anchor.confidence > readings[anchor.depth].confidence
                ):
                    readings[anchor.depth] = anchor
            if len(readings) == 1:
                merged.append(next(iter(readings.values())))
            # Preserve conflicting evidence so later calls cannot silently accept
            # one side of a previously ambiguous row.
            else:
                merged.extend(
                    sorted(readings.values(), key=lambda a: a.confidence, reverse=True)[
                        :2
                    ]
                )
        merged = sorted(merged, key=lambda a: a.y)
        return merged[-limit:] if limit is not None else merged

    @staticmethod
    def _tick_y(gray, box):
        """Use an isolated long horizontal tick near the label, if one exists."""
        x0, y0, x1, y1 = box
        centre = (y0 + y1) / 2
        radius = min(8, max(2, int((y1 - y0) / 2)))
        start, end = (
            max(0, round(centre) - radius),
            min(len(gray), round(centre) + radius + 1),
        )
        # Search outside the text box, avoiding glyph strokes.
        left, right = max(0, int(x0) - 2), min(gray.shape[1], math.ceil(x1) + 2)
        regions = [gray[start:end, :left], gray[start:end, right:]]
        hits = []
        for region in regions:
            if region.shape[1] < 6:
                continue
            rows = np.flatnonzero((region < 128).mean(axis=1) >= 0.8) + start
            hits.extend(rows.tolist())
        rows = sorted(set(hits))
        if rows and rows[-1] - rows[0] <= 2:
            return round(sum(rows) / len(rows))
        return round(centre)

    @staticmethod
    def _validate_image(depth_crop):
        if (
            not isinstance(depth_crop, np.ndarray)
            or depth_crop.dtype != np.uint8
            or depth_crop.ndim != 3
            or depth_crop.shape[2] != 3
            or min(depth_crop.shape[:2]) < 1
        ):
            raise ValueError("depth_crop must be a nonempty H x W x 3 RGB uint8 array")

    def _read_anchors(self, image, origin=0):
        height, width = image.shape[:2]
        gray = np.array(Image.fromarray(image).convert("L"))
        found = []
        for rotation in (0, 1, 3):
            for token in self.ocr_backend.recognize(
                np.ascontiguousarray(np.rot90(image, rotation))
            ):
                value = _number(token.text)
                if (
                    value is None
                    or not math.isfinite(token.confidence)
                    or not self.min_confidence <= token.confidence <= 1
                ):
                    continue
                if len(token.box) != 4 or not all(math.isfinite(v) for v in token.box):
                    continue
                x0, y0, x1, y1 = box = _original_box(token.box, rotation, width, height)
                # A clipped label is deferred until it is fully visible in the tail.
                if not (0 < x0 < x1 < width and 0 < y0 < y1 < height):
                    continue
                y = origin + self._tick_y(gray, box)
                found.append(_Anchor(y, value, token.confidence, token.text.strip()))
        return found

    def extract_observations(
        self, depth_crop: np.ndarray
    ) -> dict[str, dict[int, float]]:
        """Extract one independent task without buffering labels or calibrating.

        Return every confident, unclipped, unambiguous numeric observation,
        including a lone label. Global calibration/interpolation belongs to the
        merger. This method never reads or updates this object's stream state.
        """
        self._validate_image(depth_crop)
        candidates = self._merge(self._read_anchors(depth_crop), limit=None)
        unambiguous = [
            a
            for a in candidates
            if not any(abs(a.y - b.y) <= 3 and a.depth != b.depth for b in candidates)
        ]
        return {"y_to_depth_map": {a.y: a.depth for a in unambiguous}}

    def extract(self, depth_crop: np.ndarray) -> dict[str, dict[int, float]]:
        """Consume one strip atomically; failures leave state unchanged."""
        self._validate_image(depth_crop)
        if self._width is not None and depth_crop.shape[1] != self._width:
            raise ValueError("strip width changed; call reset() before a new document")
        tail_height = 0 if self._tail is None else len(self._tail)
        image = (
            depth_crop
            if self._tail is None
            else np.concatenate((self._tail, depth_crop))
        )
        found = self._read_anchors(image, self._offset - tail_height)
        width = image.shape[1]
        candidates = self._merge([*self._candidates, *found])
        # Keep a previously emitted row stable when overlap OCR moves its box
        # by a couple of pixels. Downstream dictionaries cannot retract rows.
        candidates = [
            _Anchor(
                next(
                    (
                        y
                        for y, depth in self._emitted.items()
                        if abs(y - a.y) <= 3 and depth == a.depth
                    ),
                    a.y,
                ),
                a.depth,
                a.confidence,
                a.text,
            )
            for a in candidates
        ]
        candidates.sort(key=lambda a: a.y)
        # Rows with conflicting numeric readings cannot support a calibration.
        unambiguous = [
            a
            for a in candidates
            if not any(abs(a.y - b.y) <= 3 and a.depth != b.depth for b in candidates)
        ]
        trusted = _consensus(unambiguous, self.residual_pixels)
        emitted = {a.y: self._emitted[a.y] for a in candidates if a.y in self._emitted}
        output = {}
        for anchor in trusted:
            if emitted.get(anchor.y) != anchor.depth:
                output[anchor.y - self._offset] = anchor.depth
                emitted[anchor.y] = anchor.depth
        # All potentially failing work precedes the state commit.
        tail = image[-128:].copy()
        self._candidates, self._trusted, self._emitted = candidates, trusted, emitted
        self._tail, self._width = tail, width
        self._offset += len(depth_crop)
        return {"y_to_depth_map": output}

    def depth_at(self, y: float) -> float | None:
        """Interpolate in stream-relative pixels, inside retained support only."""
        if (
            isinstance(y, (bool, np.bool_))
            or not isinstance(y, (int, float, np.integer, np.floating))
            or not self._trusted
            or not self._trusted[0].y <= y <= self._trusted[-1].y
        ):
            return None
        return float(
            np.interp(y, [a.y for a in self._trusted], [a.depth for a in self._trusted])
        )
