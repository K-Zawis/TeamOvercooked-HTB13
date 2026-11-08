"""Exercise the delivered centre add-on against the actual merger CSV contract."""

import csv
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import pytest


ADDON = Path(__file__).resolve().parents[1] / "centre-classifier"


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.syspath_prepend(str(ADDON))
    spec = importlib.util.spec_from_file_location("centre_pipeline", ADDON / "pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def document(tmp_path, pipeline):
    data = tmp_path / "data"
    output = tmp_path / "output"
    legends = tmp_path / "legends"
    for path in (data / "images", data / "bbox_annotation", output, legends):
        path.mkdir(parents=True)
    image = data / "images" / "scan.tif"
    Image.new("RGB", (80, 100), "white").save(image)
    annotation = data / "bbox_annotation" / "scan.json"
    annotation.write_text(json.dumps({"image_name": "scan.tif", "bounding_boxes": {
        "interpreted_lithology_column": [0, 0, 80, 100]}}))
    Image.new("RGB", (40, 40), "white").save(legends / "white.png")
    Image.new("RGB", (40, 40), "black").save(legends / "black.png")
    legend = {"source_sha256": pipeline.filehash(image), "entries": [
        {"id": key, "label": label, "image": f"{key}.png", "verified": True, "kind": "lithology"}
        for key, label in (("white", "Sand"), ("black", "Shale"))]}
    (legends / "scan.legend.json").write_text(json.dumps(legend))
    csv_path = output / "scan.csv"
    csv_path.write_text("top_depth,bottom_depth,lithology,confidence,top_y,bottom_y,cluster_id\n"
                        ",,Sand,0.900,0,50,c0\n100.000,110.000,Shale,0.800,50,100,c1\n")
    return csv_path, data, legends


def decisions(pipeline, csv_path, indices=(0, 1)):
    template = pipeline.base.load(csv_path.with_suffix(".centre") / "decision_template.json")
    template["decisions"] = [template["decisions"][i] for i in indices]
    for row in template["decisions"]:
        row["probabilities"] = {key: float(key == "white") for key in template["options"]}
    path = csv_path.with_suffix(".decisions.json")
    path.write_text(json.dumps(template))
    return path


def test_prepare_review_flat_outputs_and_retry(pipeline, document):
    csv_path, data, legends = document
    original = csv_path.read_bytes()
    pipeline.process_document(*document)
    evidence = csv_path.with_suffix(".centre")
    assert (evidence / "i00000.png").is_file()
    assert csv_path.with_suffix(".classified.csv").exists()
    with Image.open(csv_path.with_suffix(".classified.png")) as image:
        assert image.size == (80, 100)
        assert image.format == "PNG"
    # First row unreviewed, second reviewed: CSV must support differing keys.
    decisions(pipeline, csv_path, indices=(1,))
    pipeline.process_document(*document)
    result = pipeline.base.load(csv_path.with_suffix(".classified.json"))
    assert [(r["top_y"], r["bottom_y"]) for r in result] == [(0, 50), (50, 100)]
    assert result[0]["top_depth"] == ""
    assert result[1]["bottom_depth"] == "110.000"
    assert result[0]["lithology"] == "Sand"
    assert result[0]["classification_status"] == "proposal_accepted"
    assert result[0]["review_status"] == "not_reviewed"
    assert result[1]["lithology"] == "Sand"
    assert result[1]["segmentation_lithology"] == "Shale"
    assert result[1]["confidence"] == "0.800"
    with csv_path.with_suffix(".classified.csv").open() as file:
        rows = list(csv.DictReader(file))
    assert rows[1]["review_probability"] == "1.0"
    assert csv_path.read_bytes() == original
    with patch.object(pipeline.centre, "prepare", side_effect=AssertionError("reprepared")), \
         patch.object(pipeline.base, "apply", side_effect=AssertionError("reapplied")):
        pipeline.process_document(*document)
    # Missing output and modified decisions both trigger publication again.
    csv_path.with_suffix(".classified.csv").unlink()
    pipeline.process_document(*document)
    decisions(pipeline, csv_path)
    pipeline.process_document(*document)
    assert pipeline.base.load(csv_path.with_suffix(".classified.json"))[0]["lithology"] == "Sand"


@pytest.mark.parametrize("changed", ["csv", "swatch", "annotation"])
def test_changed_inputs_refused(pipeline, document, changed):
    csv_path, data, legends = document
    pipeline.process_document(*document)
    if changed == "csv":
        csv_path.write_text(csv_path.read_text().replace("0.900", "0.500"))
    elif changed == "annotation":
        annotation = data / "bbox_annotation" / "scan.json"
        annotation.write_text(annotation.read_text() + "\n")
    else:
        Image.new("RGB", (40, 40), "red").save(legends / "white.png")
    with pytest.raises(ValueError, match="Inputs changed"):
        pipeline.process_document(*document)


def test_legend_mismatch_and_failed_preparation_retry(pipeline, document):
    csv_path, data, legends = document
    image = data / "images" / "scan.tif"
    original = image.read_bytes()
    Image.new("RGB", (80, 100), "red").save(image)
    with pytest.raises(ValueError, match="Legend/source mismatch"):
        pipeline.process_document(*document)
    image.write_bytes(original)
    with patch.object(pipeline.centre, "prepare", side_effect=OSError("disk")):
        with pytest.raises(OSError):
            pipeline.process_document(*document)
    assert list(csv_path.parent.iterdir()) == [csv_path]
    pipeline.process_document(*document)
    assert csv_path.with_suffix(".centre").is_dir()


def test_invalid_decision_keeps_published_results(pipeline, document):
    csv_path, _, _ = document
    pipeline.process_document(*document)
    path = decisions(pipeline, csv_path)
    pipeline.process_document(*document)
    target = csv_path.with_suffix(".classified.csv")
    original = target.read_bytes()
    payload = json.loads(path.read_text())
    payload["decisions"][0]["evidence_sha256"] = "wrong evidence"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="binding mismatch"):
        pipeline.process_document(*document)
    assert target.read_bytes() == original


