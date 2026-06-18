#!/usr/bin/env python3
"""Score the CURRENT active payer base with the trained big-LTV two-part model.

Index = today. For every payer active within 2y, build features with the same
`feature_row()` used in training, predict 12-month forward value (two-part
P(pay)×E(rev|pay) + the single-GBM estimate), assign decile + value tier, and
write ltv_scores_current.csv.gz → ClickHouse ml.ltv_scores.
"""
import functools
from datetime import date

import joblib
import numpy as np
import pandas as pd

import ltv2_dataset as D

print = functools.partial(print, flush=True)
EPOCH = date(1970, 1, 1)
TODAY = (date(2026, 6, 18) - EPOCH).days


def tier(v):
    return ("Platinum" if v >= 200 else "Gold" if v >= 75 else "Silver" if v >= 25
            else "Bronze" if v > 0 else "Prospect")


def main():
    m = joblib.load("ml/ltv_big_model.joblib")
    feats, CAT = m["features"], m["categorical"]
    rev = D.load_revenue()

    rows, uids = [], []
    for u, txns in rev.items():
        fr = D.feature_row(txns, TODAY)
        if fr is not None:
            uids.append(u)
            rows.append(fr)
    X = pd.DataFrame(rows, columns=D.FEATURE_COLS)
    X["last_plan_id"] = X["last_plan_id"].astype(str)
    print(f"active base scored as of {date.fromordinal(TODAY+EPOCH.toordinal())}: {len(X):,} payers")

    Xc = X[feats].copy()
    for c in CAT:
        Xc[c] = Xc[c].astype("category")
    pred_gbm = np.expm1(m["reg"].predict(Xc)).clip(min=0)
    pred_tp = (m["clf"].predict_proba(Xc)[:, 1] * np.expm1(m["reg_pos"].predict(Xc)).clip(min=0))

    out = pd.DataFrame({
        "user_id": uids,
        "pred_ltv_12m": pred_tp.round(2),          # primary (two-part)
        "pred_ltv_gbm": pred_gbm.round(2),
        "recency_days": X.recency_days, "frequency": X.frequency,
        "ltv_to_date": X.monetary_sum, "rev_365_prior": X.rev_365,
        "last_amount": X.last_amount, "is_annual": X.is_annual,
        "country": X.country, "provider": X.provider, "last_plan_id": X.last_plan_id,
    })
    out["value_tier"] = out.ltv_to_date.apply(tier)
    out["ltv_decile"] = pd.qcut(out.pred_ltv_12m.rank(method="first"), 10, labels=False) + 1
    out = out.sort_values("pred_ltv_12m", ascending=False)
    out.to_csv("ltv_scores_current.csv.gz", index=False)

    print(f"\ntotal predicted 12-month value of active base: ${out.pred_ltv_12m.sum():,.0f}")
    print(f"top-10% (decile 10) hold {out[out.ltv_decile==10].pred_ltv_12m.sum()/out.pred_ltv_12m.sum()*100:.0f}% "
          f"of predicted value, n={int((out.ltv_decile==10).sum())}")
    print("\nby value tier:")
    print(out.groupby("value_tier").agg(users=("user_id", "size"),
          avg_pred_ltv=("pred_ltv_12m", "mean"), sum_pred=("pred_ltv_12m", "sum")).round(2).to_string())
    print("\ntop-10 by predicted 12-month LTV:")
    print(out.head(10)[["user_id", "pred_ltv_12m", "ltv_to_date", "recency_days",
                        "frequency", "value_tier", "country"]].to_string(index=False))
    print("\nsaved: ltv_scores_current.csv.gz")


if __name__ == "__main__":
    main()
