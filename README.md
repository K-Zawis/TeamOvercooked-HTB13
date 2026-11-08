# Streaming depth extraction

`src.depth.DepthExtractor` consumes contiguous RGB strips from a cropped depth-scale
column. It reads numeric labels, rejects inconsistent OCR, and interpolates
between trusted observations. Rock segmentation and naming remain separate.

## Setup

Use Python 3.15. Runtime dependencies live in `pyproject.toml`; the `dev`
dependency group includes pytest, rasterio, and fakeredis with Lua support.
`uv.lock` pins the resolved versions, and `.python-version` selects Python 3.15.

```bash
uv python install
uv sync --locked
```

Without uv, create a Python 3.15 virtual environment and install the generated
pip requirements instead:

```bash
python3.15 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
# To include the validation/test tools as well:
python -m pip install -r requirements-validation.txt
```

Pip users can run `python -m src.depth`, `python -m src.dispatcher`, and
`python -m pytest` directly inside that environment; uv is not required.

Keep `pyproject.toml` as the dependency source of truth. After changing dependencies,
update the lock and regenerate both pip exports; do not edit the requirements
files by hand:

```bash
uv lock
python tools/sync_requirements.py
python tools/sync_requirements.py --check
```

The exports pin runtime dependencies (`requirements.txt`) and runtime plus the
`dev` group (`requirements-validation.txt`) to the same versions as `uv.lock`.
The `--check` command exits nonzero if either export is stale or if the lockfile
no longer matches `pyproject.toml`, and can also be run in CI.

`uv sync` includes the development group by default. For a worker-only environment,
use `uv sync --locked --no-dev` and `uv run --locked --no-dev python -m src.depth`.
Some native dependencies may need a source build if a Python 3.15 wheel is not
available for your platform. Install the
Tesseract executable and English language data through your system package
manager (for example, `tesseract-ocr` and `tesseract-ocr-eng` on Debian/Ubuntu).
No network requests or model downloads occur during extraction.

Heavy imaging and Redis imports use Python 3.15's explicit `lazy import` syntax.
They load on first use, so importing pipeline modules or showing worker/validator
CLI help does not load NumPy, Pillow, tifffile, Zarr, imagecodecs, or redis-py.
Library internals retain their normal import behavior.

```python
from src.depth import DepthExtractor

extractor = DepthExtractor()
extractor.reset()  # required at the start of EVERY document
offset = 0
anchors = {}
for rgb_strip in strips:  # H x W x 3 uint8; default caller height is 2000
    result = extractor.extract(rgb_strip)
    anchors.update({y + offset: d for y, d in result["y_to_depth_map"].items()})
    # A query uses pixels relative to the start of this document's depth crop.
    depth = extractor.depth_at(offset + len(rgb_strip) // 2)
    offset += len(rgb_strip)
```

Strip heights can vary, including a short final strip. Width must remain fixed.
Add the depth crop's original-image top coordinate to accumulated anchor rows
when mapping them back to the source image. Depth units remain those printed on
the diagram; no unit conversion or filename-derived calibration is performed.

`extract_observations(rgb_strip)` is the separate, stateless API used by broker
workers. It immediately returns confident, unclipped, unambiguous numeric labels,
including one or two labels, without retaining them for later calls. It neither
uses nor modifies the legacy stream calibration. Global calibration and
interpolation of these observations belong to the merger.

## OCR replacement and failures

```python
from src.depth import OCRToken, TesseractOCR

class AlternativeOCR:
    def recognize(self, rgb_image):
        # Return OCRToken(text, confidence, (left, top, right, bottom)).
        # Confidence is 0..1; boxes use this input's original pixel dimensions.
        # Input may be rotated; the extractor reverses that transformation.
        return []

extractor = DepthExtractor(ocr_backend=AlternativeOCR())
# Or customize local Tesseract:
extractor = DepthExtractor(TesseractOCR(executable="tesseract", timeout=30, scale=2))
```

Backend calls are synchronous and must not mutate the input array. The extractor
tries upright and both quarter-turn orientations. Tesseract applies contrast
enhancement, bounded enlargement, padding, and straight-frame cleanup.

Invalid image inputs raise `ValueError`. Missing Tesseract, invalid TSV, timeouts,
and nonzero OCR exits raise `DepthExtractionError`. A failed extraction does not
advance stream state; retry that same strip. A valid strip with no usable labels
returns an empty map and advances normally. A replacement backend should raise
`DepthExtractionError` for its execution failures.

