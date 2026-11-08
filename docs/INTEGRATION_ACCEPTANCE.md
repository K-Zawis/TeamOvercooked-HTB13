See [required configuration](../docs/REQUIRED_CONFIGURATION.md) before deployment.

# Integration acceptance

## What this delivery establishes

Python 3.15 and explicit `lazy import` remain production requirements. The integration changes worker logic and Redis staging. Dispatcher and
compose originals remain unchanged. The checker embeds SHA-256
baselines taken from the supplied original archive for dispatcher.py and compose.yaml.
It checks their full bytes, the constructor's call to recovery, and executes the
original recovery method with interface doubles for LIVE, BATCH and stationary
startup. It checks active-log reconciliation, saved batch/target restoration,
thread target/arguments and daemon start. This is not a Redis durability test.

All four service requirements were reviewed against direct and transitive source
imports. The merger imports lithology's package initializer, which eagerly reaches
Pillow and rasterio when lazy syntax is normalized; both are declared to avoid
implicit reliance on package initialization details. Depth uses the `tifffile`
distribution, not `tiffile`. Depth no longer copies a nonexistent root requirements
file. Dispatcher requirements cover its API/control plane. Native Tesseract is
installed in both OCR images; the merger does not execute OCR. Historical
lithology/merger version pins are replaced by explicit direct requirements rather
than retaining an unverifiable frozen environment. These are NOT reproducible
locks. Build resolution, Python 3.15 support and platform wheels remain host gates.
Rasterio source builds may require additional GDAL development libraries if a
compatible wheel is unavailable; do not call that build accepted without a log.

## Offline check (JSON evidence and nonzero failure exit)

Run from the project root:

```bash
python3.15 tools/check_integration.py --output integration-evidence.json
# On constrained Python 3.11+ only, diagnostic fallback:
python tools/check_integration.py --compat --output integration-evidence-compat.json
```

Only test-harness in-memory copies normalize lazy imports. Production files are
not rewritten. Compatibility mode uses existing import-only Redis/rasterio doubles,
not working substitutes. The checker compiles all delivered src/tests plus itself,
runs every supplied test*.py unittest suite, records actual run/failure/error/skip
counts and tracebacks, and exits nonzero on any failed check or test. Skips are
not passes. Native dependency import failures are errors, not acceptance skips.

The diagnostic result is recorded in the delivered evidence JSON. Atomic lithology publication, shared merge barrier, pending reclaim and unsupported-depth blank handling are implemented. Earlier defect descriptions in the historical audit are superseded by docs/PIPELINE_CONTRACT.md. Compatibility results do not establish native or deployed-stack acceptance.

## Container build and startup (execute on a Docker host)

Prerequisites: Docker Engine with Compose v2, network access for base images and
pip/apt, Python 3.15 image availability, and a reviewed dataset under
`dataset/images/` and `dataset/bbox_annotation/`. The source TIFF and corresponding
JSON annotation must both be present. Use an isolated checkout: compose fixes
container names, host ports and subnet, so a second project name alone does not
isolate a simultaneously running stack. Never reset or flush a production broker.

```bash
mkdir -p output acceptance
set -o pipefail
docker compose config > acceptance/compose-resolved.yaml
docker compose build --pull 2>&1 | tee acceptance/build.log
docker compose up -d
docker compose ps > acceptance/services.txt
for n in $(seq 1 120); do
  curl --fail --silent http://localhost:8000/health > acceptance/health.json && break
  sleep 2
done
curl --fail --silent http://localhost:8000/health
# Ensure imports and OCR executable are actually available inside built images.
docker compose exec -T worker-depth python -c 'import numpy,tifffile,zarr,PIL,imagecodecs,redis; from src.depth.extraction import DepthExtractor; from src.depth.worker import DepthWorker'
docker compose exec -T worker-depth tesseract --version
docker compose exec -T worker-lithology python -c 'import numpy,rasterio,PIL,redis; import src.worker_lithology'
docker compose exec -T worker-lithology tesseract --version
docker compose exec -T merger python -c 'import numpy,rasterio,PIL,redis; import src.worker_merge'
docker compose exec -T redis redis-cli CONFIG GET appendonly appendfsync
docker inspect well_log_redis --format '{{json .Mounts}}' > acceptance/redis-mounts.json
```

Require all commands to succeed, health `healthy` with Redis `connected`,
appendonly `yes`, appendfsync `everysec`, and a named volume mounted at `/data`.
The code hash checks in the offline checker protect the original AOF/volume
compose configuration, not the daemon's actual state.

## Single-document completion

Set FILE to a real annotation filename in the mounted dataset. No sample dataset
is included. Do not substitute a filename-derived depth calibration.

```bash
export FILE='your-reviewed-annotation.json'
python -c 'import json,os; print(json.dumps({"file":os.environ["FILE"]}))' > acceptance/request.json
curl --fail --silent -H 'Content-Type: application/json' \
  --data-binary @acceptance/request.json http://localhost:8000/live/start > acceptance/start.json
curl --fail --silent http://localhost:8000/api/dashboard > acceptance/dispatched.json
docker compose logs --no-color > acceptance/services.log
```

`started` means only that a dispatch thread was started. Capture the document's
actual `log_id` from `/api/dashboard`; it need not equal the annotation stem.
Set LOG_ID to that exact value. The following bounded poll and CSV structural
check can be executed directly:

