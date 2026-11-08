# Historical depth validation · 7 November 2026

These are inherited measurements from an earlier source snapshot, not execution evidence for this integrated archive. Refer to integration-evidence.json for the current compatibility run. The historical dataset, reference files and commands below are not bundled.

## Environment and checks

Tested with Python 3.13.12, Tesseract 5.5.4 and English tessdata 4.1.0, NumPy 2.5.4, Pillow 13.0.0, rasterio 1.5.2, and pytest 9.2.0. OCR ran locally with one OpenMP thread per call. Temporary dependencies were installed outside the repository.

The original extraction-only suite had **38 passing tests**, including actual Tesseract on a rendered scale streamed at heights 71, 200, and 600. Tests also cover numerical parsing, rotations, seam recovery, pending anchors, conflicting readings, state rollback, interpolation limits, resets, and a 75-strip bounded-state stream. The earlier snapshot reported **110 passing tests** including broker compliance. This is not the count for the current integrated package.

## Selected full-column runs

Four real depth columns were processed to their ends at each strip height. Manual references cover only the early review windows in `tests/depth_reference.json`, with a five-pixel matching tolerance. Later anchors are unverified. The original TIFFs and annotations were not modified.

For each held-out check, the label text was masked in the RGB input before OCR. These separate checks process only the early region needed to bracket the hidden label. Reference depths are never supplied to extraction.

| Log | Strip height | Full-run strips | Emitted anchors | Reviewed labels matched | Masked-run labels matched | Held-out absolute depth error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| log06 | 1000 | 7 | 15 | 4/4 | 3/3 | 0.100 |
| log06 | 2000 | 4 | 15 | 4/4 | 3/3 | 0.100 |
| log06 | 3000 | 3 | 16 | 4/4 | 3/3 | 0.100 |
| MC761 MD | 1000 | 12 | 48 | 9/9 | 8/8 | 0.500 |
| MC761 MD | 2000 | 6 | 46 | 9/9 | 8/8 | 0.500 |
| MC761 MD | 3000 | 4 | 48 | 9/9 | 8/8 | 0.500 |
| MC765 TVD | 1000 | 132 | 629 | 8/9 | 5/8 | 0.060 |
| MC765 TVD | 2000 | 66 | 639 | 9/9 | 8/8 | 0.050 |
| MC765 TVD | 3000 | 44 | 634 | 9/9 | 8/8 | 0.050 |
| Rio Nueces 222 | 1000 | 117 | 1060 | 14/19 | 12/18 | 0.100 |
| Rio Nueces 222 | 2000 | 59 | 1054 | 14/19 | 11/18 | 0.100 |
| Rio Nueces 222 | 3000 | 39 | 1056 | 13/19 | 11/18 | 0.100 |

All 24 selected runs completed with 0 execution errors. There were 0 false detections in the manually reviewed windows, across baseline and masked checks. Mean absolute pixel error for matched anchors was at most 1.0 pixel. Depth errors use the numerical units printed on each diagram; no unit conversion was applied.

Changing strip height changes OCR context and can change which labels pass calibration. MC765 at 1000 pixels misses reviewed labels that are recovered with larger strips. Rio Nueces has materially lower label recall despite accurate interpolation of the single masked check. A good held-out value does not imply complete extraction. Retained candidates never exceeded 256.

## Dataset smoke coverage

All 51 annotated images were tested at 2000 pixels for up to three strips each (at most the first 6000 depth-column rows). Including four held-out checks, all 55 runs completed with 0 execution errors. 44 images returned trusted anchors; 7 returned none in this sampled region. These are execution/coverage counts, not accuracy scores for the other 47 images.

No-anchor samples:

- `60808400140000_hadrian 2_masterlog_2in_10000-21962.json`
- `60811407300100_bp_gc743_15607_dc222_st00bp01_mudlog_1md_10700-22872.json`
- `60811407570000_bp_gc743_15607_dc802_st00bp00_mudlog_5md_6900-17643.json`
- `60818400650000_at0574_4_neptune_1inch_tvdmudlog_6300-17378.json`
- `Mudlog-Trail Mtn1Palisades.json`
- `Shell_08852_KE001_ST01BP00_1inTVD_MudLog_17564-20124ft.json`
- `Shell_AC0336_OCS-G-36102_001_ST00BP01_5inchMD_MudLog_9100 ft -27770 ft.json`

Visual inspection of these crops shows competing printed numeric series, abbreviated labels, and clipped or occluded numbers in several cases. The implementation does not select a geological depth kind among competing scales or expand abbreviated numbers. Conservative consensus can also reject readable labels when OCR introduces too much conflicting evidence. Further scale selection and OCR improvement remain necessary for those logs.