## Coordinates, calibration, and limits

- Output contains newly trusted observed anchors, not an entry for every row.
  At least three numeric anchors must agree on a positive linear scale within
  five pixels (configurable `residual_pixels`), with at least 60% support.
  `min_confidence` defaults to 0.5. Conflicting readings at the same row are
  excluded. Pending labels can become trusted after subsequent strips arrive.
- Delayed labels have **negative strip-local rows**, including bootstrap labels
  older than the seam tail. Always add the current strip offset, even for negative
  keys. Do not discard them. Previously emitted rows are not retracted.
- `depth_at(y)` uses piecewise linear interpolation between retained trusted
  observations and returns `None` outside their support. It never extrapolates.
  Its coordinate origin is the start of the stream, not the current strip or image.
- Retained state is limited to 128 RGB rows, 256 recent numeric candidates, and
  bounded calibration/emission metadata. Old support eventually expires from
  `depth_at`; callers may keep emitted anchors separately. Processing memory is
  proportional to the supplied strip, not the full log height.
- Labels clipped at the strip edge are deferred to the next strip. A glyph
  extending more than 128 rows above a seam may not be recoverable. A clipped
  label at the end of the document is discarded. Text centres are approximate
  depth positions; isolated nearby ticks are preferred when available.
- Strong scale changes, curved/skewed scans, abbreviated labels, sparse scales,
  and systematic OCR errors can defeat consensus. A positive fit is not proof of
  geological correctness. Fewer emitted anchors indicate missing evidence, not
  permission to fill unsupported depths.

## Depth module layout

Depth functionality lives in `src.depth`: `extraction` provides OCR and the
offline streaming API, `worker` owns task parsing, TIFF crops and Redis consumption,
`staging` publishes results atomically, and
`validation` retains dataset checks. Public types can be imported from `src.depth`.

Run `python -m src.depth` for the worker or `python -m src.depth.validation` for
offline validation. `python -m src.worker_depth` is the thin entry point used by
Compose, matching `src.worker_lithology`.

The unused root `worker.py` and `models.py` scaffolds have been removed: their
monolithic flow combined the depth and lithology stages, used dummy lithology
results, and had an unimplemented interpolation method. Global depth interpolation
and final interval assembly belong to the merger in the system diagram. The
ordered extraction/calibration API remains available for offline validation; the
Redis worker stages independent observations without invoking it.

## Redis depth worker

`src/depth/worker.py` consumes tasks from Redis Stream `stream:depth_tasks` using
consumer group `group:depth_workers`, using the supplied `PipelineRedisClient`.
The worker has no command-line options or in-memory Redis fallback. It runs
continuously and consumes tasks dispatched to Redis. Configure it with:

| Variable | Default |
| --- | --- |
| `REDIS_HOST` | `172.28.0.2` |
| `REDIS_PORT` | `6379` |
| `REDIS_DB` | `0` |
| `CONSUMER_NAME` | `DepthWorker` |

Use a stable, distinct `CONSUMER_NAME` for each worker instance. For a local run:

```bash
REDIS_HOST=localhost REDIS_PORT=6379 CONSUMER_NAME=depth-worker-1 uv run --locked python -m src.depth
```

For programmatic use, pass the actual wrapper:

```python
from src.depth import DepthWorker
from src.redis_client import PipelineRedisClient

DepthWorker(PipelineRedisClient(), consumer_name="depth-worker-1").run_forever()
```

The worker creates its consumer group at `0-0` to include existing tasks. Stream
entries normally carry one `data` field containing the JSON task:

```python
PipelineRedisClient().r.xadd("stream:depth_tasks", {"data": json.dumps({
    "log_id": "0000000009",
    "image_path": "/data/images/0000000009_d-582_0000020435-0001_log01_fe_1in_md.tif",
    "bbox": [1271, 4392, 1407, 92668],
    "y_start": 4392,
    "y_end": 6392,
})})
```

Flat stream fields are also accepted, with `bbox` JSON-encoded and pixel
coordinates encoded as integer strings. Byte replies and decoded Redis replies
are supported. `log_id` stays a string to preserve leading zeroes. Bounding boxes
are `[left, top, right, bottom]`, max-exclusive; `y_start` and `y_end` are absolute
image rows inside that box. Requested slices can be 2000 rows or another positive
height, including a shorter final slice.

