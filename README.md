# mlseedr — Seedr marketing ML

Segmentation + conversion/retention modelling for Seedr.cc, on the ClickHouse
warehouse at `data.seedr.cc`.

> ⚠️ **Data is NOT in this repo.** All datasets contain user-level data
> (user_id, country, LTV, payments) and live on the server — in the ClickHouse
> `ml` database (and as local caches, git-ignored). This repo holds **code +
> documentation only**. Reproduce datasets/models with the scripts below.

## Layout

```
main.go, segments.go      Go: extract the 8 marketing segments → CSV (segments_out/)
cmd/dataset/              Go: build the ML training set (+ -uplift mode) from CH
ml/*.py                   Python: training, per-segment models, NN backbone+heads,
                          PLR, calibration, uplift, and load_to_ch.py (CSV → ClickHouse)
docs/seedr/               Warehouse + project reference (schema, gotchas, access)
docs/ml/                  Dataset dictionary, research synthesis, learning guide
SEEDR_ML_SUMMARY_{RU,EN}.md   Top-level summary (datasets, segments, models, recommendations)
SEEDR_MARKETING_SEGMENTS.md   Segment catalogue + sizing
```

## Data on the server (ClickHouse `ml` database)

Loaded via `ml/load_to_ch.py`. Read-write is scoped to `ml.*` and `shaya.*`
(role `shaya_rw`); production telemetry (`seedr_telemetry`, `payments`) is
read-only. Key tables:

| Table | Rows | What |
|---|---|---|
| `ml.train_email_conversion` | 3.53M | main training set (conversion after email) |
| `ml.train_uplift` | 3.68M | + control rows + `treatment` for uplift |
| `ml.test_predictions` | 694K | held-out scores |
| `ml.segment_*` (8) | — | operational per-segment user lists |
| `ml.segtrain_*` (6) | — | per-segment training subsets |

## Quick start

```bash
# 1. SSH tunnel to data.seedr.cc (ports 8123/9000); creds in ~/.clickhouse.seedr
# 2. Go: segments + dataset
go run .                              # → segments_out/<date>/*.csv
go run ./cmd/dataset                  # → train_email_conversion.csv.gz
go run ./cmd/dataset -uplift          # → train_uplift.csv.gz
# 3. Python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python ml/train.py          # baseline + GBM
.venv/bin/python ml/nn_multitask.py   # multi-task backbone + heads
.venv/bin/python ml/load_to_ch.py     # push datasets → ClickHouse ml.*
```

See `SEEDR_ML_SUMMARY_EN.md` for results and recommendations.
