"""Construct named lithology prototypes from extracted legend swatches."""

from collections.abc import Mapping

from .features import compute_features
from .types import LegendPattern


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
