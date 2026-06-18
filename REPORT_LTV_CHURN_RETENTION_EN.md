# Seedr ML — Customer Value, Churn & Retention Priority
### Report — 2026-06-18

End-to-end work on **predicting customer value (LTV), churn risk, and combining
them into one retention-priority list**, grounded in the verified 106-agent
research. All data lives in ClickHouse `data.seedr.cc` (read everything;
read-write in `ml.*`). Companion docs: `docs/ml/SEEDR_LTV_BIG.md`,
`SEEDR_LTV_EDA.md`, `SEEDR_CHURN.md`, `SEEDR_RETENTION_PRIORITY.md`.

---

## 1. Executive summary

- Built a **large forward-LTV dataset** (647,475 rows, 10 years of revenue history)
  and a **two-part CLV model** that predicts each customer's next-12-month revenue
  and **beats the strong persistence baseline** (MAE −12%, RMSE −22%).
- Scored the **current base**: 13,576 active payers, **$333K predicted 12-month value**;
  the top decile (1,358 users) holds **47%** of it.
- Scored **30-day churn risk** for 3,173 current subscribers and combined value × risk
  into **`ml.retention_priority`** (3,034 users).
- **Actionable output:** 550 high-value-at-risk subscribers (**P1 urgent_save**),
  ~$78K annualized revenue at risk; **44% of subscribers hold 83% of future value** —
  concentrate retention spend there.

---

## 2. LTV dataset (big)

| Item | Value |
|---|---|
| Rows | **647,475** (4× the first version) |
| Distinct users | 23,204 |
| Index dates | monthly, 2017-01 … 2025-06 (102), over the active payer base (recency ≤ 730d) |
| Label | net revenue in the next 365 days |
| Features | 30 — rich RFM + spend dynamics/trend (`rev_90/180/365`, `rev_trend`), recency, frequency, gaps, `avg_monthly_rev`, `is_annual`, `n_plans`, `last_plan_id`, `provider`, `country`, refunds |
| Source | 10y `revenue_facts` (`dataset_cache/revenue_full.tsv`); built in ~23 s, no new extract |
| ClickHouse | `ml.train_ltv_big` |

A user contributes many monthly (user, index) rows → large overlapping panel; we
evaluate with a **time split** because adjacent indices for a user are autocorrelated.

---

## 3. Data analysis (EDA — full detail in `SEEDR_LTV_EDA.md`)

- **Target:** 58% zero, 42% have future revenue; mean $34, median $0. Heavy-tailed
  (**Gini 0.74**; top-10% of rows = 46% of next-year revenue).
- **Recency is THE driver:** P(any revenue next year) decays 90.7% (0–30d) → 16%
  (181–365d) → **6.3%** (366–730d).
- **Frequency:** 1 lifetime txn → 16% return / $7; 20+ txns → 76% / $80.
- **Value tiers:** Platinum 75% return / $85 forward; Gold 60% / $50; Silver 42% / $27;
  Bronze 20% / $9.
- **Monthly vs annual:** annual returns more (49% vs 42%) and is worth more ($41 vs $34)
  → supports annual-upsell.
- **Geo:** US dominates ($9.9M of forward revenue); emerging markets lower value
  ($21 vs $35) and retention (36% vs 42%).
- **Strongest predictors (|Spearman| vs label):** `rev_90` 0.76, `txns_90` 0.75,
  `rev_180` 0.73, `recency` 0.69, `rev_365` 0.68 — recent activity beats annual aggregates.

---

## 4. Models & training details

### 4.1 LTV (test = index ≥ 2024, 180,356 rows; train 467,119, index < 2024)

| Model | MAE | RMSE | Spearman | capture top-10% | top-20% |
|---|---|---|---|---|---|
| Persistence baseline (`rev_365`) | $21.73 | $47.99 | 0.689 | 37% | 60% |
| GBM log1p (single) | $19.50 | $40.84 | **0.761** | 38% | 61% |
| **Two-part P(pay)×E(rev\|pay)** | **$19.10** | **$37.56** | 0.756 | **39%** | **62%** |

- Algorithm: HistGradientBoosting on `log1p(revenue)`; two-part = classifier P(pay) ×
  regressor E(rev|pay) — the standard zero-inflated / heavy-tailed CLV approach.