## Reproduce and integration limits

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked python validate_depth.py --dataset ../dataset --output /tmp/depth-selected.json
uv run --locked python validate_depth.py --dataset ../dataset --all --max-strips 3 --heights 2000 --output /tmp/depth-smoke.json
```

Tesseract and English language data must also be installed. The JSON reports include emitted anchors, missed reviewed labels, timing, and retained candidate counts. Use `--tesseract` for a custom executable. Timings are observations, not a controlled benchmark.

The former worker reset/calculate_depth-stub gap is superseded: the integrated broker worker uses stateless extract_observations() per owned slice. The final merger exports unsupported depths blank, without extrapolation. The current archive has synthetic pipeline diagnostics; no current real-dataset acceptance is asserted.

## Redis-stream worker amendment

`worker_depth.py` was validated with tifffile 2026.10.15, Zarr 3.4.2, and
imagecodecs 2026.10.10. The initial broker adapter had **70 passing tests**, including
consumer-group delivery, global-coordinate hash writes, delayed anchors,
interleaving/reset behavior, malformed tasks, pending-task retries, write and
acknowledgement failures, and both CLI modes. TIFF tests cover LZW strips, tiles,
planar RGB, grayscale, MINISWHITE, bilevel, palettes, and RGBA; they prohibit a
full-page `TiffPage.asarray` call during crop extraction.

An end-to-end check submitted the first two 2000-row tasks from each selected log
through `StubRedisClient` and the actual Tesseract extractor. For all eight tasks,
the tifffile crop matched the existing rasterio reader pixel-for-pixel, emitted
rows remained inside the source depth-column box, and no tasks remained pending.

| Log | Tasks | Hash anchors after both tasks |
| --- | ---: | ---: |
| log06 | 2 | 8 |
| MC761 MD | 2 | 15 |
| MC765 TVD | 2 | 19 |
| Rio Nueces 222 | 2 | 33 |

These counts describe broker integration on the first 4000 rows, not accuracy
scores or a repeat of the full-column validation above. The real Redis server,
network failures, and abandoned-consumer recovery have not been tested: those historical tests used an in-memory stub. The delivered source now uses PipelineRedisClient and supports abandoned-consumer claiming, with live Redis validation still outstanding. The initial
adapter's dependence on contiguous same-log tasks has been removed by the
compliance changes below. The earlier broker counts are historical measurements.

## Completion counting and independent-task staging

The historical suite reported **110 passed**. In addition to the TIFF and OCR tests,
the actual completion Lua script is exercised with fakeredis 2.40.0 and lupa 2.8,
as well as the in-memory client. Coverage includes concurrent duplicate commits,
empty successful results, invalid Redis key types, invalid/overflowing counters,
integer precision beyond 2^53, lost commit responses, failed acknowledgements,
worker restarts, and duplicate dispatches under different stream IDs. Both byte
and decoded redis-py stream replies are tested, including empty pending lists.

Successful tasks atomically stage their global-Y observations, increment
`log:<id>:meta` field `completed_depth`, and record their fingerprint in
`log:<id>:depth_completed_tasks`. XACK follows the commit. A task already marked
complete skips decoding/OCR and is acknowledged without incrementing again.
Successful tasks with zero observations also increment completion exactly once.

Broker workers now use stateless `extract_observations()`, with up to 128 context
rows above/below each requested slice and half-open ownership of label centres.
One or two confident labels are immediately staged instead of buffered locally.
Unlike the legacy `extract()` calibration path measured in the full-column tables,
these are OCR observations without a three-anchor global fit; the merger owns
global depth consistency checks and interpolation. More staged observations do
not establish greater accuracy. The existing legacy stream API is preserved.

Real-data integration submitted eight distinct 2000-row tasks from the same four
logs in reverse order, then four duplicate entries, constructing a fresh worker
for every delivery. All 12 entries were acknowledged; each log's completion
counter was exactly two, and every published row belonged to its task slice.

| Log | Distinct tasks | completed_depth | Staged observations |
| --- | ---: | ---: | ---: |
| log06 | 2 | 2 | 10 |
| MC761 MD | 2 | 2 | 19 |
| MC765 TVD | 2 | 2 | 20 |
| Rio Nueces 222 | 2 | 2 | 35 |

A separate 60-row MC761 task, processed alone with surrounding context,
immediately staged the single observation `"3060": "17200.0"` and incremented
its completion counter to one. No prior strip or worker-local state was needed.
These checks used actual TIFFs and Tesseract, with the stub Redis client. A real
Redis deployment and persistence/failover behavior remain untested. The dispatcher
must clear the completion-marker set together with the other staging keys before
starting a fresh run for a reused log ID.
