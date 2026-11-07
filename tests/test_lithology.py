import numpy as np
import pytest

from src.lithology import (
    LegendPattern,
    analyze_lithology,
    build_legend,
    build_legend_from_boxes,
    classify_features,
    compute_feature_map,
    compute_features,
    find_swatch_boxes,
    match_labels,
    read_legend,
    TextToken,
)


def pattern(kind, height=96, width=96, foreground=(0, 0, 0), background=(255, 255, 255)):
    image = np.full((height, width, 3), background, np.uint8)
    if kind == "horizontal":
        image[::8, :] = foreground
        image[1::8, :] = foreground
    elif kind == "vertical":
        image[:, ::8] = foreground
        image[:, 1::8] = foreground
    elif kind == "checker":
        yy, xx = np.indices((height, width))
        image[((yy // 6) + (xx // 6)) % 2 == 0] = foreground
    elif kind == "solid":
        image[:] = foreground
    else:
        raise ValueError(kind)
    return image


def legend(*names):
    return build_legend({name: pattern(name) for name in names})


def test_build_legend_preserves_names_and_computes_shared_features():
    patterns = build_legend({
        "Shale": pattern("horizontal"),
        "Sandstone": pattern("vertical"),
    })
    assert [item.name for item in patterns] == ["Shale", "Sandstone"]
    assert all(np.linalg.norm(item.features.combined) == pytest.approx(1)
               for item in patterns)
    with pytest.raises(ValueError):
        build_legend({})


def test_find_and_extract_framed_legend_swatches():
    image = np.full((100, 150, 3), 255, np.uint8)
    boxes = [(10, 10, 50, 40), (80, 10, 120, 40), (10, 60, 50, 90)]
    patterns = ["horizontal", "vertical", "checker"]
    for box, name in zip(boxes, patterns):
        left, top, right, bottom = box
        image[top:bottom, left] = 0
        image[top:bottom, right - 1] = 0
        image[top, left:right] = 0
        image[bottom - 1, left:right] = 0
        image[top + 2:bottom - 2, left + 2:right - 2] = pattern(
            name, bottom - top - 4, right - left - 4
        )
    assert find_swatch_boxes(image) == tuple(boxes)

    named = build_legend_from_boxes(
        image,
        {name: box for name, box in zip(patterns, boxes)},
    )
    assert [item.name for item in named] == patterns
    assert all(np.linalg.norm(item.features.combined) == pytest.approx(1)
               for item in named)


def test_match_labels_combines_words_and_associates_nearest_swatch():
    boxes = [(10, 10, 50, 40), (80, 10, 120, 40)]
    tokens = [
        TextToken("Silty", 0.9, (7, 44, 27, 53)),
        TextToken("sandstone", 0.8, (29, 44, 66, 53)),
        TextToken("Shale", 0.95, (84, 44, 116, 53)),
        TextToken("HEADING", 0.99, (20, 0, 80, 8)),
    ]
    assert match_labels(boxes, tokens) == {
        "Silty sandstone": boxes[0],
        "Shale": boxes[1],
    }


def test_read_legend_composes_detection_ocr_and_features():
    image = np.full((70, 130, 3), 255, np.uint8)
    boxes = [(10, 5, 50, 35), (75, 5, 115, 35)]
    for box, name in zip(boxes, ("horizontal", "vertical")):
        left, top, right, bottom = box
        image[top:bottom, left] = 0
        image[top:bottom, right - 1] = 0
        image[top, left:right] = 0
        image[bottom - 1, left:right] = 0
        image[top + 2:bottom - 2, left + 2:right - 2] = pattern(
            name, bottom - top - 4, right - left - 4
        )

    class Recognizer:
        def recognize(self, pixels):
            assert pixels is image
            return [
                TextToken("Shale", 0.9, (15, 40, 46, 49)),
                TextToken("Sandstone", 0.9, (69, 40, 120, 49)),
            ]

    result = read_legend(image, Recognizer())
    assert [item.name for item in result] == ["Shale", "Sandstone"]


def test_features_are_normalized_and_serializable():
    features = compute_features(pattern("horizontal"))
    assert np.linalg.norm(features.combined) == pytest.approx(1)
    assert len(features.lbp) == 10
    assert set(features.to_dict()) == {"lbp", "color", "edges", "density", "combined"}
    with pytest.raises(ValueError):
        features.combined[0] = 5


def test_color_remains_available_when_texture_is_identical():
    red = compute_features(pattern("horizontal", foreground=(180, 20, 20)))
    blue = compute_features(pattern("horizontal", foreground=(20, 20, 180)))
    assert np.allclose(red.lbp, blue.lbp)
    assert not np.allclose(red.color, blue.color)
    assert not np.allclose(red.combined, blue.combined)


def test_feature_map_uses_global_coordinates_and_overlapping_windows():
    windows = compute_feature_map(
        pattern("horizontal", height=100),
        global_y=4404,
        window_height=40,
        stride=20,
    )
    assert [(window.top_y, window.bottom_y) for window in windows] == [
        (4404, 4444),
        (4424, 4464),
        (4444, 4484),
        (4464, 4504),
    ]
    intervals = classify_features(windows, legend("horizontal", "vertical"))
    assert len(intervals) == 1
    assert intervals[0].to_dict() == {
        "top_y": 4404,
        "bottom_y": 4504,
        "lithology": "horizontal",
        "confidence": pytest.approx(intervals[0].confidence),
    }


@pytest.mark.parametrize("bad", [
    np.zeros((20, 20), np.uint8),
    np.zeros((20, 20, 4), np.uint8),
    np.zeros((20, 20, 3), np.float32),
    np.zeros((2, 20, 3), np.uint8),
])
def test_invalid_feature_inputs_rejected(bad):
    with pytest.raises(ValueError):
        compute_features(bad)


def test_analyze_lithology_collapses_window_labels_into_global_intervals():
    track = np.concatenate(
        [
            pattern("horizontal", height=128),
            pattern("vertical", height=128),
            pattern("checker", height=128),
        ]
    )
    intervals = analyze_lithology(
        track,
        legend("horizontal", "vertical", "checker"),
        global_y=4404,
        window_height=48,
        stride=8,
        transition_penalty=0.04,
    )
    assert [interval.lithology for interval in intervals] == [
        "horizontal", "vertical", "checker"
    ]
    assert intervals[0].top_y == 4404
    assert intervals[-1].bottom_y == 4404 + len(track)
    assert abs(intervals[0].bottom_y - (4404 + 128)) <= 24
    assert abs(intervals[1].bottom_y - (4404 + 256)) <= 24
    assert all(left.bottom_y == right.top_y for left, right in zip(intervals, intervals[1:]))
    assert all(0 <= interval.confidence <= 1 for interval in intervals)


def test_unknown_pattern_is_not_forced_into_a_named_class():
    intervals = analyze_lithology(
        pattern("solid", height=128),
        legend("horizontal", "vertical"),
        window_height=48,
        stride=8,
        transition_penalty=0,
        unknown_distance=0.2,
    )
    assert len(intervals) == 1
    assert intervals[0].lithology is None
