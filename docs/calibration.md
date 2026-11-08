# Review-gate calibration matrix (C5)

Real-model pushes through the full gate pipeline (hook -> supervisor ->
sandbox clone -> `kimchi -p "/skill:review --range A..B --json"` -> verdict
store), driven by `scripts/ocr_calibrate.py`. `stub` rows prove the plumbing
with the stub worker and no model. A `block` on a `pass` row is a false
positive; its findings are recorded verbatim.

| id | source | range | expected | verdict | blocked | decision | findings | wall_s | note |
|---|---|---|---|---|---|---|---|---|---|
| stub-py-app-a5ccc09-ddbb912 | py-app | a5ccc09..ddbb912 | pass | pass | no | pass | (none) | 1.5 | plumbing proof [stub worker] |
