"""Document-local lithology pattern analysis."""

from .classifier import analyze_lithology, classify_features
from .annotation import annotate_lithology
from .export import write_intervals
from .features import compute_feature_map, compute_features
from .legend import (
    build_legend,
    build_legend_from_boxes,
    find_swatch_boxes,
    match_labels,
    read_legend,
)
from .intervals import merge_intervals, restrict_intervals
from .text import LegendRecognitionError, TesseractTextRecognizer, TextRecognizer
from .task import (
    LithologyTask,
    LithologyTaskResult,
    process_lithology_task,
    read_task_images,
)
from .types import (
    FeatureWindow,
    LegendPattern,
    LithologyResult,
    LithologyInterval,
    PatternFeatures,
    TextToken,
)
from .pipeline import interpret_lithology

__all__ = [
    "FeatureWindow",
    "LegendPattern",
    "LithologyResult",
    "LithologyTask",
    "LithologyTaskResult",
    "LithologyInterval",
    "PatternFeatures",
    "TextRecognizer",
    "TextToken",
    "TesseractTextRecognizer",
    "LegendRecognitionError",
    "analyze_lithology",
    "annotate_lithology",
    "build_legend",
    "build_legend_from_boxes",
    "classify_features",
    "compute_feature_map",
    "compute_features",
    "find_swatch_boxes",
    "interpret_lithology",
    "merge_intervals",
    "match_labels",
    "read_legend",
    "read_task_images",
    "restrict_intervals",
    "process_lithology_task",
    "write_intervals",
]
