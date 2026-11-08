"""Post-process the merger's flat CSV outputs, without changing its lifecycle."""

import argparse
import csv
from functools import lru_cache
import logging
import os
from pathlib import Path
import tempfile
import time

import centre_interval as centre
import interval_legend as base
import overlay

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def _cached_hash(path, size, mtime_ns, ctime_ns):
    return base.filehash(path)


def filehash(path):
    stat = path.stat()
    return _cached_hash(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def read_intervals(path):
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        for field in ("top_y", "bottom_y"):
            row[field] = int(row[field])
        # Keep the segmentation label separate from the new motif prediction.
        row["segmentation_lithology"] = row["lithology"]
        row["lithology"] = "unknown"
    base.validate_rows(rows)
    return rows


def resolve_inputs(log_id, data_dir, legend_dir):
    annotation = next((p for p in (
        data_dir / "bbox_annotation" / f"{log_id}.json",
        data_dir / f"{log_id}.json",
    ) if p.is_file()), None)
    if annotation is None:
        raise FileNotFoundError(f"No annotation for {log_id}")
    metadata = base.load(annotation)
    image_name = Path(metadata["image_name"].strip("'\" \t\r\n"))
    if image_name.name != str(image_name):
        raise ValueError("annotation image_name must be a filename")
    image = next((p for p in (data_dir / "images" / image_name,
                              data_dir / image_name) if p.is_file()), None)
    if image is None:
        raise FileNotFoundError(f"No source TIFF for {log_id}")
    legend = legend_dir / f"{log_id}.legend.json"
    if not legend.is_file():
        legend = legend_dir / "legend.json"
    return annotation, image, legend, metadata


def process_document(csv_path, data_dir, legend_dir):
    """Publish automatic motif predictions; optionally apply bound review."""
    log_id = csv_path.stem
    annotation, image, legend, metadata = resolve_inputs(log_id, data_dir, legend_dir)
    spec = base.load(legend)
    identity = filehash(image)
    if spec.get("source_sha256") != identity:
        raise ValueError(
            f"Legend/source mismatch for {log_id}: {legend} expects "
            f"{spec.get('source_sha256')}, but {image} has SHA-256 {identity}. "
            f"Provide {legend_dir / (log_id + '.legend.json')} with swatches "
            "verified against this TIFF; do not rebind an unrelated legend."
        )
    swatches = {entry["image"] for entry in spec["entries"]
                if entry.get("verified") is True and entry.get("kind") == "lithology"}
    inputs = {"csv": filehash(csv_path), "annotation": filehash(annotation),
              "image": identity, "legend": filehash(legend),
              "swatches": {name: filehash(legend.parent / name) for name in swatches}}
    evidence = csv_path.with_suffix(".centre")
    decisions = csv_path.with_suffix(".decisions.json")
    destinations = [csv_path.with_suffix(f".classified.{ext}") for ext in ("csv", "json", "png")]
    needs_prepare = not evidence.exists()
    if evidence.exists():
        previous = base.load(evidence / "inputs.json")
        sampler = previous.pop("sampler", None)
        if previous != inputs:
            raise ValueError(f"Inputs changed for {log_id}; move {evidence} aside and prepare again")
        if sampler != centre.SAMPLER_VERSION:
            receipt = evidence / "applied.json"
            reviewed = receipt.exists() and base.load(receipt).get("decisions") is not None
            untracked_outputs = any(path.exists() for path in destinations) and not receipt.exists()
            if decisions.exists() or reviewed or untracked_outputs:
                raise ValueError(f"Sampler changed for {log_id}; move old evidence, decisions "
                                 "and classified results aside, then review new horizontal strips")
            needs_prepare = True
    inputs["sampler"] = centre.SAMPLER_VERSION
    if needs_prepare:
        # Only publish complete evidence. A failed attempt leaves no populated
        # directory that would prevent the add-on from retrying.
        with tempfile.TemporaryDirectory(prefix=".centre-", dir=csv_path.parent) as tmp:
            staged = Path(tmp) / "evidence"
            raster = base.Raster(image)
            try:
                centre.prepare(read_intervals(csv_path),
                               metadata["bounding_boxes"]["interpreted_lithology_column"],
                               raster.read, centre.refs(spec, legend.parent), staged, identity)
            finally:
                raster.close()
            base.save(staged / "inputs.json", inputs)
            if evidence.exists():
                evidence.rename(Path(tmp) / "old-evidence")
            staged.rename(evidence)
        logger.info("Prepared motif slices for %s; review template available at %s", log_id,
                    evidence / "decision_template.json")

    decision_hash = filehash(decisions) if decisions.is_file() else None
    marker = evidence / "applied.json"
    if (marker.is_file() and base.load(marker).get("decisions") == decision_hash
            and base.load(marker).get("overlay_version") == overlay.VERSION
            and all(path.is_file() for path in destinations)
            and base.load(marker).get("outputs") == {p.name: filehash(p) for p in destinations}):
        return
    with tempfile.TemporaryDirectory(prefix=".centre-", dir=csv_path.parent) as tmp:
        reviewed = Path(tmp) / "reviewed"
        if decision_hash is not None:
            base.apply(evidence, decisions, reviewed)
        else:
            reviewed.mkdir()
            rows = base.load(evidence / "intervals.json")
            base.save(reviewed / "intervals.json", rows)
            base.write_csv(reviewed / "intervals.csv", rows)
            base.save(reviewed / "summary.json", {
                "intervals": len(rows), "threshold": base.ACCEPTANCE_THRESHOLD,
                "auto_accepted": sum(r["classification_status"] == "proposal_accepted" for r in rows),
                "slice_disagreements": sum(r.get("slice_disagreement", False) for r in rows),
                "accuracy": "not measured",
            })
        overlay.render_overlay(image, base.load(reviewed / "intervals.json"),
                               metadata["bounding_boxes"]["interpreted_lithology_column"],
                               reviewed / "intervals.png")
        for target in destinations:
            os.replace(reviewed / f"intervals{target.suffix}", target)
        # Publish the receipt last so interruption causes a safe re-application.
        receipt = Path(tmp) / "applied.json"
        base.save(receipt, {"decisions": decision_hash,
                           "overlay_version": overlay.VERSION,
                           "outputs": {p.name: filehash(p) for p in destinations},
                           "summary": base.load(reviewed / "summary.json")})
        os.replace(receipt, marker)
    logger.info("Published %s classification for %s", "reviewed" if decision_hash else "automatic", log_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "/data")))
    parser.add_argument("--output-dir", type=Path, default=Path(os.getenv("OUTPUT_DIR", "/output")))
    parser.add_argument("--legend-dir", type=Path, default=Path(os.getenv("LEGEND_DIR", "/legends")))
    parser.add_argument("--log-id", help="Process just this merger output (without .csv)")
    parser.add_argument("--once", action="store_true", help="Scan once; fail if any document fails")
    parser.add_argument("--poll-seconds", type=float, default=10)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.log_id and (Path(args.log_id).name != args.log_id or "\\" in args.log_id):
        parser.error("--log-id must be a filename component")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        paths = ([args.output_dir / f"{args.log_id}.csv"] if args.log_id else
                 sorted(p for p in args.output_dir.glob("*.csv") if not p.name.endswith(".classified.csv")))
        failed = False
        for path in paths:
            try:
                process_document(path, args.data_dir, args.legend_dir)
            except Exception:
                failed = True
                logger.exception("Centre classification failed for %s", path.name)
        if args.once:
            raise SystemExit(1 if failed else 0)
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
