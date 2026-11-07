"""Document-local lithology pattern analysis."""

from .classifier import analyze_lithology, classify_features
from .features import compute_feature_map, compute_features
from .legend import (
    build_legend,
    build_legend_from_boxes,
    find_swatch_boxes,
    match_labels,
    read_legend,
)
from .text import LegendRecognitionError, TesseractTextRecognizer, TextRecognizer
from .types import (
    FeatureWindow,
    LegendPattern,
    LithologyInterval,
    PatternFeatures,
    TextToken,
)

__all__ = [
    "FeatureWindow",
    "LegendPattern",
    "LithologyInterval",
    "PatternFeatures",
    "TextRecognizer",
    "TextToken",
    "TesseractTextRecognizer",
    "LegendRecognitionError",
    "analyze_lithology",
    "build_legend",
    "build_legend_from_boxes",
    "classify_features",
    "compute_feature_map",
    "compute_features",
    "find_swatch_boxes",
    "match_labels",
    "read_legend",
]
