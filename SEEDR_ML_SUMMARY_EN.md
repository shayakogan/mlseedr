# Seedr ML — Summary Report (datasets, segments, learning)

A concise summary of the 2026-06 work: what we collected, what we trained, what to do next.
Details: `SEEDR_ML_LEARNING_GUIDE.md` (learning methods), `SEEDR_ML_RESEARCH.md`
(final model report), `SEEDR_ML_DATASET.md` (data dictionary),
`SEEDR_MARKETING_SEGMENTS.md` (segments). Data source: ClickHouse `data.seedr.cc`.

---

## 1. Datasets collected

| File | Size | Rows / cols | Purpose |
|---|---|---|---|
| `train_email_conversion.csv.gz` | 55 MB | 3,529,143 × 61 | Main training set: "conversion after a marketing email". 1 row = (user_id, send day) |
| `train_uplift.csv.gz` | 58 MB | 3.60M treatment + 76K control × 62 | Same set + control rows (active no-email days) + a `treatment` column for uplift |
| `dataset_cache/` | 752 MB | ~28M TSV rows (40 files) | Cache of monthly ClickHouse extracts (email/web/task/revenue/subs/profile). Reused on rebuild |
| `ml/segments/*.csv.gz` | ~3 MB | 6 files | Per-segment training sets (one per segment, own target + train/test split) |
| `segments_out/2026-06-11/*.csv` | ~1 MB | 8 files | Operational per-segment user_id lists for campaigns |
| `ml/model_conv14.joblib` | 2.3 MB | — | Saved models (LogReg + GBM) |
| `ml/nn/*.pt` | ~0.1 MB each | 15 files | NN backbone + swappable heads + calibrator |

**Candidates in full population:** ~13.9M (after 25% negative downsampling → 3.53M rows).
**Window:** sends 2025-06-26 → 2026-05-27 (effectively the mautic era).
**Split:** chronological 80/20 (train ≤ 2026-03-24), leakage-free.

### Main dataset columns (61, by group)
- **Identifiers/context:** `user_id`, `send_date`, `era`, `sends_today`
- **Email engagement:** `em_sent_7/30/90`, `em_opened_30/90`, `em_clicked_90`, `em_open_rate_90`, `days_since_last_send/open`
- **Web behavior:** `web_active_days_7/30`, `pageviews_7/30`, `file_dl_30/90`, `archive_dl_30/90`, `file_views_30`, `streams_30/90`, `pricing_views_30/90`, `goal4_30/90`, `days_since_web_activity`
- **Tasks/quota:** `tasks_completed_30`, `tasks_failed_30`, `storage_warnings_30`, `tasks_observable`
- **Monetary:** `ever_paid`, `ltv_before_usd`, `txns_before`, `days_since_last_txn`, `last_txn_amount`, `txns_365d`, `premium_at_send`
- **Subscription:** `last_sub_event`, `days_since_sub_event`, `subs_observable`
- **Profile:** `country`, `tenure_days`, `devices`, `mobile_share_now`
- **8 segment flags** (`seg_*`)
- **Labels:** `label_payment_14d`, `label_conv_14d` (primary), `label_sub_started_14d`, `days_to_payment`, `first_payment_usd`, `sample_weight`
- All features computed strictly BEFORE the send day (no leakage).

---

## 2. Segments (8 marketing)

| Segment | Size (operational) | Conversion (after email) | Open rate | Purpose |
|---|---|---|---|---|
| winback (lapsed payers, active) | 4,571 | **3.7%** | 34% | win-back, LTV-personalized offer |
| dormant payers (paid ≤180d, left) | 1,674 | 3.2% | 37% | win-back with incentive |
| soft-cancel (cancellation scheduled) | 847 | 1.3% | 34% | immediate save campaign |
| storage-warning (hit quota) | 995/mo | — | 35% (click 2.2%) | tier upsell |
| heavy bandwidth free (>10GB/wk) | 8,899 | 0.4% | 16% | free→paid conversion |
| free streamers (480p-gated) | 15,393 | — | 16% | "watch in HD" upsell |
| cart abandoners (funnel, no pay) | 682 | 0.4% | 26% | abandonment drip |
| monthly loyal (3+ payments/yr) | 1,413 | — (renewal) | 38% | switch to annual plan |

- Baseline conversion across the base: **0.056%** (label_conv_14d). Segments give ×7–66 over base.
- Opening an email is ~300–600× more common than paying (open 16–38% vs conv 0.06–3.7%).
- ⚠️ storage_warning and streamer are empty in the ML window (events start after the migration; available ~mid-July 2026).

---

## 3. Learning methods we experimented with

| Approach | Result (test) | Takeaway |
|---|---|---|
| **LogReg** (baseline) | AUC **0.953**, lift@1% ×65 | Strong, cheap, near-ceiling |
| **HistGBM** | AUC 0.756 | Worse than linear (signal ~linear after log) |
| **NN backbone + global head** | AUC 0.962 | ≈LogReg + gives modularity |
| **NN per-segment heads** (frozen backbone) | worse than global | Segments too small; specialization didn't help |
| **NN multi-task** (conv+renewal) | conv **0.962** / renewal **0.991** | Best architecture: shared backbone + heads; renewal 0.51→0.99 |
| **NN PLR** (periodic embeddings) | conv 0.951 | No AUC gain on our data |
| **Calibration** (isotonic) | ECE 0.174 → **0.0004** | The key practical win — probabilities now usable |
| **Uplift** (naive / S-learner / T-learner) | inconclusive | Email's causal effect not identifiable from observational data — needs a holdout |

Quality bottom line: model's top-1% → conversion **~3.7%** vs 0.056% base (lift ×67).

---

## 4. Recommendations for further work

**Ship now:**
1. **Multi-task NN backbone + isotonic calibration** (`ml/nn/backbone_mt.pt` + heads + `calibrator_conv.joblib`). Fallback alternative — LogReg (0.953, cheaper).
2. Pre-campaign scoring: send to top-k by score, not everyone.

**Highest value next (prioritized):**
1. **Randomized 10% holdout** per campaign → true uplift (the only way to answer "does the email cause payment"). Start with large segments.
2. **Internal-era features** (task/storage/stream) after ~mid-July 2026 — strongest Usage-PQL signals, currently empty.
3. To push accuracy: **TabM** or focal loss + logit adjustment (small gain — linear is already near ceiling).
4. **CGC/MMoE gating** if expanding to many per-segment heads (guards against negative transfer).

**Avoid:** resampling/SMOTE (ruins calibration); trusting observational uplift; one separate model per tiny segment.

*Generated 2026-06-14 from our experiments + a verified 106-agent research run.*
