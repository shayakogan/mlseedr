# Seedr — Churn Prediction (subscriber retention)

Research thread started 2026-06-17. Goal: predict whether an **active subscriber**
cancels/expires within 30 days, to prioritise retention outreach. Code:
`ml/churn_dataset.py` (build), `ml/churn_train.py` (train). Companion:
`SEEDR_ML_RESEARCH.md`, `SEEDR_ML_DATASET.md`.

## 1. Problem & data

- **Population:** active premium subscribers, **reconstructed from the event log**
  (`user_subscription_state` is churn-blind / has no history). Active at snapshot S =
  latest `subscription.*` event before S ∈ {created, reactivated, billing_plan_change,
  cancellation_scheduled}, and a real created/reactivated exists.
- **Label:** `subscription.canceled` OR `subscription.expired` in (S, S+30].
- **Sampling:** 8 bi-weekly snapshots 2026-02-01 … 2026-05-10 (each with a full 30d
  forward window). Chronological split: train Feb–Apr12, test Apr26 + May10.
- **Dataset:** `train_churn.csv.gz` — 8,517 rows, **20.2% churn** (balanced, unlike the
  0.06% conversion task). Built from the local cache (subscriber-filtered).

## 2. Headline results (test, GBM)

| Model | ROC-AUC | top-10% precision | lift@10% | Brier / ECE |
|---|---|---|---|---|
| **A — full population** (operational score) | 0.605 | 24% | ×2.1 | 0.104 / 0.059 |
| **B — pre-emptive** (before the cancel click) | **0.649** | **28%** | **×2.4** | 0.101 / 0.047 |

LogReg is ~random here (0.48–0.52) — the signal is non-linear. Top features:
**country**, **prior_txn_gap_median** (billing-cycle length), **days_since_sub_event**,
tenure, pageviews_30, prior_cancels, last_txn_amount, web activity recency.

**Model B is the useful one:** among subscribers who have NOT yet signalled cancel,
targeting the top-10% by risk catches 28% of churners vs an 11.7% base — a ×2.4 lift
for proactive retention. (Users who already clicked cancel — `had_cancel_sched_30=1` —
churn 55% and don't need a model; route them straight to the soft-cancel save flow.)

## 3. What we learned / why it's only modest

1. **Churn is intrinsically harder than conversion here.** Everyone in the population
   already pays, so the dominant "ever-paid" signal that made conversion AUC 0.96 is
   gone; we're separating leavers among payers — genuinely hard.
2. **Billing-cycle position is the key signal** — adding `prior_txn_gap_median` (term,
   inferred from txn cadence) lifted AUC 0.58→0.60 (A) and 0.60→0.65 (B). The model
   still lacks the *exact next-renewal date* and *plan term* (no `billing_plan_id` in
   the cache) — the biggest remaining lever.
3. **Left-censoring caps coverage.** `subscription.*` only since 2026-01-12, so subs
   created earlier are invisible until their next event — we reconstruct only ~1,400 of
   ~3,467 active subs at recent snapshots, and early snapshots are biased to high-churn
   recent cohorts (churn 30%+ in Feb → ~11% by Apr/May). `snapshot_age` is included so
   the model can absorb this drift.
4. `expired` only since 2026-06-07; task/storage/stream features are 0 before late May
   (dropped). So churn features are email + web + monetary + subscription-state only.

## 4. Recommendations / next steps (ranked)

1. **Re-extract subscription state WITH `billing_plan_id` + next-renewal date** (from
   `revenue_facts.billing_plan_id` / payments DB). Exact cycle position is the single
   biggest expected AUC gain — churn models with billing features typically reach 0.70–0.80.
2. **Operationalise Model B now anyway:** ×2.4 lift is useful. Route `had_cancel_sched`
   users to the save flow directly; score the rest weekly for proactive outreach.
3. **Let history accrue:** with only ~5 months and left-censoring, the model will improve
   substantially as more subscription history (and uncensored tenure) builds up.
4. **Add as a 3rd head** to the multi-task backbone (conv / renewal / churn) once the
   feature set is richer, so retention and conversion share one representation.

## 5. Reproduce

```bash
.venv/bin/python ml/churn_dataset.py   # → train_churn.csv.gz (8.5K rows)
.venv/bin/python ml/churn_train.py      # Model A + Model B, metrics + importances
```
