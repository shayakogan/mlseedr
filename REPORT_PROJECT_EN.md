# Seedr — Marketing ML & Retention Engine — Project Report

Date: 2026-06-22. Full account of the work: why, what we built, new features,
business value, architecture, how the metrics improved, and what remains.
All data in ClickHouse `ml.*`; code + docs in GitHub `mlseedr`; files mirrored to
`stat2.seedr.cc:/root/mlseedr`.

---

## 1. Why we did this
Seedr is a freemium cloud-torrent / download / streaming service: ~70K weekly-active
users but only ~3.2–3.5K paying subscribers and a ~0.06% free→paid email-conversion
rate. Marketing was broad and untargeted. **Goal:** turn the ClickHouse telemetry into a
data-driven marketing engine — know *who* will convert, churn, or is valuable, and *act*
(upsell / retention / win-back) on the right users with the right message.

---

## 2. What we built (deliverables)
- **Verified research** (106-agent runs): freemium segmentation + which signals predict
  conversion — used to ground every model and segment.
- **8 marketing segments** (win-back, soft-cancel, storage-pressure, heavy-bandwidth,
  streamers, cart-abandon, dormant payers, monthly-loyal) — sized live, exported.
- **3 core models** (datasets + training):
  - Conversion ("pays in 14d after an email"), 7.17M rows.
  - Churn ("active subscriber cancels in 30d"), 17.5K rows.
  - LTV / CLV ("revenue in next 365d"), 647K rows.
- **Retention priority** = value × risk → `ml.retention_priority`.
- **Win-back** of the 591 already-churned (30d), persona-grouped.
- **Content-affinity** — a brand-new data source (Seedr admin FS API): per-user storage/
  file metadata → personas, 42 features → `ml.user_content`.
- **Edge/QoS, task-usage, billing-health** feature tables from ClickHouse.
- **Customer-360** — one row per user unifying everything + a `next_best_action`.
- **Live snapshots** (`customer_360_history`), **uplift-holdout** framework, **campaign
  audiences**.

---

## 3. New features collected (the signal that didn't exist before)
| Group | Source | Examples |
|---|---|---|
| Intent | telemetry | `promo_sub` (visited-subscription, ×7 raw lift), pricing/goal funnel |
| Billing timing | Partytime metadata | **`expires_on`** (exact renewal date), `has_payment_method` (120/120 expired had none) |
| Content affinity | **FS API (new)** | `content_persona` (video_streamer 60% / empty 24% / …), storage_gb, share_video/audio/ebook, `n_lost_files`, `files_added_30d`, `saw_walkthrough`, `last_signin_day`, acquisition flags |
| Edge / QoS | request_events | **`n_rate_limited`** (429 quota-pressure, 14.5K users), **`n_stall`** (QoE, 58K), streaming volume, bandwidth |
| Usage | task.* | downloads_30d, **task_failure_rate** (frustration) |
| Billing health | payments DB | `in_grace_period` (dunning), `reconciliation_critical` |
| Engagement dynamics | derived | recency/frequency/monetary (RFM), spend trend, session/activity trends |

---

## 4. How it helps the business
- **Targeted campaigns instead of blasts:** `next_best_action` per user —
  HD-upsell (1.1K active streamers hitting 429/stalls), reactivate-empty (3.0K paid-but-
  empty), fix-payment (388 billing failures), VIP-nurture (1.1K, $113 LTV), urgent-save (71).
- **Spend where the money is:** 44% of payers hold **83%** of future value → focus there.
- **Win-back** of $110K lifetime revenue already churned, persona-personalised.
- **Catch involuntary churn cheaply:** 220 users near-certain to lapse for lack of a payment
  method → "update your card", no discount.
- **Measure causality, not correlation:** randomized holdout → true email uplift.

---

## 5. Architecture
```
SOURCES                         FEATURE TABLES (ml.*)              MODELS / SCORES            ACTION
ClickHouse seedr_telemetry ─┐   user_content (content)            conversion (LogReg/NN)     customer_360
  (web/email/subs/tasks)    ├─► user_edge   (edge/QoS)      ┐     churn (GBM)          ┐     + next_best_action
revenue_facts (10y)         ├─► user_tasks  (usage)         ├──► LTV two-part (P×E)     ├──► campaign audiences
payments DB (Partytime)     ├─► user_billing_health         │     retention_priority    │     + holdout (uplift)
Seedr FS API (NEW)          ┘   ltv_scores / churn_scores   ┘     (value × risk)        ┘     customer_360_history
                                                                                              (daily live snapshots)
```
- **Models:** gradient-boosted trees (HistGradientBoosting) everywhere; LTV is a two-part
  hurdle (P(pay) × E(revenue)); a multi-task neural backbone + swappable heads as an option.
- **Identity:** join on `user_id`. **Storage:** all in ClickHouse `ml.*` (read-write granted);
  PII never in git. **Refresh:** `snapshot_daily.sh` rebuilds CH features + appends a dated
  `customer_360` snapshot.

---

## 6. How the metrics improved
| Model | Metric | Result |
|---|---|---|
| **Conversion** | ROC-AUC / lift@1% | **0.95 / ×65** — top-1% by score convert at ~3.7% vs 0.056% base |
| **Churn** | ROC-AUC | **0.60 → 0.78** after adding billing-cycle features; top-10% precision **67%**, lift ×3.8 |
| **LTV** | P(pay) AUC; ranking | AUC **0.91**; two-part beats persistence baseline (MAE −12%, RMSE −22%); top-10% captures **39%** of next-year revenue |
| **Retention priority** | concentration | P1+P2 = 44% of payers hold **83%** of future value |
| **Content lift** | churn / LTV | +0.01 AUC / +0.016 Spearman (modest on retro models; real lift expected on live snapshots) |
| **Calibration** | ECE | 0.174 → **0.0004** (isotonic) — probabilities usable for €-decisions |

Honest notes: conversion is at a ~0.95 ceiling (dominated by past-payment behaviour);
churn's exact-renewal signal (`expires_on`) is June-2026+ only, so it sharpens *current*
scoring but can't lift the *trained* AUC until we retrain on accrued data; content's retro
lift is small because it's a now-snapshot vs historical labels.

---

## 7. What remains
1. **Run a campaign on `arm='treatment'`** (holdout already assigned) → measure the first
   real **uplift** — the one causal answer still open.
2. **Retrain churn/LTV on ~30 days of live snapshots** (`customer_360_history`) → real
   (not retro) lift from content/edge/billing features; churn AUC expected ~0.85.
3. **storage_used_pct** — needs the storage quota (from `billing_plan_id`→tier or
   `/dynamic/get_space`); the one missing content gap.
4. **Schedule** `snapshot_daily.sh` on a host with a persistent tunnel (autossh) — crontab
   wasn't available in the working env.
5. **`billing_plan_id` → tier/cycle map** (esp. the 1000-series) from the billing team.
6. Optional: per-geo / PPP pricing analysis; content-aware creative; bandit/RL targeting
   once uplift data exists.

---

## 8. Where everything lives
- **ClickHouse `ml`:** customer_360 (+history), retention_priority, ltv_scores, churn_scores,
  user_content, user_edge, user_tasks, user_billing_health, campaign_holdout, churned_winback_30d,
  train_* datasets.
- **GitHub `mlseedr`:** all code + docs (datasets/PII git-ignored).
- **stat2.seedr.cc:/root/mlseedr:** full project incl. data files.
- **Docs:** `docs/ml/` (LTV, churn, retention, content, feature catalog, customer-360,
  research, learning guide) + `docs/seedr/` (warehouse reference).
