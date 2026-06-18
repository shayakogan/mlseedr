# Seedr — Unified Retention Priority (LTV × Churn)

2026-06-18. Combines the two models into one actionable list: **who is valuable AND
about to leave**. Code: `ml/churn_score.py` (scores current subscribers),
`ml/ltv2_score.py` (scores current payers); joined in ClickHouse → `ml.retention_priority`.

## Method
- **Value axis:** `ml.ltv_scores.pred_ltv_12m` — predicted next-12-month revenue (two-part CLV model).
- **Risk axis:** `ml.churn_scores.churn_risk_30d` — P(cancel/expire in 30d) from the operational churn GBM, + a hard `had_cancel_sched` flag (already clicked cancel).
- **Join** (INNER, current subscribers who are also payers) → 3,034 users. `expected_loss = value_12m × churn_risk`.
- **Priority quadrant** (the classic value×risk 2×2):

| Priority | Rule | Users | Avg value | Avg risk | $ at risk (30d) |
|---|---|---|---|---|---|
| **P1 urgent_save** | high value **and** high risk / cancel-scheduled | **550** | $52 | 0.197 | **$6,497** |
| P2 high_value_nurture | high value, low risk | 786 | $51 | 0.089 | $3,675 |
| P3 at_risk_lowvalue | high risk, low value | 618 | $8 | 0.028 | $174 |
| P4 monitor | low value, low risk | 1,080 | $8 | 0.042 | $379 |

`SELECT * FROM ml.retention_priority WHERE priority='P1_urgent_save' ORDER BY expected_loss DESC`

## Key results
- **P1+P2 = 1,336 subscribers (44% of base) hold 83% of future value** → concentrate retention spend there.
- **The cancellation_scheduled pool is NOT uniform:** of 888 current soft-cancels, **278 are high-value (P1, $12.9K)** worth fighting for, **582 are low-value (P3, $4.6K)** — let go cheaply. This is the prioritization a flat "soft-cancel segment" misses.
- P1 = 550 users, ~$6.5K revenue at risk in 30 days (~$78K annualized) — the highest-ROI save list.

## Comparison with previous research / deliverables

| Prior work | What it gave | What the unified priority adds |
|---|---|---|
| **LTV research** (`SEEDR_LTV_BIG.md`) | ranked customers by value; $333K active-base value | adds the **risk** axis → don't nurture a high-value user who isn't leaving as if urgent |
| **Churn research** (`SEEDR_CHURN.md`) | risk score (AUC ~0.6; recency/billing-cycle drivers) | adds **value** → stop "saving" low-value churners (P3, 618 users, only $174 at risk) |
| **Marketing segments** (`SEEDR_MARKETING_SEGMENTS.md`) — soft-cancel as "prime retention window" (flat ~1.2K/mo) | identified the trigger | **splits it by value**: 278 fight (P1) vs 582 let-go (P3) — 31% of the pool holds 74% of its value |
| **Win-back churned** (`segments/`, 591 users, $110K) | recovers users who ALREADY left | retention_priority is **pre-churn prevention** on 3,034 current subscribers — cheaper than win-back; the two are sequential funnel stages (prevent → then recover) |
| **Research §value-cohorts / Usage-PQL** | "target high-value, act on intent" | operationalized: Platinum/Gold concentration + cancel-scheduled intent, now scored per user |

## Tables in ClickHouse `ml`
`ltv_scores` (13,576 active payers) · `churn_scores` (3,173 active subscribers) ·
`retention_priority` (3,034 joined) · `churned_winback_30d` (591 already-churned).

## Caveats
- Churn model is modest (AUC ~0.6) and left-censored (subscription.* since 2026-01-12); risk ranking is directional. The `had_cancel_sched` flag is the hard, reliable signal.
- `expected_loss` uses a 30-day churn risk × 12-month value — a prioritization proxy, not a precise $ forecast.
- Caches are from 2026-06-11; for live use, refresh the feature extracts.

## Reproduce
```bash
.venv/bin/python ml/ltv2_score.py     # → ml.ltv_scores
.venv/bin/python ml/churn_score.py    # → ml.churn_scores
# then the CREATE TABLE ml.retention_priority AS SELECT ... JOIN (see this doc / git history)
```