```bash
export LOG_ID='actual-log-id-from-dashboard'
python - <<'PY'
import csv,json,math,os,pathlib,time,urllib.request
log=os.environ['LOG_ID']
assert pathlib.Path(log).name==log and log not in ('.','..')
for attempt in range(600):
    data=json.load(urllib.request.urlopen('http://localhost:8000/api/dashboard',timeout=10))
    pathlib.Path('acceptance/latest-dashboard.json').write_text(json.dumps(data,indent=2))
    doc=next((d for d in data['documents'] if d['log_id']==log),None)
    if doc and doc['status'] in ('FAILED','CORRUPTED'):
        raise SystemExit('Document failed: '+str(doc))
    if doc and doc['status']=='COMPLETED': break
    time.sleep(2)
else: raise SystemExit('Completion timeout')
for name in ('depth_progress','lith_progress'):
    done,total=map(int,doc[name].split('/')); assert total>0 and done==total,(name,doc)
with open(pathlib.Path('output')/(log+'.csv'),newline='') as f:
    reader=csv.DictReader(f)
    assert {'top_depth','bottom_depth','lithology','confidence','top_y','bottom_y','cluster_id'}<=set(reader.fieldnames or [])
    rows=list(reader)
assert rows,'Empty output is not a successful well-log result'
seen=set(); previous=None
for row in rows:
    # Full-depth acceptance is stricter than supported partial CSV output.
    assert row['top_depth'] and row['bottom_depth'],'Blank calibration: not full-depth acceptance'
    top,bottom=map(float,(row['top_depth'],row['bottom_depth']))
    y0,y1=map(float,(row['top_y'],row['bottom_y']))
    conf=float(row['confidence'])
    assert all(math.isfinite(v) for v in (top,bottom,y0,y1,conf))
    assert top<bottom and y0<y1 and 0<=conf<=1
    assert previous is None or y0>=previous,'Overlapping or unordered output'
    previous=y1
    key=tuple(row.items()); assert key not in seen,'Duplicate row';seen.add(key)
    assert row['lithology'].strip()
print(json.dumps({'scope':'CSV structure, not accuracy','log_id':log,'rows':len(rows),'passed':True}))
PY
```

Also compare sampled depths, labels and global coordinates with manually reviewed
TIFF regions, including crop origin, overlap seams, gaps and final short strips.
UNKNOWN is explicit uncertainty, not proof of correct classification. The structural
check cannot establish recall, physical depth accuracy or correct lithology labels.

## Interrupted startup and AOF recovery

Use a sufficiently large unfinished document in an isolated test stack. First
stop consumers, then start the LIVE request above, and poll dashboard until its
persisted_state says LIVE/is_running true and the document is PROCESSING. If it
completes before the crash, this does not test interrupted recovery: use another
unfinished fixture. Do not POST start again after the crash.

```bash
docker compose stop worker-depth worker-lithology merger
# POST /live/start as above, only once for an unfinished fixture.
curl --fail --silent http://localhost:8000/api/dashboard > acceptance/before-crash.json
docker compose exec -T redis redis-cli HGETALL pipeline:state > acceptance/state-before.txt
# Require running LIVE state and an in-flight document before proceeding.
# Make the baseline a known durable snapshot; Redis everysec alone can lose
# up to approximately one second of recent writes on abrupt failure.
docker compose exec -T redis redis-cli SAVE
docker compose exec -T redis redis-cli INFO persistence > acceptance/persistence-before.txt
docker compose kill -s SIGKILL dispatcher redis
docker compose up -d redis dispatcher worker-depth worker-lithology merger
for n in $(seq 1 120); do
  curl --fail --silent http://localhost:8000/health > acceptance/health-after.json && break
  sleep 2
done
curl --fail --silent http://localhost:8000/api/dashboard > acceptance/after-crash.json
docker compose logs --no-color dispatcher > acceptance/dispatcher-recovery.log
docker compose exec -T redis redis-cli INFO persistence > acceptance/persistence-after.txt
```

Require the same tracked log and saved target/batch to survive. Recovery logs
must show disruption auto-resume without a second start request. Apply the
bounded completion and CSV check above to the same LOG_ID. Inspect stream lag
and pending counts in dashboard after completion; investigate stalled entries.
`SAVE` is an explicit durable baseline, not proof of zero-loss everysec AOF behavior.

Repeat with POST `/batch/start` body `{"batch_size":1}` on multiple reviewed logs,
crashing while BATCH/is_running is true. Compare saved current_batch and
batch_size across restart; require remaining documents to complete exactly once,
and stable already-completed CSV hashes. A same-file retry after completion is
not an interrupted BATCH test. For a pending-entry exercise, crash an active
worker after observing a nonzero pending count in queue_breakdown, then restart
it and require pending/lag to drain. Capture this as a separate result.

Never use `docker compose down -v`, `/reset` or FLUSHALL in this recovery test:
they intentionally destroy the evidence being tested. A normal finish may use
`docker compose stop`; leave the named Redis volume intact.

## Recorded execution scope

The supplied evidence JSON comes from Python 3.11 compatibility diagnostics.
Docker builds/startup, native Python 3.15 imports, live Redis/AOF crash recovery,
real TIFF datasets, manual OCR accuracy and end-to-end output completion were
NOT EXECUTED in that environment. The commands above are runnable acceptance
instructions, not a record of those commands having succeeded.