def test_document_legend_takes_precedence_over_unrelated_fallback(pipeline, document):
    csv_path, _, legends = document
    (legends / "legend.json").write_text(json.dumps({"source_sha256": "unrelated"}))
    pipeline.process_document(*document)
    assert csv_path.with_suffix(".centre").is_dir()


def test_mismatched_fallback_identifies_required_configuration(pipeline, document):
    _, _, legends = document
    (legends / "scan.legend.json").rename(legends / "legend.json")
    spec = pipeline.base.load(legends / "legend.json")
    spec["source_sha256"] = "unrelated"
    (legends / "legend.json").write_text(json.dumps(spec))
    with pytest.raises(ValueError, match=r"legend.json expects unrelated.*scan.legend.json"):
        pipeline.process_document(*document)


@pytest.mark.parametrize("width", [3, 4, 10, 47, 128, 297, 600])
@pytest.mark.parametrize("height", [1, 2, 8, 23, 48, 129, 385, 1000])
def test_motif_slices_cover_interval_without_crossing_boundaries(pipeline, width, height):
    image = Image.new("RGB", (width + 20, height + 30))
    for y in range(image.height):
        image.paste((y % 256, 0, 0), (0, y, image.width, y + 1))
    images, boxes = pipeline.centre.patches(image.crop, [10, 20, 10 + width, 20 + height],
                                           {"top_y": 20, "bottom_y": 20 + height})
    trim = max(1, round(width * .08))
    pad = min(4, height // 8)
    covered = set()
    for patch_image, (left, top, right, bottom) in zip(images, boxes):
        assert (left, right) == (10 + trim, 10 + width - trim)
        assert 20 <= top < bottom <= 20 + height
        assert bottom - top == min(32, height - 2 * pad)
        assert 20 + pad <= top < bottom <= 20 + height - pad
        covered.update(range(top, bottom))
        assert patch_image.size == (right - left, bottom - top)
        assert patch_image.getpixel((0, 0)) == (top % 256, 0, 0)
        assert patch_image.getpixel((patch_image.width - 1, 0)) == (top % 256, 0, 0)
        assert patch_image.getpixel((0, patch_image.height - 1)) == ((bottom - 1) % 256, 0, 0)
    assert covered == set(range(20 + pad, 20 + height - pad))


def test_legend_preserves_complete_native_motifs(pipeline, document):
    _, _, legends = document
    refs = pipeline.centre.refs(pipeline.base.load(legends / "scan.legend.json"), legends)
    assert all(ref["im"].size == (40, 40) for ref in refs)
    for ref in refs:
        with Image.open(legends / (ref['id'] + '.png')) as original:
            assert ref['im'].tobytes() == original.convert('RGB').tobytes()
    pipeline.process_document(*document)
    folder = document[0].with_suffix('.centre')
    assert Image.open(folder / 'legend_000.png').size == (40, 40)
    evidence = pipeline.base.load(folder / 'evidence.json')
    assert all(box[3] - box[1] == 40 for e in evidence for box in e['boxes'])


def test_unreviewed_vertical_evidence_is_regenerated(pipeline, document):
    csv_path, _, _ = document
    pipeline.process_document(*document)
    folder = csv_path.with_suffix(".centre")
    inputs = pipeline.base.load(folder / "inputs.json")
    inputs.pop("sampler")
    pipeline.base.save(folder / "inputs.json", inputs)
    (folder / "obsolete-vertical.png").write_text("old crop")
    pipeline.process_document(*document)
    assert not (folder / "obsolete-vertical.png").exists()
    assert pipeline.base.load(folder / "inputs.json")["sampler"] == pipeline.centre.SAMPLER_VERSION
    for evidence in pipeline.base.load(folder / "evidence.json"):
        assert all(b[3] - b[1] == 40 for b in evidence["boxes"])


def test_reviewed_vertical_evidence_requires_new_review(pipeline, document):
    csv_path, _, _ = document
    pipeline.process_document(*document)
    decisions(pipeline, csv_path)
    folder = csv_path.with_suffix(".centre")
    inputs = pipeline.base.load(folder / "inputs.json")
    inputs.pop("sampler")
    pipeline.base.save(folder / "inputs.json", inputs)
    with pytest.raises(ValueError, match="Sampler changed"):
        pipeline.process_document(*document)
    assert csv_path.with_suffix(".classified.csv").exists()


@pytest.mark.parametrize('distance,accepted', [(0.49, True), (0.5, True), (0.51, False)])
def test_simple_half_similarity_threshold(pipeline, monkeypatch, distance, accepted):
    refs = [{'id': 'a', 'label': 'Sand', 'feature': [distance]},
            {'id': 'b', 'label': 'Shale', 'feature': [.9]}]
    monkeypatch.setattr(pipeline.base, 'distance', lambda _, ref: ref[0])
    result = pipeline.base.classify([Image.new('RGB', (40, 40))], refs)
    assert result['score'] == pytest.approx(1 - distance)
    assert (result['state'] == 'proposal_accepted') is accepted
    assert result['proposal_label'] == 'Sand'
    assert result['slices'][0]['lithology'] == ('Sand' if accepted else 'unknown')


def test_automatic_results_need_no_decisions_and_are_idempotent(pipeline, document):
    csv_path, _, _ = document
    pipeline.process_document(*document)
    result = pipeline.base.load(csv_path.with_suffix('.classified.json'))
    assert all(r['lithology'] == 'Sand' and r['proposal_score'] == 1 for r in result)
    target = csv_path.with_suffix('.classified.csv')
    before = target.stat().st_mtime_ns
    with patch.object(pipeline.centre, 'prepare', side_effect=AssertionError('reprepared')):
        pipeline.process_document(*document)
    assert target.stat().st_mtime_ns == before


def test_internal_material_boundary_produces_two_classified_segments(pipeline, document):
    csv_path, data, legends = document
    image = data / 'images' / 'scan.tif'
    pixels = Image.new('RGB', (80, 100), 'white')
    pixels.paste('black', (0, 50, 80, 100))
    pixels.save(image)
    spec = pipeline.base.load(legends / 'scan.legend.json')
    spec['source_sha256'] = pipeline.filehash(image)
    pipeline.base.save(legends / 'scan.legend.json', spec)
    csv_path.write_text('top_depth,bottom_depth,lithology,confidence,top_y,bottom_y,cluster_id\n'
                        '100.000,200.000,Original,0.900,0,100,c0\n')
    pipeline.process_document(*document)
    rows = pipeline.base.load(csv_path.with_suffix('.classified.json'))
    assert [(r['top_y'], r['bottom_y'], r['lithology']) for r in rows] == [(0, 50, 'Sand'), (50, 100, 'Shale')]
    assert [(r['top_depth'], r['bottom_depth']) for r in rows] == [('100.000', '150.000'), ('150.000', '200.000')]
    assert all(r['parent_top_y'] == 0 and r['parent_bottom_y'] == 100 for r in rows)
    assert all(r['depth_method'] == 'linear_within_parent' for r in rows)
    assert all(r['classification_status'] == 'proposal_accepted' for r in rows)


def test_optional_review_accepts_half_without_old_margin_gate(pipeline, document):
    pipeline.process_document(*document)
    path = decisions(pipeline, document[0])
    payload = pipeline.base.load(path)
    for item in payload['decisions']:
        item['probabilities']['white'] = .51
        item['probabilities']['black'] = .49
    pipeline.base.save(path, payload)
    pipeline.process_document(*document)
    rows = pipeline.base.load(document[0].with_suffix('.classified.json'))
    assert all(r['classification_status'] == 'review_accepted' for r in rows)


def test_changed_auto_prediction_is_rejected_before_review(pipeline, document):
    pipeline.process_document(*document)
    csv_path = document[0]
    decisions(pipeline, csv_path)
    path = csv_path.with_suffix('.centre') / 'intervals.json'
    rows = pipeline.base.load(path)
    rows[0]['lithology'] = 'tampered'
    pipeline.base.save(path, rows)
    with pytest.raises(ValueError, match='changed prediction'):
        pipeline.process_document(*document)


def test_overlay_preserves_source_and_half_open_mask_bounds(pipeline, tmp_path, monkeypatch):
    source = tmp_path / 'source.tif'
    target = tmp_path / 'overlay.png'
    background = (120, 150, 180)
    Image.new('RGB', (240, 96), background).save(source)
    rows = [dict(top_y=10, bottom_y=35, lithology='Sand'),
            dict(top_y=40, bottom_y=90, lithology='unknown')]
    # Exercise mask and text rendering through many PNG band boundaries.
    monkeypatch.setattr(pipeline.overlay, 'BAND_HEIGHT', 7)
    pipeline.overlay.render_overlay(source, rows, [40, 10, 90, 90], target)
    with Image.open(target) as result:
        result.load()  # Validates the streamed PNG can be fully decoded.
        assert result.size == (240, 96)
        assert result.mode == 'RGB'
        for xy in [(0, 0), (45, 9), (45, 35), (45, 39), (45, 90), (39, 12), (90, 12)]:
            assert result.getpixel(xy) == background
        assert result.getpixel((45, 12)) != background
        assert result.getpixel((45, 45)) != background
        # Rendering in bands must be pixel-identical to rendering one band.
        monkeypatch.setattr(pipeline.overlay, 'BAND_HEIGHT', 200)
        second = tmp_path / 'single-band.png'
        pipeline.overlay.render_overlay(source, rows, [40, 10, 90, 90], second)
        with Image.open(second) as reference:
            assert result.tobytes() == reference.tobytes()
    with Image.open(source) as original:
        assert original.getpixel((45, 12)) == background


def test_overlay_is_repaired_and_tracks_reviewed_labels(pipeline, document):
    csv_path = document[0]
    pipeline.process_document(*document)
    target = csv_path.with_suffix('.classified.png')
    original = target.read_bytes()
    target.unlink()
    with patch.object(pipeline.centre, 'prepare', side_effect=AssertionError('reprepared')):
        pipeline.process_document(*document)
    assert target.read_bytes() == original
    target.write_bytes(b'corrupted PNG')
    pipeline.process_document(*document)
    assert target.read_bytes() == original
    path = decisions(pipeline, csv_path)
    payload = pipeline.base.load(path)
    for row in payload['decisions']:
        row['probabilities']['white'] = 0
        row['probabilities']['black'] = 1
    pipeline.base.save(path, payload)
    pipeline.process_document(*document)
    assert target.read_bytes() != original
    assert all(row['lithology'] == 'Shale' for row in
               pipeline.base.load(csv_path.with_suffix('.classified.json')))


def test_overlay_failure_preserves_previous_deliverables(pipeline, document):
    csv_path = document[0]
    pipeline.process_document(*document)
    targets = [csv_path.with_suffix('.classified.' + ext) for ext in ('csv', 'json', 'png')]
    original = {p: p.read_bytes() for p in targets}
    decisions(pipeline, csv_path)
    with patch.object(pipeline.overlay, 'render_overlay', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            pipeline.process_document(*document)
    assert all(p.read_bytes() == contents for p, contents in original.items())


@pytest.mark.parametrize('supported_depths', [True, False])
def test_all_thin_interbeds_are_segmented_classified_and_covered(pipeline, document, supported_depths):
    csv_path, data, legends = document
    height = 120
    bands = [(18, 19), (33, 35), (61, 65), (80, 88)]
    image = Image.new('RGB', (80, height), (128, 255, 128))
    for top, bottom in bands:
        image.paste((255, 255, 0), (0, top, 80, bottom))
    source = data / 'images' / 'scan.tif'
    image.save(source)
    for name, color in [('white', (255, 255, 0)), ('black', (128, 255, 128))]:
        Image.new('RGB', (40, 40), color).save(legends / (name + '.png'))
    spec = pipeline.base.load(legends / 'scan.legend.json')
    spec['source_sha256'] = pipeline.filehash(source)
    pipeline.base.save(legends / 'scan.legend.json', spec)
    annotation_path = data / 'bbox_annotation' / 'scan.json'
    annotation = pipeline.base.load(annotation_path)
    annotation['bounding_boxes']['interpreted_lithology_column'] = [0, 0, 80, height]
    pipeline.base.save(annotation_path, annotation)
    csv_path.write_text('top_depth,bottom_depth,lithology,confidence,top_y,bottom_y,cluster_id\n'
                        + ('0.000' if supported_depths else '') + ',120.000,Coarse,0.900,0,120,c0\n')
    pipeline.process_document(*document)
    rows = pipeline.base.load(csv_path.with_suffix('.classified.json'))
    assert [(r['top_y'], r['bottom_y']) for r in rows if r['lithology'] == 'Sand'] == bands
    assert len(rows) == 9
    coverage = [None] * height
    for row in rows:
        for y in range(row['top_y'], row['bottom_y']):
            assert coverage[y] is None
            coverage[y] = row['lithology']
        if supported_depths:
            assert float(row['top_depth']) == row['top_y']
            assert float(row['bottom_depth']) == row['bottom_y']
        else:
            assert row['top_depth'] == ''
            assert row['bottom_depth'] == ('120.000' if row['bottom_y'] == height else '')
    assert all(coverage)
    assert all(coverage[y] == ('Sand' if any(a <= y < b for a, b in bands) else 'Shale')
               for y in range(height))
    # All three deliverables describe the refined segments, not the parent.
    with csv_path.with_suffix('.classified.csv').open() as file:
        assert len(list(csv.DictReader(file))) == 9
    assert csv_path.with_suffix('.classified.png').is_file()


def test_monochrome_motif_change_is_segmented_without_color_change(pipeline):
    from segmentation import refine_intervals
    horizontal = Image.new('RGB', (80, 90), 'white')
    for y in range(0, 90, 8):
        horizontal.paste('black', (0, y, 80, y + 1))
    vertical = Image.new('RGB', (80, 90), 'white')
    for x in range(0, 80, 8):
        vertical.paste('black', (x, 0, x + 1, 90))
    source = Image.new('RGB', (80, 180))
    source.paste(horizontal, (0, 0))
    source.paste(vertical, (0, 90))
    refs = []
    for key, image in [('h', horizontal.crop((0, 0, 40, 40))),
                       ('v', vertical.crop((0, 0, 40, 40)))]:
        refs.append(dict(id=key, label=key, im=image, feature=pipeline.base.descriptor(image)))
    result = refine_intervals([dict(top_y=0, bottom_y=180)], [0, 0, 80, 180], source.crop, refs)
    assert any(abs(row['bottom_y'] - 90) <= 20 for row in result[:-1])
    assert result[0]['top_y'] == 0 and result[-1]['bottom_y'] == 180


def test_segmentation_covers_gaps_and_ends_of_annotated_track(pipeline, document):
    from segmentation import refine_intervals
    refs = pipeline.centre.refs(pipeline.base.load(document[2] / 'scan.legend.json'), document[2])
    image = Image.new('RGB', (80, 120), 'white')
    parents = [dict(top_y=10, bottom_y=20, top_depth='10.000', bottom_depth='20.000'),
               dict(top_y=80, bottom_y=100, top_depth='80.000', bottom_depth='100.000')]
    rows = refine_intervals(parents, [0, 0, 80, 120], image.crop, refs)
    assert [(r['top_y'], r['bottom_y']) for r in rows] == [(0, 10), (10, 20), (20, 80), (80, 100), (100, 120)]
    assert sum(r['bottom_y'] - r['top_y'] for r in rows) == 120
    assert sum(r['parent_interval_id'].startswith('gap') for r in rows) == 3
    assert rows[0]['top_depth'] == '' and rows[-1]['bottom_depth'] == ''


def test_band_boundary_does_not_erase_a_thin_bed(pipeline, document):
    from segmentation import refine_intervals
    refs = pipeline.centre.refs(pipeline.base.load(document[2] / 'scan.legend.json'), document[2])
    image = Image.new('RGB', (80, 2100), 'white')
    image.paste('black', (0, 2047, 80, 2055))
    reads = []
    def read(box):
        reads.append(box)
        return image.crop(box)
    rows = refine_intervals([dict(top_y=0, bottom_y=2100)], [0, 0, 80, 2100], read, refs)
    assert [(r['top_y'], r['bottom_y']) for r in rows] == [(0, 2047), (2047, 2055), (2055, 2100)]
    assert max(b[3] - b[1] for b in reads) <= 2048
