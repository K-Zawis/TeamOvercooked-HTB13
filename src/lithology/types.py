"""Data exchanged between lithology feature extraction and classification."""

from dataclasses import dataclass
import math

lazy import numpy as np


def _vector(values, name: str) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float64)
    if vector.ndim != 1 or not len(vector):
        raise ValueError(f"{name} must be a nonempty one-dimensional vector")
    if not np.all(np.isfinite(vector)):
        raise ValueError(f"{name} must contain only finite values")
    vector = np.ascontiguousarray(vector)
    vector.setflags(write=False)
    return vector


@dataclass(frozen=True)
class PatternFeatures:
    """Normalized descriptors for one lithology pattern crop."""

    lbp: np.ndarray
    color: np.ndarray
    edges: np.ndarray
    density: np.ndarray
    combined: np.ndarray

    def __post_init__(self) -> None:
        for name in ("lbp", "color", "edges", "density", "combined"):
            object.__setattr__(self, name, _vector(getattr(self, name), name))

    def to_dict(self) -> dict[str, list[float]]:
        return {
            name: getattr(self, name).tolist()
            for name in ("lbp", "color", "edges", "density", "combined")
        }


@dataclass(frozen=True)
class FeatureWindow:
    """Pattern features for one half-open window in full-image coordinates."""

    top_y: int
    bottom_y: int
    features: PatternFeatures

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (self.top_y, self.bottom_y)):
            raise ValueError("window coordinates must be integers")
        if self.top_y < 0 or self.bottom_y <= self.top_y:
            raise ValueError("window must be a positive half-open Y range")


@dataclass(frozen=True)
class TextToken:
    """Recognized text and its max-exclusive box in crop-local coordinates."""

    text: str
    confidence: float
    box: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("token text must be a nonempty string")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("token confidence must be finite and in [0, 1]")
        if not isinstance(self.box, tuple) or len(self.box) != 4:
            raise ValueError("token box must be an (x0, y0, x1, y1) tuple")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in self.box):
            raise ValueError("token coordinates must be integers")
        left, top, right, bottom = self.box
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("token box must be a positive max-exclusive range")
        object.__setattr__(self, "text", self.text.strip())


@dataclass(frozen=True)
class LegendPattern:
    """A named legend swatch represented in the shared feature space."""

    name: str
    features: PatternFeatures

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("legend name must be a nonempty string")
        object.__setattr__(self, "name", self.name.strip())


@dataclass(frozen=True)
class LithologyInterval:
    """A classified, half-open interval in full-image Y coordinates."""

    top_y: int
    bottom_y: int
    lithology: str | None
    confidence: float

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (self.top_y, self.bottom_y)):
            raise ValueError("interval coordinates must be integers")
        if self.top_y < 0 or self.bottom_y <= self.top_y:
            raise ValueError("interval must be a positive half-open Y range")
        if self.lithology is not None and (
            not isinstance(self.lithology, str) or not self.lithology.strip()
        ):
            raise ValueError("lithology must be a nonempty string or None")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be finite and in [0, 1]")

    def to_dict(self) -> dict[str, int | float | str | None]:
        return {
            "top_y": self.top_y,
            "bottom_y": self.bottom_y,
            "lithology": self.lithology,
            "confidence": self.confidence,
        }
