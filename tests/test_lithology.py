import csv

import numpy as np
import pytest
from PIL import Image

from src.lithology import (
    LegendPattern,
    LithologyInterval,
    LithologyTask,
    analyze_lithology,
    annotate_lithology,
    build_legend,
    build_legend_from_boxes,
    classify_features,
    compute_feature_map,
    compute_features,
    find_swatch_boxes,
    interpret_lithology,
    match_labels,
    merge_intervals,
    read_legend,
    process_lithology_task,
    restrict_intervals,
    TextToken,
    write_intervals,
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
        "cluster_id": "cluster_0",
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
    assert intervals[0].cluster_id is None


def test_write_intervals_exports_structured_ml_fields(tmp_path):
    path = tmp_path / "intervals.csv"
    intervals = [
        LithologyInterval(100, 200, "Shale", 0.9, "cluster_0"),
        LithologyInterval(200, 300, None, 0.4, None),
    ]
    write_intervals(path, intervals)
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))
    assert rows == [
        {
            "top_y": "100", "bottom_y": "200", "cluster_id": "cluster_0",
            "lithology": "Shale", "confidence": "0.9",
        },
        {
            "top_y": "200", "bottom_y": "300", "cluster_id": "",
            "lithology": "", "confidence": "0.4",
        },
    ]


def test_annotate_lithology_draws_global_intervals_on_crop():
    image = Image.new("RGB", (100, 100), "white")
    intervals = [
        LithologyInterval(1010, 1050, "Shale", 0.9, "cluster_0"),
        LithologyInterval(1050, 1090, "Sandstone", 0.8, "cluster_1"),
    ]
    result = annotate_lithology(
        image,
        intervals,
        (10, 1000, 50, 1100),
        origin=(0, 1000),
        opacity=255,
    )
    pixels = np.asarray(result)
    assert tuple(pixels[20, 20]) == (40, 160, 60)
    assert tuple(pixels[70, 20]) == (160, 100, 40)
    assert tuple(pixels[5, 20]) == (255, 255, 255)


def test_interpret_lithology_composes_legend_and_track_analysis():
    legend_image = np.full((70, 130, 3), 255, np.uint8)
    boxes = [(10, 5, 50, 35), (75, 5, 115, 35)]
    for box, name in zip(boxes, ("horizontal", "vertical")):
        left, top, right, bottom = box
        legend_image[top:bottom, left] = 0
        legend_image[top:bottom, right - 1] = 0
        legend_image[top, left:right] = 0
        legend_image[bottom - 1, left:right] = 0
        legend_image[top + 2:bottom - 2, left + 2:right - 2] = pattern(
            name, bottom - top - 4, right - left - 4
        )

    class Recognizer:
        def recognize(self, pixels):
            return [
                TextToken("Shale", 0.9, (15, 40, 46, 49)),
                TextToken("Sandstone", 0.9, (69, 40, 120, 49)),
            ]

    track = np.concatenate([
        pattern("horizontal", height=128),
        pattern("vertical", height=128),
    ])
    result = interpret_lithology(
        track,
        legend_image,
        global_y=1000,
        recognizer=Recognizer(),
        window_height=48,
        stride=8,
        transition_penalty=0.04,
    )
    assert [entry.name for entry in result.legend] == ["Shale", "Sandstone"]
    assert [interval.lithology for interval in result.intervals] == [
        "Shale", "Sandstone"
    ]
    assert [interval.cluster_id for interval in result.intervals] == [
        "cluster_0", "cluster_1"
    ]
    assert result.intervals[0].top_y == 1000
    assert result.intervals[-1].bottom_y == 1256


def test_merge_intervals_only_combines_matching_classes():
    intervals = [
        LithologyInterval(100, 150, "Shale", 0.9, "cluster_0"),
        LithologyInterval(150, 220, "Shale", 0.8, "cluster_0"),
        LithologyInterval(220, 260, "Sandstone", 0.7, "cluster_1"),
    ]
    merged = merge_intervals(intervals)
    assert [interval.to_dict() for interval in merged] == [
        {
            "top_y": 100, "bottom_y": 220, "lithology": "Shale",
            "cluster_id": "cluster_0", "confidence": 0.8,
        },
        intervals[2].to_dict(),
    ]
    with pytest.raises(ValueError, match="conflicting"):
        merge_intervals([
            intervals[0],
            LithologyInterval(140, 200, "Sandstone", 0.9, "cluster_1"),
        ])


def test_restrict_intervals_clips_to_owned_range():
    result = restrict_intervals([
        LithologyInterval(80, 120, "Shale", 0.9, "cluster_0"),
        LithologyInterval(120, 180, "Sandstone", 0.8, "cluster_1"),
    ], 100, 160)
    assert [(item.top_y, item.bottom_y) for item in result] == [
        (100, 120), (120, 160)
    ]


@pytest.mark.parametrize("change", [
    {"log_id": ""},
    {"image_path": ""},
    {"column_bbox": [70, 80, 70, 240]},
    {"legend_bbox": None},
    {"y_start": 79},
    {"y_end": 241},
    {"y_end": 80},
])
def test_lithology_task_validation(tmp_path, change):
    payload = {
        "log_id": "0000000009",
        "image_path": str(tmp_path / "image.tif"),
        "column_bbox": [70, 80, 110, 240],
        "legend_bbox": [5, 5, 65, 70],
        "y_start": 80,
        "y_end": 240,
    }
    payload.update(change)
    with pytest.raises(ValueError):
        LithologyTask.from_payload(payload)


def test_process_lithology_task_returns_listener_handoff(tmp_path, monkeypatch):
    image = np.full((300, 130, 3), 255, np.uint8)
    path = tmp_path / "image.tif"
    Image.fromarray(image).save(path)
    payload = {
        "log_id": "0000000009",
        "image_path": str(path),
        "column_bbox": [70, 80, 110, 240],
        "legend_bbox": [5, 5, 65, 70],
        "y_start": 80,
        "y_end": 240,
    }
    legend = build_legend({"Shale": pattern("horizontal")})
    cache = {("0000000009", str(path), (5, 5, 65, 70)): legend}
    monkeypatch.setattr("src.lithology.task.analyze_lithology", lambda *args, **kwargs: [
        LithologyInterval(80, 160, "Shale", 0.9, "cluster_0"),
        LithologyInterval(160, 240, "Shale", 0.8, "cluster_0"),
    ])
    result = process_lithology_task(payload, legend_cache=cache)
    assert result.to_dict() == {
        "log_id": "0000000009",
        "y_start": 80,
        "y_end": 240,
        "legend": {"cluster_0": "Shale"},
        "intervals": [{
            "top_y": 80,
            "bottom_y": 240,
            "lithology": "Shale",
            "cluster_id": "cluster_0",
            "confidence": 0.8,
        }],
    }
