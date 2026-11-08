"""Composition of legend recognition and lithology-track interpretation."""

from .classifier import analyze_lithology
from .legend import read_legend
from .text import TextRecognizer
from .types import LithologyResult


def interpret_lithology(
    image,
    legend_image,
    *,
    global_y: int = 0,
    recognizer: TextRecognizer | None = None,
    window_height: int = 64,
    stride: int = 16,
    transition_penalty: float = 0.08,
    unknown_distance: float = 0.38,
) -> LithologyResult:
    """Recognize a legend and interpret its corresponding lithology crop."""
    legend = read_legend(legend_image, recognizer)
    intervals = analyze_lithology(
        image,
        legend,
        global_y=global_y,
        window_height=window_height,
        stride=stride,
        transition_penalty=transition_penalty,
        unknown_distance=unknown_distance,
    )
    return LithologyResult(tuple(legend), tuple(intervals))
