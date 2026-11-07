"""Document-local lithology pattern analysis."""

from .classifier import analyze_lithology, classify_features
from .features import compute_feature_map, compute_features
from .legend import build_legend
from .types import FeatureWindow, LegendPattern, LithologyInterval, PatternFeatures

__all__ = [
    "FeatureWindow",
    "LegendPattern",
    "LithologyInterval",
    "PatternFeatures",
    "analyze_lithology",
    "build_legend",
    "classify_features",
    "compute_feature_map",
    "compute_features",
]
