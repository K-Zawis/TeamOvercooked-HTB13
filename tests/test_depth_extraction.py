import io
import os
import shutil
import subprocess
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from src.depth.extraction import (
    DepthExtractionError,
    OCRToken,
    TesseractOCR,
    _grayscale,
    _number,
)
from src.depth import DepthExtractor


def test_numpy_grayscale_preserves_rgb_luminance():
    rgb = np.random.default_rng(17).integers(0, 256, (100, 80, 3), dtype=np.uint8)
    np.testing.assert_array_equal(_grayscale(rgb), np.array(Image.fromarray(rgb).convert("L")))


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_tesseract_receives_bounded_graymap_with_white_border(scale):
    rgb = np.full((100, 80, 3), 255, dtype=np.uint8)
    rgb[30:70, 20:60] = 0
    result = subprocess.CompletedProcess([], 0, b"text\tconf\tleft\ttop\twidth\theight\n", b"")
    with patch("src.depth.extraction.subprocess.run", return_value=result) as run:
        assert TesseractOCR(scale=scale).recognize(rgb) == []
    encoded = run.call_args.kwargs["input"]
    assert encoded.startswith(b"P5\n")
    with Image.open(io.BytesIO(encoded)) as image:
        pixels = np.array(image)
    assert pixels.shape == (100 * scale + 24, 80 * scale + 24)
    assert np.all(pixels[:12] == 255) and np.all(pixels[-12:] == 255)
    assert np.all(pixels[:, :12] == 255) and np.all(pixels[:, -12:] == 255)
    assert pixels[len(pixels)//2, pixels.shape[1]//2] == 0


def token(y, value, confidence=0.95, box=None):
    return OCRToken(str(value), confidence, box or (10, y - 5, 40, y + 5))


class ScriptedOCR:
    def __init__(self, *strips):
        self.calls = iter([result for strip in strips for result in (strip, [], [])])

    def recognize(self, image):
        return next(self.calls)


def strip(height=400, width=80):
    return np.full((height, width, 3), 255, dtype=np.uint8)


@pytest.mark.parametrize(
    "text,value",
    [
        ("1,234.5", 1234.5),
        ("-12.5", -12.5),
        ("+20", 20),
        ("\u221210", -10),
        (" 17200 ", 17200),
        ("00", 0),
        ("1,20", None),
        ("MD", None),
        ("12ft", None),
        ("1e3", None),
        ("NaN", None),
        ("12 34", None),
        ("1.2.3", None),
        ("9" * 400, None),
    ],
)
def test_numeric_parser(text, value):
    assert _number(text) == value


def test_independent_observations_emit_one_or_two_labels_immediately():
    extractor = DepthExtractor(ScriptedOCR([token(50, 1000)],
                                           [token(100, 2000), token(200, 2100)]))
    assert extractor.extract_observations(strip()) == {"y_to_depth_map": {50: 1000}}
    assert extractor.extract_observations(strip(300)) == {"y_to_depth_map": {100: 2000, 200: 2100}}
    assert extractor._offset == 0 and extractor._tail is None
    assert extractor._candidates == [] and extractor.depth_at(100) is None


def test_observations_preserve_existing_legacy_calibration():
    extractor = DepthExtractor(ScriptedOCR(
        [token(50, 100), token(150, 200), token(250, 300)], [token(75, 999)]))
    extractor.extract(strip())
    old_tail = extractor._tail.copy()
    assert extractor.extract_observations(strip(200, 60)) == {"y_to_depth_map": {75: 999}}
    assert extractor._offset == 400 and extractor._width == 80
    assert extractor.depth_at(100) == 150
    np.testing.assert_array_equal(extractor._tail, old_tail)


def test_observations_do_not_truncate_large_task_results():
    extractor = DepthExtractor(ScriptedOCR([token(y, y) for y in range(20, 3020, 10)]))
    assert len(extractor.extract_observations(strip(3100))["y_to_depth_map"]) == 300


def test_sparse_anchors_and_piecewise_interpolation():
    extractor = DepthExtractor(
        ScriptedOCR(
            [
                token(50, 1000),
                token(150, 1020),
                token(250, 1041),
                token(350, 9999),
            ]
        )
    )
    assert extractor.extract(strip()) == {
        "y_to_depth_map": {50: 1000, 150: 1020, 250: 1041}
    }
    assert extractor.depth_at(100) == 1010
    assert extractor.depth_at(200) == 1030.5
    for y in [49, 251, float("nan"), float("inf"), True, None, "100", 1 + 2j, 10**1000]:
        assert extractor.depth_at(y) is None


def test_pending_labels_emitted_in_later_strip_and_reset():
    backend = ScriptedOCR(
        [token(50, 1000), token(150, 1020)], [token(28, 1020), token(128, 1040)], []
    )
    extractor = DepthExtractor(backend)
    assert extractor.extract(strip(250)) == {"y_to_depth_map": {}}
    # Combined origin is 122: y=28 repeats prior anchor at 150, y=128 is 250.
    assert extractor.extract(strip(75)) == {
        "y_to_depth_map": {-200: 1000, -100: 1020, 0: 1040}
    }
    assert extractor.depth_at(200) == 1030
    extractor.reset()
    assert extractor.depth_at(200) is None
    assert extractor.extract(strip(20, 50)) == {"y_to_depth_map": {}}
    assert extractor._offset == 20


def test_seam_recovery_and_duplicate_suppression():
    backend = ScriptedOCR(
        [token(30, 100), token(80, 110), token(148, 120)],
        [token(58, 110), token(128, 124), token(178, 134)],
    )
    # First token at 148 extends beyond the first 150-row strip: reject it.
    extractor = DepthExtractor(backend)
    assert extractor.extract(strip(150))["y_to_depth_map"] == {}
    result = extractor.extract(strip(100))["y_to_depth_map"]
    assert result == {-120: 100, -70: 110, 0: 124, 50: 134}
    assert len(extractor._tail) == 128


def test_overlap_box_jitter_does_not_emit_duplicate_row():
    extractor = DepthExtractor(
        ScriptedOCR(
            [token(100, 100), token(200, 200), token(350, 350)],
            [token(79, 350, 0.99), token(178, 450)],
        )
    )
    assert len(extractor.extract(strip())["y_to_depth_map"]) == 3
    assert extractor.extract(strip(200))["y_to_depth_map"] == {50: 450}
    assert extractor.depth_at(350) == 350


def test_repeated_reading_does_not_erase_conflicting_evidence():
    extractor = DepthExtractor(
        ScriptedOCR(
            [token(100, 100), token(200, 200), token(350, 350), token(350, 999, 0.8)],
            [token(78, 350, 0.99), token(178, 450)],
        )
    )
    assert extractor.extract(strip())["y_to_depth_map"] == {}
    result = extractor.extract(strip(200))["y_to_depth_map"]
    assert result == {-300: 100, -200: 200, 50: 450}
    assert any(a.depth == 999 for a in extractor._candidates)


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((0, 10, 3), np.uint8),
        np.zeros((10, 0, 3), np.uint8),
        np.zeros((20, 10), np.uint8),
        np.zeros((20, 10, 4), np.uint8),
        np.zeros((20, 10, 3), float),
        None,
    ],
)
def test_invalid_input_does_not_advance(bad):
    extractor = DepthExtractor(ScriptedOCR([]))
    with pytest.raises(ValueError):
        extractor.extract(bad)
    assert extractor._offset == 0 and extractor._tail is None


