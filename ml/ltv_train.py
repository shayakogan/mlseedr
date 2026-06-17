#!/usr/bin/env python3
"""Train the forward-LTV (CLV) model on train_ltv.csv.gz.

Target = revenue in the next 365 days (heavy-tailed, zero-inflated). We model
log1p(revenue) with gradient boosting and report on the original $ scale.

The business use is RANKING users by future value, so the key metrics are:
  - Spearman corr (pred vs actual) and a decile chart;
  - revenue-capture: what % of the next year's actual revenue falls in the
    top-10%/20% of users by predicted LTV;
plus MAE/RMSE in $. We compare against a strong PERSISTENCE baseline
(next-year ≈ prior-365-day revenue), which CLV models must beat.
"""
import functools

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

print = functools.partial(print, flush=True)

DATA = "train_ltv.csv.gz"
TARGET = "label_rev_365"
DROP = ["user_id", "index_date", TARGET]


def capture(y_true, score, pct):
    """Share of total actual future revenue captured by the top pct% by score."""
    k = max(int(len(score) * pct / 100), 1)
    top = np.argsort(-score)[:k]
    return y_true[top].sum() / max(y_true.sum(), 1e-9)


def report(tag, y, pred):
    sp = spearmanr(pred, y).correlation
    print(f"[{tag}] MAE ${mean_absolute_error(y,pred):.2f}  RMSE ${mean_squared_error(y,pred)**0.5:.2f}"
          f"  Spearman {sp:.3f}  | revenue captured by top-10% {capture(y,pred,10)*100:.0f}%"
          f"  top-20% {capture(y,pred,20)*100:.0f}%")


def main():
    df = pd.read_csv(DATA)
    df["index_date"] = pd.to_datetime(df["index_date"])
    print(f"loaded {len(df):,} rows · mean future rev ${df[TARGET].mean():.2f} · "
          f"{(df[TARGET]>0).mean()*100:.0f}% have future revenue")

    # chronological split: train index <= 2023, test index >= 2024
    cut = pd.Timestamp("2024-01-01")
    tr, te = df[df.index_date < cut], df[df.index_date >= cut]
    feats = [c for c in df.columns if c not in DROP]
    ytr, yte = tr[TARGET].to_numpy(), te[TARGET].to_numpy()
    print(f"train {len(tr):,} (idx <2024) · test {len(te):,} (idx >=2024) · {len(feats)} feats\n")

    # persistence baseline: predict next-year = prior-365d revenue
    report("baseline: rev_365_prior", yte, te["rev_365_prior"].to_numpy())

    # GBM on log1p target
    gb = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
                                       min_samples_leaf=50, l2_regularization=1.0, random_state=42)
    gb.fit(tr[feats], np.log1p(ytr))
    pred = np.expm1(gb.predict(te[feats])).clip(min=0)
    report("GBM (log1p)", yte, pred)

    try:
        from sklearn.inspection import permutation_importance
        # importance on ranking (use neg-MAE on log scale)
        imp = permutation_importance(
            gb, te[feats], np.log1p(yte), scoring="r2", n_repeats=5, random_state=0, n_jobs=4)
        top = np.argsort(-imp.importances_mean)[:8]
        print("\n  top features:", ", ".join(f"{feats[i]}({imp.importances_mean[i]:+.3f})" for i in top))
    except Exception as e:  # noqa: BLE001
        print("  importance skipped:", e)

    # decile table (test, by predicted)
    q = pd.qcut(pd.Series(pred).rank(method="first"), 10, labels=False)
    dec = pd.DataFrame({"pred": pred, "actual": yte, "dec": q}).groupby("dec").agg(
        n=("actual", "size"), pred_mean=("pred", "mean"), actual_mean=("actual", "mean"),
        actual_sum=("actual", "sum"))
    print("\n  predicted-LTV deciles (test):")
    print(dec.assign(rev_share=lambda d: (d.actual_sum / d.actual_sum.sum() * 100).round(1))
          .round(2).to_string())


if __name__ == "__main__":
    main()
