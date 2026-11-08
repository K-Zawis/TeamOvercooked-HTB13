# Required slice configuration

The immutable compose.yaml sets dispatcher SLICE_HEIGHT=2000 and its default
SLICE_OVERLAP_RATIO=0.10. Lithology workers MUST use exactly the dispatcher
geometry. Matching a single task is not proof of matching the whole dispatch.
This archive does not propagate nondefault dispatcher settings automatically.
When changing either setting, run both services with the same explicit values:

```sh
docker compose stop dispatcher worker-lithology
docker compose run -d --service-ports -e SLICE_HEIGHT=2000 -e SLICE_OVERLAP_RATIO=0.10 dispatcher
docker compose run -d -e SLICE_HEIGHT=2000 -e SLICE_OVERLAP_RATIO=0.10 worker-lithology
```

Do not mix an existing dispatch with different geometry; retain the original
settings across interrupted recovery. Originals remain byte-identical.

Global merger calibration now requires at least three supported observations,
a positive fit, pixel-residual and competing-scale gates. One/two observations
produce blank depths, never a raw-point fallback or unit-specific assumed pitch.
Depth staging accepts new fingerprints only in PROCESSING for an expected,
not-yet-staged slice. Exact fingerprint replay is read-only after merge too.
Own pending polling advances a cursor and resets after an empty page, allowing
abandoned claims and new tasks a turn even when the earliest pending is poison.
Errors stay pending; there is no dead-letter policy.

Validation is offline compatibility diagnostics, not live Redis, Docker,
Python 3.15 or real-dataset acceptance. Historical pass totals are not current.