def test_width_change_does_not_advance():
    extractor = DepthExtractor(ScriptedOCR([]))
    extractor.extract(strip())
    with pytest.raises(ValueError, match="width"):
        extractor.extract(strip(width=79))
    assert extractor._offset == 400


def test_failure_mid_orientation_does_not_commit():
    class FailingOCR:
        def recognize(self, image):
            if image.shape[0] == 80:
                raise DepthExtractionError("timeout")
            return [token(50, 100), token(150, 200), token(250, 300)]

    extractor = DepthExtractor(FailingOCR())
    with pytest.raises(DepthExtractionError):
        extractor.extract(strip())
    assert extractor._offset == 0 and extractor._candidates == []
    extractor.ocr_backend = ScriptedOCR(
        [token(50, 100), token(150, 200), token(250, 300)]
    )
    assert len(extractor.extract(strip())["y_to_depth_map"]) == 3


def test_failure_preserves_existing_calibration_and_tail():
    extractor = DepthExtractor(
        ScriptedOCR([token(50, 100), token(150, 200), token(250, 300)])
    )
    extractor.extract(strip())
    before = (
        extractor._offset,
        list(extractor._candidates),
        dict(extractor._emitted),
        extractor._tail.copy(),
        extractor.depth_at(100),
    )

    class BrokenOCR:
        def recognize(self, image):
            raise DepthExtractionError("OCR unavailable")

    extractor.ocr_backend = BrokenOCR()
    with pytest.raises(DepthExtractionError):
        extractor.extract(strip(200))
    assert extractor._offset == before[0]
    assert extractor._candidates == before[1]
    assert extractor._emitted == before[2]
    np.testing.assert_array_equal(extractor._tail, before[3])
    assert extractor.depth_at(100) == before[4]


def test_conflicts_noise_and_decreasing_depths_rejected():
    for tokens in [
        [token(50, 300), token(150, 200), token(250, 100)],
        [token(50, 100), token(150, 200), token(250, 300), token(251, 400)],
        [token(50, 100), token(150, 200, 0.1), token(250, 300)],
        [token(50, 100), token(150, "MD"), token(250, 300)],
    ]:
        extractor = DepthExtractor(ScriptedOCR(tokens))
        assert extractor.extract(strip())["y_to_depth_map"] == {}
        assert extractor.depth_at(150) is None


