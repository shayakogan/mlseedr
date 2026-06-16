# Seedr — ML Dataset: "Conversion after a marketing email"

Training set for predicting whether a user pays within N days of receiving a marketing email.
Built 2026-06-11 by `cmd/dataset` (this repo) from the ClickHouse warehouse. Companion docs:
`SEEDR_MARKETING_SEGMENTS.md` (segment portfolio), `SEEDR_DATA_GUIDE.md` (warehouse rules).

## 1. Files

| File | What |
|---|---|
| `train_email_conversion.csv.gz` | 3,529,143 rows × 61 cols, 55 MB gzipped |
| `dataset_cache/` | raw per-user-day extracts (resumable cache, ~28M rows TSV) |
| `cmd/dataset/main.go` | the builder; rerunnable as new data accumulates |

```bash
go run ./cmd/dataset                 # extract (skips cached months) + build
go run ./cmd/dataset -phase build -neg-rate 1.0   # rebuild without negative sampling
```

## 2. Design

- **Sample unit:** `(user_id, day)` on which the user received ≥1 **marketing** email.
  - mautic era (2025-01-10 → 2026-05-28): every `email.sent` with `src='mautic'` (mautic was the campaign platform).
  - internal era (since 2026-05-28): sends whose `email_id` had ≥1,000 sends that day (bulk campaigns; small-volume lifecycle/transactional sends are excluded from samples but counted in features).
- **Date range:** sends from **2025-06-26** (30d after web telemetry starts, so behavior windows are complete) to **today − label-days − 1** (label window complete). With today = 2026-06-11 and the 14-day label this means ≤ 2026-05-27 — i.e. **the current training window is effectively the mautic era**; internal-era samples enter automatically on later re-runs.
- **Leakage rule:** every feature window ends at `d−1`. Labels look only forward. Exceptions flagged below (`mobile_share_now`).
- **Class balance:** all 56,919 positives kept; negatives downsampled to 25% deterministically (FNV hash of user+day). `sample_weight` (1.0 for positives, 4.0 for negatives) reconstructs the population: Σweights = 13,945,815 ≈ 13,945,848 candidates.

## 3. Labels

| Column | Definition | Count |
|---|---|---|
| `label_payment_14d` | any completed `revenue_facts` txn in `(d, d+14]` | 56,919 (0.408% of candidates) |
| `label_conv_14d` | same AND user not premium at send — **the primary free→paid / win-back target** | 8,309 |
| `label_sub_started_14d` | `subscription.created/reactivated` event in window (observable since 2026-01-12 only — see `subs_observable`) | 5,094 |
| `days_to_payment`, `first_payment_usd` | first-payment details, −1/0 when none | |

`label_payment_14d` on premium users is dominated by auto-renewals (see `seg_monthly_loyal`
positive rate of 39%) — do not interpret it as email-driven conversion without
conditioning on `premium_at_send`.

## 4. Feature dictionary (by group)

**Identifiers / context:** `user_id`, `send_date`, `era` (mautic|internal), `sends_today`.

**Email engagement** (all email incl. transactional): `em_sent_7/30/90`, `em_opened_30/90`,
`em_clicked_90`, `em_open_rate_90`, `days_since_last_send`, `days_since_last_open` (−1 = never).

**Web behavior** (`surface IN ('web','landing')`, spans both eras correctly):
`web_active_days_7/30`, `pageviews_7/30`, `file_dl_30/90`, `archive_dl_30/90`, `file_views_30`,
`streams_30/90`, `pricing_views_30/90` (url-substring proxy), `goal4_30/90` (clicked-pay firings),
`days_since_web_activity`.

**Tasks/quota:** `tasks_completed_30`, `tasks_failed_30`, `storage_warnings_30`,
`tasks_observable` (1 = the full 30d window is covered by the task stream).

**Monetary state (before d):** `ever_paid`, `ltv_before_usd`, `txns_before`,
`days_since_last_txn`, `last_txn_amount`, `txns_365d`,
`premium_at_send` (proxy: txn ≤35d ago, or annual-sized txn ≥$60 ≤370d ago).