- **% with future revenue ("conversion"):** 42% overall, 45% on test.
- **Training time:** single GBM 27 s · two-part 60 s · **~88 s total** (CPU).
- Calibration (test total): actual $6.73M · two-part $5.90M (−12%) · single GBM $4.75M (−30%).
- Top feature: `recency_days` dominates; then `last_plan_id`, `rev_180`, `country`, `rev_365`.

### 4.2 Churn (30-day subscriber churn)

| Item | Value |
|---|---|
| Dataset | **8,517 rows** × 34 features (bi-weekly snapshots of active subscribers, reconstructed from the event log) |
| Train / Test (quality eval) | 5,759 / 2,758 (chronological) |
| **% churn ("conversion")** | **20.2%** |
| Scoring model (Model A) | trained on all 8,517 rows; **2.8 s** |
| Quality | ROC-AUC ~0.60 (pre-emptive variant 0.65); drivers: recency, billing-cycle length, country |
| ClickHouse | `ml.churn_scores` (3,173 current subscribers) |

Caveat: subscription events exist only since 2026-01-12 → **left-censored**; risk
ranking is directional. `had_cancel_sched` (already clicked cancel) is the hard signal.

---

## 5. Scoring the current base

- **LTV:** 13,576 active payers → `ml.ltv_scores`. Total predicted 12-month value
  **$333,027**; top decile (1,358) = 47%; Platinum 2,968 users = $195K (59%).
- **Churn:** 3,173 active subscribers → `ml.churn_scores`; 888 with `cancellation_scheduled`.

---

## 6. Unified retention priority (`ml.retention_priority`, 3,034 users)

`expected_loss = pred_ltv_12m × churn_risk_30d`. Value × risk quadrant:

| Priority | Users | Avg value | Avg risk | $ at risk (30d) |
|---|---|---|---|---|
| **P1 urgent_save** (high value + high risk / cancel-scheduled) | **550** | $52 | 0.197 | **$6,497** |
| P2 high_value_nurture | 786 | $51 | 0.089 | $3,675 |
| P3 at_risk_lowvalue | 618 | $8 | 0.028 | $174 |
| P4 monitor | 1,080 | $8 | 0.042 | $379 |

- **P1+P2 = 1,336 subscribers (44%) hold 83% of future value.**
- The `cancellation_scheduled` pool is **not uniform**: of 888, **278 are high-value (P1,
  $12.9K)** worth fighting for, **582 are low-value (P3, $4.6K)** — let go cheaply.

---

## 7. Comparison with previous research

| Prior work | What it gave | What this adds |
|---|---|---|
| LTV research | ranked by value | + risk axis → don't treat a non-leaving high-value user as urgent |
| Churn research | risk score (AUC ~0.6) | + value → stop "saving" low-value churners (P3: $174 at risk) |
| Marketing segments (soft-cancel, flat) | the trigger | splits it by value: 278 fight vs 582 let-go |
| Win-back churned (591, $110K) | recovers users who ALREADY left | this is pre-churn **prevention** (cheaper); sequential funnel stage |
| 106-agent research (value cohorts, Usage-PQL) | "target high-value, act on intent" | operationalized per user |

Methodology consistent with the research: heavy-tailed → log target + two-part; **no
resampling**; ranking metrics (decile capture, Spearman, Gini) over accuracy; GBDT
(GBDT ≈ DL parity on tabular).

---

## 8. Where everything lives

**ClickHouse `ml`:** `train_ltv_big` (647K) · `ltv_big_test_predictions` (180K) ·
`ltv_scores` (13,576) · `churn_scores` (3,173) · `retention_priority` (3,034) ·
`train_churn` (8,517) · `train_email_conversion` (7.17M) · `churned_winback_30d` (591).

**Code (GitHub `mlseedr` + stat2):** `ml/ltv2_dataset.py`, `ltv2_eda.py`, `ltv2_train.py`,
`ltv2_score.py`, `churn_dataset.py`, `churn_train.py`, `churn_score.py`, `load_to_ch.py`.

**Docs:** `docs/ml/` (LTV/EDA/churn/retention/research/learning guide), `docs/seedr/`
(warehouse reference).

---

## 9. Caveats & next steps

- Churn model modest (AUC ~0.6) + left-censored; `expected_loss` is a prioritization
  proxy (30-day risk × 12-month value), not a precise $ forecast.
- Feature caches are from 2026-06-11; refresh before live use.
- Next: add behavioral features to LTV for 2025+ cohorts; richer churn features
  (billing_plan_id / next-renewal date) to lift AUC to 0.7+; randomized email holdout
  for true uplift; per-geo / PPP pricing analysis.