The worker opens the given TIFF path directly. `tifffile` exposes the first page
as a chunked Zarr store; only blocks intersecting the requested crop are decoded.
This avoids allocating the whole image, although a TIFF's native block size
still controls minimum decoder memory. RGB, planar RGB, 8-bit grayscale,
bilevel, palettes, and RGBA are normalized to RGB. Unsupported axes, color
spaces, and non-palette high-bit-depth samples are rejected explicitly.

Each task independently reads up to 128 context rows above and below its slice,
clamped to the column box. This recovers labels crossing task boundaries without
requiring adjacent tasks on the same worker. Only labels whose centres lie in
the task's half-open `[y_start, y_end)` range are staged. `context_rows` is
configurable on `DepthWorker`; the decoder's `read_depth_strip` helper itself
still reads exactly the supplied range.

Each detected local row is offset by the context crop's absolute top and written directly
to Hash `log:<log_id>:depths`, for example `HSET log:0000000009:depths 4492 17200.0`.
Redis fields and values are numerical strings; there is no nested result JSON.
An empty result creates no depth hash entry but still counts as completed.

No labels or calibration are buffered between broker tasks. Separate workers,
interleaved logs, reversed order, and process restarts produce independent task
results. The legacy `extract()` method retains its ordered-stream behavior for
other callers; broker workers use only `extract_observations()`.

The Lua operation in `src/depth/staging.py` publishes depth hash entries, executes
`HINCRBY log:<id>:meta completed_depth_chunks 1`, synchronizes the compatibility
field `completed_depth`, and records a completion marker in
Set `log:<id>:depth_completed_tasks` as one atomic Redis operation. Other metadata
fields are preserved. The marker is a SHA-256 fingerprint of the log ID, image
path, column box, and requested rows, so duplicate dispatches with different
stream IDs count only once. The worker then calls XACK. Lost commit responses or
failed acknowledgements can be retried without repeating OCR or incrementing
completion again. Empty successful tasks follow the same completion protocol.
The commit also records the slice in `log:<id>:slices:depth:completed`, matching
dispatcher recovery. The worker calls `PipelineRedisClient.complete_depth_chunk`
after staging so the provided client acknowledges and evaluates the merge barrier.
Depth tasks do not contain a lithology column box, so the merge request leaves
that optional box unset rather than passing the depth column as a lithology box.

The dispatcher must reset the completion-marker set together with depths and
metadata when starting a fresh run for a reused log ID. Keep those staging keys
for the duration of retries; the merger does not need to read the marker set.
Results and counter updates use the same keys specified by the system design.

OCR, decoding, and Redis failures leave the task pending. The worker retries its
own pending messages before new work, backing off one second on errors. Delivery
is at least once, with deduplicated publication and counting. Claiming abandoned
consumers' tasks and dead-letter handling remain governed by the shared Redis
client and dispatcher. Use a stable `consumer_name`
when restarting a worker to resume its pending work.

With Docker Compose, the service receives its connection settings from the
configured environment and starts the same worker entry point:

```bash
docker compose up worker-depth
```

The dispatcher owns task creation. The worker does not load task files, enqueue
tasks, drain-and-exit, or print final result JSON. It blocks while Redis is idle,
processes tasks when they arrive, and continues polling after each completion.
Results remain in Redis and progress is logged to the process output.

## Tests and dataset validation

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked python -m src.depth.validation --dataset ../dataset --output /tmp/depth-selected.json
uv run --locked python -m src.depth.validation --dataset ../dataset --all --max-strips 3 --heights 2000 --output /tmp/depth-all.json
```

Set `TESSERACT_BIN` for tests, or `--tesseract` for validation, to use a nonstandard
executable path. OCR integration tests skip when Tesseract is unavailable. Unit
tests use a replaceable fake engine and require no OCR installation.

The validator uses rasterio windows, including palette expansion, rather than
decoding entire TIFFs. It runs four selected logs at heights 1000, 2000, and 3000;
`--all` includes all annotations. `--max-strips` bounds a smoke run. It writes a
JSON report after each run, records OCR errors, and exits nonzero if any run fails.
Successful execution does not imply accurate extraction or nonempty results.

`tests/depth_reference.json` contains small manual transcriptions and reviewed
windows. Only those windows have recall, false-detection, and pixel-error metrics.
Each selected log also has one held-out label: its entire text region is masked
before OCR, then its reference depth is compared with `depth_at`. Reference
depths are never supplied to the extractor. Original dataset files are unchanged
and are not included in repository fixtures. See `DEPTH_VALIDATION.md` for measured
results and remaining limitations.