**Subscription lifecycle:** `last_sub_event` (e.g. `cancellation_scheduled`),
`days_since_sub_event`, `subs_observable` (1 = send after 2026-01-12).

**Profile:** `country` (modal), `tenure_days` (since first event or first txn),
`devices` (lifetime distinct vids), `mobile_share_now`.

**Segment flags at send time** (historical reconstruction of the 8 campaign segments):
`seg_storage_warning`, `seg_heavy_downloader`, `seg_streamer`, `seg_cart_abandoner`,
`seg_winback_active`, `seg_dormant_payer`, `seg_soft_cancel`, `seg_monthly_loyal`.

## 5. Per-segment conversion (all 13.9M candidates, label_payment_14d)

| Segment at send | Samples | Positives | Rate | vs 0.408% base |
|---|---|---|---|---|
| `seg_monthly_loyal` | 107,558 | 41,989 | 39.04% | renewals, not conversions |
| `seg_soft_cancel` | 2,741 | 116 | 4.23% | ×10.4 |
| `seg_heavy_downloader` | 122,919 | 5,093 | 4.14% | ×10.2 |
| `seg_winback_active` | 96,709 | 3,585 | 3.71% | ×9.1 |
| `seg_dormant_payer` | 33,512 | 1,079 | 3.22% | ×7.9 |
| `seg_cart_abandoner` | 7,613 | 32 | 0.42% | ×1.0 (proxy is weak — see caveats) |
| `seg_storage_warning` | 0 | — | — | stream starts 2026-06-01, after the window |
| `seg_streamer` | 0 | — | — | video events start 2026-05-16 |

Even before any model, behavioral flags alone separate ×8–10 — consistent with the
Usage-PQL research in `SEEDR_MARKETING_SEGMENTS.md`.

## 6. Caveats (read before training)

1. **Stream coverage discovered while building (corrects the docs):** `task.*` events exist
   only since **2026-05-25**, `account.storage_warning` since **2026-06-01**, `category='video'`
   web events since **2026-05-16** — NOT since 2026-01-12 as previously documented (that date
   holds only for `subscription.*`). Hence `tasks_observable=0`, `seg_storage_warning=0`,
   `streams_*≈0` across the current training window. They become useful on re-runs once
   internal-era sends age past the label window.
2. **`seg_cart_abandoner` is a weak proxy** (pricing-URL pageviews): funnel goals 1–3 carry no
   `user_id` (guide §6), and logged-in pricing pageviews are rare. Don't trust this flag; the
   per-vid funnel can't be reconstructed per user historically.
3. **`premium_at_send` is a heuristic** (revenue cadence). It misclassifies long-cycle plans
   with odd amounts; `user_subscription_state` has no history so a point-in-time truth doesn't exist.
4. **`mobile_share_now` is measured today**, not at send time — a deliberate, mild leak
   (device preference is sticky). Drop it for strict causal evaluation.
5. **Mautic volume ramped up** through 2025 (Apr-2025 ≈ 1.3K user-days → Aug-2025 ≈ 1.6M);
   early months are sparse, and email-history features for mid-2025 sends see a shorter
   effective history.
6. **Open/click tracking parity** differs between eras (mautic 19%/1.6% vs internal 14.8%/0.6%,
   under investigation by the email team) — `era` is included so the model can absorb the shift.
7. **Time-based validation is mandatory:** split train/test by `send_date` (e.g. train ≤ 2026-02,
   test ≥ 2026-03). Random splits leak user-level future information.
8. Use `sample_weight` for calibrated probabilities (negatives are 25%-sampled).

## 7. Rebuild / extend

- The cache is monthly and resumable; delete a month file to force re-extraction.
- `-label-days 7|30` rebuilds with a different conversion window from the same cache.
- `-neg-rate 1.0` writes all 13.9M rows (~220 MB gz) if you prefer no sampling.
- Re-running after ~mid-July 2026 adds internal-era samples with working task/storage/stream
  features — expected to be the strongest Usage-PQL signals (see research doc).
