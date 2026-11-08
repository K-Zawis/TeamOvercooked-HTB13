"""Replaceable OCR engine used to extract labels adjacent to legend swatches."""

from __future__ import annotations

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
    """Raised when legend text recognition fails."""


class TextRecognizer(Protocol):
    def recognize(self, image: np.ndarray) -> Iterable[TextToken]: ...


class TesseractTextRecognizer:
    """Recognize legend text via Tesseract OCR using TSV output."""

    __slots__ = ("executable", "timeout", "scale")

    def __init__(
        self,
        executable: str = "tesseract",
        *,
        timeout: float = 30.0,
        scale: int = 2,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if isinstance(scale, bool) or scale not in (1, 2, 3):
            raise ValueError("scale must be 1, 2, or 3")
        self.executable = executable
        self.timeout = float(timeout)
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

        gray_image = Image.fromarray(pixels).convert("L")
        enhanced = ImageOps.autocontrast(gray_image)

        if self.scale > 1:
            enhanced = enhanced.resize(
                (enhanced.width * self.scale, enhanced.height * self.scale),
                Image.Resampling.LANCZOS,
            )

        pad_border = 12
        padded_image = ImageOps.expand(enhanced, border=pad_border, fill="white")

        buffer = io.BytesIO()
        padded_image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

        cmd = [
            self.executable,
            "stdin",
            "stdout",
            "-l",
            "eng",
            "--psm",
            "11",
            "tsv",
        ]
        sub_env = dict(os.environ, OMP_THREAD_LIMIT="1")

        try:
            result = subprocess.run(
                cmd,
                input=image_bytes,
                capture_output=True,
                timeout=self.timeout,
                env=sub_env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LegendRecognitionError("Tesseract is unavailable") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LegendRecognitionError(f"Tesseract execution failed: {exc}") from exc

        if result.returncode != 0:
            err_msg = result.stderr.decode(errors="replace")[:500]
            raise LegendRecognitionError(f"Tesseract failed with code {result.returncode}: {err_msg}")

        try:
            tsv_reader = csv.DictReader(
                io.StringIO(result.stdout.decode("utf-8", errors="replace")),
                delimiter="\t",
                quoting=csv.QUOTE_NONE,
            )
            required_cols = {"text", "conf", "left", "top", "width", "height"}
            if not tsv_reader.fieldnames or not required_cols.issubset(tsv_reader.fieldnames):
                raise ValueError("missing TSV columns from Tesseract output")

            tokens: list[TextToken] = []
            for row in tsv_reader:
                raw_text = row["text"].strip()
                if not raw_text:
                    continue

                try:
                    conf_val = float(row["conf"])
                except (ValueError, TypeError):
                    continue

                if conf_val < 0 or conf_val > 100:
                    continue

                confidence = conf_val / 100.0

                raw_left = int(row["left"]) - pad_border
                raw_top = int(row["top"]) - pad_border
                left = max(0, round(raw_left / self.scale))
                top = max(0, round(raw_top / self.scale))
                width = max(1, round(int(row["width"]) / self.scale))
                height = max(1, round(int(row["height"]) / self.scale))

                tokens.append(
                    TextToken(
                        text=raw_text,
                        confidence=confidence,
                        box=(left, top, left + width, top + height),
                    )
                )

            return tokens

        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise LegendRecognitionError("Invalid Tesseract TSV output format") from exc