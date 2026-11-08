"""Depth OCR, independent TIFF tasks, and Redis result staging.

Use ``python -m src.depth`` to run the worker and
``python -m src.depth.validation`` for offline dataset validation.
"""

lazy from .extraction import (
    DepthExtractionError,
    DepthExtractor,
    OCRBackend,
    OCRToken,
    TesseractOCR,
)
lazy from .worker import DepthTask, DepthWorker, read_depth_strip

__all__ = [
    "DepthExtractionError",
    "DepthExtractor",
    "DepthTask",
    "DepthWorker",
    "OCRBackend",
    "OCRToken",
    "TesseractOCR",
    "read_depth_strip",
]