@pytest.mark.parametrize("rotation", [1, 3])
def test_rotated_ocr_boxes(rotation):
    class RotatedOCR:
        calls = 0

        def recognize(self, image):
            self.calls += 1
            if self.calls != (2 if rotation == 1 else 3):
                return []
            # Convert the desired original boxes into rotated coordinates.
            boxes = []
            for y, value in [(50, 100), (150, 200), (250, 300)]:
                box = (
                    (y - 5, 40, y + 5, 70)
                    if rotation == 1
                    else (400 - y - 5, 10, 400 - y + 5, 40)
                )
                boxes.append(token(y, value, box=box))
            return boxes

    extractor = DepthExtractor(RotatedOCR())
    assert extractor.extract(strip())["y_to_depth_map"] == {50: 100, 150: 200, 250: 300}


def test_unambiguous_tick_alignment():
    image = strip()
    for y in [52, 152, 252]:
        image[y, 43:] = 0
    extractor = DepthExtractor(
        ScriptedOCR([token(50, 100), token(150, 200), token(250, 300)])
    )
    assert extractor.extract(image)["y_to_depth_map"] == {52: 100, 152: 200, 252: 300}


def test_bounded_state_and_input_not_retained():
    labels = [token(y, y) for y in range(20, 3020, 10)]
    extractor = DepthExtractor(ScriptedOCR(labels, []))
    image = strip(3100)
    extractor.extract(image)
    assert len(extractor._candidates) <= 256
    assert len(extractor._emitted) <= 256
    assert extractor._tail.shape == (128, 80, 3)
    assert not np.shares_memory(image, extractor._tail)
    assert extractor.depth_at(20) is None  # dropped historical support
    assert extractor.depth_at(3000) == 3000
    assert extractor.extract(strip(1))["y_to_depth_map"] == {}


def test_many_strips_expire_old_support_and_keep_state_bounded():
    batches = []
    for index in range(75):
        prefix = 128 if index else 0
        batches.append(
            [token(prefix + y, index * 400 + y) for y in [50, 150, 250, 350]]
        )
    extractor = DepthExtractor(ScriptedOCR(*batches))
    emitted_count = 0
    for _ in batches:
        emitted_count += len(extractor.extract(strip())["y_to_depth_map"])
        assert len(extractor._candidates) <= 256
        assert len(extractor._emitted) <= 256
        assert len(extractor._tail) == 128
    assert emitted_count == 300
    assert extractor.depth_at(50) is None
    assert extractor.depth_at(29900) == 29900


def test_backend_errors_and_tsv_coordinates():
    backend = TesseractOCR()
    with patch("src.depth.extraction.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(DepthExtractionError, match="install tesseract"):
            backend.recognize(strip())
    with patch(
        "src.depth.extraction.subprocess.run",
        side_effect=subprocess.TimeoutExpired("tesseract", 30),
    ):
        with pytest.raises(DepthExtractionError, match="execution failed"):
            backend.recognize(strip())
    # OCR quotes must be literal: CSV quoting would swallow subsequent TSV rows.
    tsv = b'text\tconf\tleft\ttop\twidth\theight\n"\t50\t0\t0\t2\t2\n1234\t95\t32\t52\t60\t20\n'
    with patch(
        "src.depth.extraction.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, tsv, b""),
    ):
        assert backend.recognize(strip())[-1] == OCRToken(
            "1234", 0.95, (10, 20, 40, 30)
        )
    for result in [
        subprocess.CompletedProcess([], 1, b"", b"bad language data"),
        subprocess.CompletedProcess([], 0, b"not tsv", b""),
    ]:
        with patch("src.depth.extraction.subprocess.run", return_value=result):
            with pytest.raises(DepthExtractionError):
                backend.recognize(strip())


@pytest.mark.parametrize("strip_height", [71, 200, 600])
def test_actual_tesseract(strip_height):
    executable = os.environ.get("TESSERACT_BIN", "tesseract")
    if not shutil.which(executable):
        pytest.skip("Tesseract is not installed")
    # Pillow's bundled font avoids a system-font dependency.
    image = Image.new("RGB", (180, 600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=26)
    for y, depth in [(50, 1000), (200, 1100), (350, 1200), (500, 1300)]:
        draw.text((20, y), str(depth), fill="black", font=font)
    extractor = DepthExtractor(TesseractOCR(executable))
    pixels = np.array(image)
    result = {}
    for offset in range(0, len(pixels), strip_height):
        local = extractor.extract(pixels[offset : offset + strip_height])[
            "y_to_depth_map"
        ]
        result.update({y + offset: depth for y, depth in local.items()})
    assert set(result.values()) == {1000, 1100, 1200, 1300}
    assert extractor.depth_at(285) == pytest.approx(1150, abs=10)
