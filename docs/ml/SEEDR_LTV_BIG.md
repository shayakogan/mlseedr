# Seedr — Big LTV / CLV: dataset, analysis, model (v2)

2026-06-18. A larger, richer forward-LTV build than `SEEDR_LTV.md` (v1, 167K rows),
with a full EDA (`SEEDR_LTV_EDA.md`) and research-grounded models. Code:
`ml/ltv2_dataset.py`, `ml/ltv2_eda.py`, `ml/ltv2_train.py`.

## 1. Dataset (`train_ltv_big.csv.gz` → `ml.train_ltv_big`)

| Item | Value |
|---|---|
| Rows | **647,475** (vs 167K v1) |
| Distinct users | 23,204 |
| Index dates | **monthly** 2017-01 … 2025-06 (102), over the active payer base (recency ≤ 730d) |
| Label | net revenue in the next 365 days |
| Features | 30: rich RFM + spend dynamics/trend (`rev_90/180/365`, `rev_prev_365`, `rev_trend`), `recency`, `frequency`, gaps, `avg_monthly_rev`, `is_annual`, `n_plans`, `last_plan_id`, `provider`, `country`, refunds |
| Build | from `dataset_cache/revenue_full.tsv` (10y, plan/provider/country/refunds); no new extract; ~23 s |

One user contributes many monthly (user, index) rows → large overlapping panel; evaluate with a **time split** (adjacent indices for a user are autocorrelated).

## 2. Data analysis (full report: `SEEDR_LTV_EDA.md`)

- **Target:** 58% zero, 42% have future revenue; mean $34, median $0; among returners mean $82. Heavy-tailed — **Gini 0.74**, top-10% of rows = 46% of next-year revenue.
- **Recency is THE driver:** P(any revenue next year) decays 90.7% (0–30d) → 48% (31–60d) → 16% (181–365d) → **6.3%** (366–730d).
- **Frequency:** 1 lifetime txn → 16% return / $7; 20+ txns → 76% / $80.
- **Value tiers:** Platinum 75% return / $85 forward; Gold 60% / $50; Silver 42% / $27; Bronze 20% / $9.
- **Monthly vs annual:** annual returns more (49% vs 42%) and is worth more ($41 vs $34) — supports annual-upsell.
- **Geo:** US dominates ($9.9M of forward revenue); emerging markets lower value ($21 vs $35) & retention (36% vs 42%).
- **Strongest predictors (|Spearman| vs label):** `rev_90` 0.76, `txns_90` 0.75, `rev_180` 0.73, `recency` 0.69, `rev_365` 0.68 — recent activity beats annual aggregates.

## 3. Models (test = index ≥ 2024, 180K rows)

| Model | MAE | RMSE | Spearman | capture top-10% | top-20% |
|---|---|---|---|---|---|
| Persistence baseline (`rev_365`) | $21.73 | $47.99 | 0.689 | 37% | 60% |
| GBM log1p (single) | $19.50 | $40.84 | **0.761** | 38% | 61% |
| **Two-part P(pay)×E(rev\|pay)** | **$19.10** | **$37.56** | 0.756 | **39%** | **62%** |

- **The two-part model is best and clearly beats persistence** (MAE −12%, RMSE −22%) — unlike v1 where the gain was marginal; the bigger data + recency-dynamics features (`rev_90/180`, recency, plan, geo) made the difference.
- **Calibration (test total $):** actual $6.73M · two-part **$5.90M** (−12%) · single GBM $4.75M (−30%). Two-part is the better-calibrated $ estimator.
- **Deciles (two-part, test):** monotonic; top decile actual mean **$145 (38.9% of revenue)**, top-3 deciles = 79%. Per-decile pred≈actual (top: $131 vs $145).
- **Top feature:** `recency_days` dominates (perm-importance +0.525), then `last_plan_id`, `rev_180`, `country`, `rev_365`.

Artifacts: `ml/ltv_big_model.joblib` (reg + clf + reg_pos), `ltv_big_test_predictions.csv.gz`,
`ml.train_ltv_big` (dataset) + `ml.ltv_big_test_predictions` (scores) in ClickHouse.

## 4. Research grounding

- Heavy-tailed + zero-inflated revenue → **log1p target** and a **two-part (hurdle) model** — the standard zero-inflated CLV approach; both beat the persistence baseline.
- **Don't resample**; rank metrics (decile capture, Spearman, Gini) over accuracy for value ranking (research §imbalance/calibration).
- GBDT is the right tool on this tabular table (research: GBDT ≈ DL parity; trees fine here).
- RFM dominance + persistence-as-baseline confirmed empirically (EDA §7–8).

## 5. Use & next steps

- **Score the active base** (latest index per user) with the two-part model → rank by predicted 12-month value for: VIP retention, annual-upsell targeting, win-back prioritization (combine with churn risk, `SEEDR_CHURN.md`).
- **Next lifts:** add behavioral features (web/email engagement) for 2025+ cohorts; a 24-month label for longer CLV; per-geo models or PPP pricing analysis (emerging-market value gap is real).

## 6. Reproduce
```bash
.venv/bin/python ml/ltv2_dataset.py   # → train_ltv_big.csv.gz (647K rows)
.venv/bin/python ml/ltv2_eda.py        # → docs/ml/SEEDR_LTV_EDA.md
.venv/bin/python ml/ltv2_train.py      # baseline vs GBM vs two-part + deciles
```
