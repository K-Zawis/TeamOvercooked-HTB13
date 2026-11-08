See [required configuration](../docs/REQUIRED_CONFIGURATION.md) before deployment.

# Integrated pipeline contract

## Delivered behavior
- Dispatcher startup recovery and compose persistence configuration are unchanged byte for byte. The checker asserts their original SHA-256 values and executes isolated LIVE/BATCH/IDLE recovery paths.
- Depth uses the integrated legacy OCR adapter on bounded RGB strips with context. Observations retain global coordinates and ownership clipping. Finite observed values are staged idempotently before completion and ACK.
- Depth run_once advances a retained own-pending cursor first, resetting at the end of the pending scan, scans one XAUTOCLAIM page with count=1 and a retained cursor, then reads new work. Abandoned entries need at least 300000 ms idle. Redis 6.2 two-item and Redis 7 three-item responses are accepted. Claim failures propagate, with no premature ACK. Redis >=6.2 is required.
- Lithology handle_task supplies intervals and legend to complete_lith_chunk as one atomic publication. The Redis Lua implementation checks key types, publishes each slice once, updates its counter, evaluates the merge barrier and acknowledges. Replays do not append duplicate intervals. These are implemented source contracts; real Redis execution remains a separate opt-in acceptance test.
- Lithology overlap ownership is derived from the unchanged dispatcher slicing contract. Global owned intervals are contiguous; overlap is not double counted.
- Merger rejects invalid finite/monotonic anchor data. Fewer than three globally supported anchors exports blank depths, never pixel-derived depths. Query boundaries outside observed support stay blank. CSV completion means an output file was published, not full depth coverage or OCR accuracy.
- CSV publication uses a flushed/fsynced temporary file and atomic replace before setting COMPLETED and acknowledging. Failed publication leaves the task pending; an ACK-loss replay reuses an existing completed CSV.

## Diagnostic evidence
Run `python tools/check_integration.py --compat --output evidence.json` on older Python, or omit --compat on Python 3.15. The contract suite loads first; every tests/test*.py suite is discovered. Compatibility changes only in-memory import syntax and uses import doubles, not a Redis server or TIFF decoder. All test failures remain failures, skips are enumerated separately.
Set SYNTHETIC_OUTPUT_DIR to retain synthetic supported and unsupported-boundary CSV fixtures outside temporary directories. Those fixtures run real extractor/classifier/merger code with injected OCR, image IO and broker, not the deployed stack.

## Remaining acceptance scope
Native Python 3.15, dependency resolution/container builds, real TIFF decoding, actual Tesseract, live Redis Lua and AOF/process-crash recovery require their own runtime evidence. No real dataset is bundled. Unit and synthetic diagnostics do not establish full real-dataset accuracy or completion. Calibration requires three positive-slope supported anchors and residuals within five pixels; no depth-unit-specific pitch range is assumed.
