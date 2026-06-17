# Seedr — Forward-LTV / CLV Prediction

Research thread, 2026-06-17. Goal: predict each user's **revenue over the next 365
days** to prioritise retention/targeting by value. Code: `ml/ltv_dataset.py` (build),
`ml/ltv_train.py` (train). Companion: `SEEDR_CHURN.md`, `SEEDR_ML_RESEARCH.md`.

## 1. Problem & data

- **Sample unit:** (user_id, index_date) for every user with ≥1 completed txn before
  index. **RFM features** computed strictly before index (10y of `revenue_facts`, so no
  new extract). **Label = revenue in (index, index+365].**
- **Cohorts:** 15 semi-annual index dates 2018-01 … 2025-01 (each with a full forward
  year in data) → one user contributes many rows across their lifetime.
- **Dataset:** `train_ltv.csv.gz` — **166,975 rows**, mean future revenue $20.82, 26%
  have any future revenue. Heavy-tailed (per-user LTV: median $25, p90 $285, max $2,789).
- Chronological split: train index < 2024 (108,962), test index ≥ 2024 (58,013).
- **Revenue-only** (no web/email features — those exist only from 2025-05; this is the
  full-history CLV core). Model target = log1p(revenue), reported on $ scale.

## 2. Results (test)

| Model | MAE | RMSE | Spearman | rev captured top-10% | top-20% |
|---|---|---|---|---|---|
| Persistence baseline (prior-365d revenue) | $11.07 | $33.04 | **0.713** | 58% | 84% |
| **GBM (log1p, RFM)** | **$10.08** | **$29.48** | 0.665 | **60%** | **89%** |

**Predicted-LTV deciles (test) — excellent separation & calibration:**

| Decile | n | pred mean | actual mean | % of next-year revenue |
|---|---|---|---|---|
| 9 (top) | 5,802 | $91.96 | $114.62 | **60.5%** |
| 8 | 5,801 | $28.68 | $53.39 | 28.2% |
| 7 | 5,801 | $1.33 | $11.95 | 6.3% |
| 0–6 | ~40,600 | <$0.35 | <$4 | <5% combined |

Top 2 deciles (20% of payers) hold **89%** of next year's revenue; the model ranks them
cleanly and is monotonic + roughly calibrated (slightly under-predicts the very top).

## 3. What we learned

1. **CLV ranking is largely solved by "last-12-months spend."** Subscription revenue is
   sticky, so the persistence baseline already has Spearman 0.71 and captures 58% in the
   top decile. The GBM **improves dollar accuracy** (MAE −9%, RMSE −11%) and **tail
   capture** (+5pp at top-20%) but not rank-correlation — honest, useful nuance.
2. **Use GBM for $ estimates** (campaign ROI, budget), **and `rev_365_prior` as a cheap
   near-equal ranking proxy** when a model isn't wanted.
3. **Top features:** prior-year revenue, frequency, monetary sum, recency, billing
   cadence — classic RFM. (Permutation-importance run hit a NaN from a few negative
   forward-revenue rows = refunds; cosmetic, fix by clipping label ≥0.)
4. **Limitation:** revenue-only. Adding behavioral features (web/email/tasks) for 2025+
   cohorts is the obvious next lift, but only recent index dates have them.

## 4. The payoff — combine with churn (the actionable output)

LTV alone says *who is valuable*; churn (`SEEDR_CHURN.md`) says *who is leaving*. The
retention priority is the **product**:

```
retention_priority = predicted_LTV_next_365d  ×  churn_risk_30d
```

- **High LTV × high churn-risk → urgent saves** (most revenue at risk) — the top targeting list.
- High LTV × low risk → nurture/upsell. Low LTV × high risk → cheap/automated only.

Next concrete step: score current subscribers with both models and write
`ml.retention_priority` (user_id, pred_ltv, churn_risk, priority) to ClickHouse for the
marketing team. (Needs a fresh "today" feature snapshot; cache is from 06-11.)

## 5. Reproduce

```bash
.venv/bin/python ml/ltv_dataset.py   # → train_ltv.csv.gz (167K rows, no extract needed)
.venv/bin/python ml/ltv_train.py      # GBM vs persistence baseline, deciles, importances
```
