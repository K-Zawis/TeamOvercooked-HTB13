# Depth validation — 7 November 2026

## Environment and checks

Tested with Python 3.13.12, Tesseract 5.5.4 and English tessdata 4.1.0, NumPy 2.5.4, Pillow 13.0.0, rasterio 1.5.2, and pytest 9.2.0. OCR ran locally with one OpenMP thread per call. Temporary dependencies were installed outside the repository.

`python -m pytest -q`: **38 passed**, including actual Tesseract on a rendered scale streamed at heights 71, 200, and 600. Tests also cover numerical parsing, rotations, seam recovery, pending anchors, conflicting readings, state rollback, interpolation limits, resets, and a 75-strip bounded-state stream.

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
python -m pip install -r requirements-validation.txt
python -m pytest -q
python validate_depth.py --dataset ../dataset --output /tmp/depth-selected.json
python validate_depth.py --dataset ../dataset --all --max-strips 3 --heights 2000 --output /tmp/depth-smoke.json
```

Tesseract and English language data must also be installed. The JSON reports include emitted anchors, missed reviewed labels, timing, and retained candidate counts. Use `--tesseract` for a custom executable. Timings are observations, not a controlled benchmark.

`worker.py` and lithology processing are unchanged. The worker still needs an explicit extractor reset between documents and an implementation of its `calculate_depth` stub. Complete multi-document pipeline depth output has therefore not been validated. Only interpolation inside retained trusted support is authorized; no extrapolation is performed.
