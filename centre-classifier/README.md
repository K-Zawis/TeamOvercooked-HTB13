# Centre classifier

This container reads `output/<log_id>.csv` after the merger publishes it. It keeps
comparison images and evidence in `output/<log_id>.centre/`, and automatically
publishes `output/<log_id>.classified.csv`, `.json`, and `.png`. Original merger outputs
stay intact. No decision file or image-model service is required.

The PNG is a full-resolution copy of the original scan with translucent masks
over the annotated lithology track, interval boundaries, and rock-name labels.
It uses the final `lithology` values in the classified JSON, including optional
review updates. Each rock label has a stable color; unknown/special results use
gray. TIFF reads and PNG encoding run in horizontal bands to bound memory usage.
Missing or modified overlays are regenerated automatically on the next scan.

Before classification, coarse merger intervals are refined across the entire
annotated lithology track. Background-color changes are located at individual
source rows, preserving thin colored interbeds without a minimum bed thickness.
One/two-row dark strokes surrounded by the same background are treated as motif
ink. Local motif-height windows locate texture changes within color runs;
isolated texture votes are suppressed to reduce pattern-phase artifacts. Gaps
between coarse intervals are processed too. These are image-based engineering
rules, not a guarantee that every geological contact has been identified.

Legend references retain their full configured motif interiors, excluding frames
and labels. Segment slices cover the usable track width (8% trimmed on each
side), at the median height of the full legend motifs. For the included scan,
that means 39-pixel-wide slices, 33 rows high. Thin intervals use their available
height. Up to four boundary rows are excluded, slices cover the remaining
interval, and the final slice overlaps its predecessor if needed to retain full
motif height. No slice extends outside its refined segment. Neither motifs nor
samples are resized for classification.

Each slice is classified independently. The similarity score is
`max(0, 1 - descriptor_distance)`; proposals scoring **at least 0.5** are accepted.
This is an engineering similarity score, not a calibrated probability of rock
type. The motif with the lowest mean distance across slices is the interval
proposal. When its similarity meets 0.5, its legend label becomes the interval's
`lithology`. A low score leaves it `unknown` with status `below_threshold`.
Disagreeing slice winners set `slice_disagreement=true` without suppressing an
accepted rock label; slice-specific labels, scores, and Y bounds remain in
`evidence.json`. Each refined segment gets its own CSV/JSON row and overlay mask;
the original merger CSV remains unchanged. `parent_interval_id`, `parent_top_y`,
and `parent_bottom_y` retain the coarse interval lineage. Parent depth endpoints
are retained in `parent_top_depth` and `parent_bottom_depth`. Internal segment
depths use linear interpolation only when both parent endpoints are supported,
marked `depth_method=linear_within_parent`; otherwise unsupported internal depths
stay blank. This does not recover the merger's full depth calibration curve.

`legend_proposal` is the motif ID, `proposal_lithology` its rock label, and
`proposal_score` the automatic similarity. Original segmentation confidence and
label remain in `confidence` and `segmentation_lithology`. Each full motif is
saved as `legend_*.png`, each native sample as `i*_s*.png`, and the corresponding
`i00003.png`-style comparison sheet shows all slices with their scores and Y
bounds. Comparison sheets wrap samples into rows of four.

The worker automatically regenerates obsolete evidence and unreviewed automatic
outputs when the sampler changes. Existing decisions or reviewed outputs must
be moved aside first; decisions for previous pictures cannot be reused.

Optional manual review still works: complete `decision_template.json`, saving it
as `output/<log_id>.decisions.json`, reviewing the refined segments. Reviewed distributions also use a simple
0.5 acceptance threshold, without the former 0.95 / margin requirements.
Unreviewed intervals keep their automatic results. Review can explicitly choose
mixed, operational/blank, or no reliable match. The program does not call an
image-assessment service.

Legends are selected from `CENTRE_LEGEND_DIR` (the project root by default), using
`<log_id>.legend.json` first and `legend.json` as a fallback. The configuration's
`source_sha256` must match the original TIFF. An unrelated legend cannot be made
valid by changing its hash: extract and verify swatches from the target scan.

The root-level `60807402860000_gb506#2_md_mudlog.legend.json` and matching PNG
use the annotated legend crop from that TIFF (SHA-256
`336c16e8621e1cc1eab07b1a34df23e4e27f8b8d5aa8c4b361126a688939be00`).
They contain twelve lithology swatches; operational symbols are excluded. The
older generic `legend.json` / `legend.png` are for a different development scan.

With the default Compose mount, new legend files are visible immediately; the
worker retries every ten seconds. With a custom `CENTRE_LEGEND_DIR`, copy both
document-specific legend files there. Rebuild to pick up Python changes:

```sh
docker compose up -d --build centre-classifier
```

For a one-shot run:

```sh
docker compose run --rm --no-deps centre-classifier pipeline.py --once --log-id '60807402860000_gb506#2_md_mudlog'
```
