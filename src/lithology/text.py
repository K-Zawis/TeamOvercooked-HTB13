"""Replaceable OCR used to read names adjacent to legend swatches."""

import csv
import io
import math
import os
import subprocess
from typing import Iterable, Protocol

lazy import numpy as np
lazy from PIL import Image, ImageOps

from .types import TextToken


class LegendRecognitionError(RuntimeError):
    """Legend text recognition could not be completed."""


class TextRecognizer(Protocol):
    def recognize(self, image: np.ndarray) -> Iterable[TextToken]: ...


class TesseractTextRecognizer:
    """Recognize legend text with a local Tesseract installation."""

    def __init__(
        self,
        executable: str = "tesseract",
        *,
        timeout: float = 30,
        scale: int = 2,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if isinstance(scale, bool) or scale not in (1, 2, 3):
            raise ValueError("scale must be 1, 2, or 3")
        self.executable = executable
        self.timeout = timeout
        self.scale = scale

    def recognize(self, image: np.ndarray) -> list[TextToken]:
        pixels = np.asarray(image)
        if (
            pixels.ndim != 3
            or pixels.shape[2] != 3
            or not pixels.shape[0]
            or not pixels.shape[1]
            or pixels.dtype != np.uint8
        ):
            raise ValueError("expected a nonempty H x W x 3 uint8 RGB image")
        prepared = ImageOps.autocontrast(Image.fromarray(pixels).convert("L"))
        prepared = prepared.resize(
            (prepared.width * self.scale, prepared.height * self.scale),
            Image.Resampling.LANCZOS,
        )
        border = 12
        prepared = ImageOps.expand(prepared, border=border, fill="white")
        encoded = io.BytesIO()
        prepared.save(encoded, format="PNG")
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "stdin",
                    "stdout",
                    "-l",
                    "eng",
                    "--psm",
                    "11",
                    "tsv",
                ],
                input=encoded.getvalue(),
                capture_output=True,
                timeout=self.timeout,
                env=dict(os.environ, OMP_THREAD_LIMIT="1"),
            )
        except FileNotFoundError as exc:
            raise LegendRecognitionError("Tesseract is unavailable") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LegendRecognitionError(f"Tesseract execution failed: {exc}") from exc
        if result.returncode:
            raise LegendRecognitionError(
                "Tesseract failed: " + result.stderr.decode(errors="replace")[:500]
            )
        try:
            rows = csv.DictReader(
                io.StringIO(result.stdout.decode()),
                delimiter="\t",
                quoting=csv.QUOTE_NONE,
            )
            required = {"text", "conf", "left", "top", "width", "height"}
            if not rows.fieldnames or not required.issubset(rows.fieldnames):
                raise ValueError("missing TSV columns")
            tokens = []
            for row in rows:
                text = row["text"].strip()
                confidence = float(row["conf"]) / 100
                if not text or not 0 <= confidence <= 1:
                    continue
                left = round((int(row["left"]) - border) / self.scale)
                top = round((int(row["top"]) - border) / self.scale)
                width = max(1, round(int(row["width"]) / self.scale))
                height = max(1, round(int(row["height"]) / self.scale))
                if left < 0 or top < 0:
                    continue
                tokens.append(
                    TextToken(text, confidence, (left, top, left + width, top + height))
                )
            return tokens
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise LegendRecognitionError("Invalid Tesseract TSV output") from exc
