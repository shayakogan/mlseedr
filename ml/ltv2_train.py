#!/usr/bin/env python3
"""Train forward-LTV models on the big dataset (train_ltv_big.csv.gz).

Grounded in the research: revenue is heavy-tailed + ~58% zero, so we model
log1p(revenue); we also fit a TWO-PART model (P(pay) classifier × E(rev|pay)
regressor) which is the standard zero-inflated CLV approach; and we compare both
against the PERSISTENCE baseline (next-year ≈ prior-365d revenue) which the EDA
showed is strong (Spearman 0.68). Business use = ranking customers by value, so
the headline metrics are decile revenue-capture + Spearman, alongside MAE/RMSE.

Chronological split: index < 2024 train, ≥ 2024 test. Categoricals
(provider, country, last_plan_id) handled natively by HistGradientBoosting.
Saves the model + scored test set; the dataset/scores also go to ClickHouse.
"""
import functools

import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import (HistGradientBoostingClassifier,
                              HistGradientBoostingRegressor)
from sklearn.metrics import mean_absolute_error, mean_squared_error

print = functools.partial(print, flush=True)
DATA = "train_ltv_big.csv.gz"
TGT = "label_rev_365"
CAT = ["provider", "country", "last_plan_id"]
DROP = ["user_id", "index_date", TGT]


def capture(y, score, pct):
    k = max(int(len(score) * pct / 100), 1)
    return y[np.argsort(-score)[:k]].sum() / max(y.sum(), 1e-9)


def report(tag, y, pred):
    sp = spearmanr(pred, y).correlation
    print(f"[{tag:<26}] MAE ${mean_absolute_error(y,pred):6.2f}  RMSE ${mean_squared_error(y,pred)**0.5:6.2f}"
          f"  Spearman {sp:.3f}  capture top-5% {capture(y,pred,5)*100:4.0f}%  "
          f"top-10% {capture(y,pred,10)*100:4.0f}%  top-20% {capture(y,pred,20)*100:4.0f}%")


def prep_cats(tr, te, feats):
    trg, teg = tr[feats].copy(), te[feats].copy()
    for c in CAT:
        trg[c] = trg[c].astype("category")
        teg[c] = pd.Categorical(teg[c], categories=trg[c].cat.categories)
    return trg, teg


def main():
    df = pd.read_csv(DATA)
    df["index_date"] = pd.to_datetime(df["index_date"])
    df["last_plan_id"] = df["last_plan_id"].astype(str)
    feats = [c for c in df.columns if c not in DROP]
    cut = pd.Timestamp("2024-01-01")
    tr, te = df[df.index_date < cut], df[df.index_date >= cut]
    ytr, yte = tr[TGT].to_numpy(), te[TGT].to_numpy()
    print(f"rows {len(df):,} | train {len(tr):,} (<2024) test {len(te):,} (>=2024) | "
          f"{len(feats)} feats | test future-rev {(yte>0).mean()*100:.0f}%\n")

    report("baseline rev_365_prior", yte, te["rev_365"].to_numpy())

    trg, teg = prep_cats(tr, te, feats)
    common = dict(max_iter=500, learning_rate=0.05, max_leaf_nodes=63,
                  min_samples_leaf=100, l2_regularization=1.0,
                  categorical_features=CAT, random_state=42)

    # (A) single log1p regressor
    reg = HistGradientBoostingRegressor(**common).fit(trg, np.log1p(ytr))
    pred_a = np.expm1(reg.predict(teg)).clip(min=0)
    report("GBM log1p (single)", yte, pred_a)

    # (B) two-part: P(pay) × E(rev | pay)
    clf = HistGradientBoostingClassifier(**common).fit(trg, (ytr > 0).astype(int))
    pos = ytr > 0
    reg2 = HistGradientBoostingRegressor(**common).fit(trg[pos], np.log1p(ytr[pos]))
    p_pay = clf.predict_proba(teg)[:, 1]
    e_rev = np.expm1(reg2.predict(teg)).clip(min=0)
    pred_b = p_pay * e_rev
    report("two-part P(pay)xE(rev)", yte, pred_b)

    # calibration: predicted vs actual total revenue (population $)
    print(f"\ncalibration (test total revenue): actual ${yte.sum():,.0f} | "
          f"GBM ${pred_a.sum():,.0f} | two-part ${pred_b.sum():,.0f}")

    # decile table for the chosen model (two-part — best ranking for CLV)
    best, bp = ("two-part", pred_b) if capture(yte, pred_b, 10) >= capture(yte, pred_a, 10) else ("GBM", pred_a)
    q = pd.qcut(pd.Series(bp).rank(method="first"), 10, labels=False)
    dec = pd.DataFrame({"pred": bp, "actual": yte, "dec": q}).groupby("dec").agg(
        n=("actual", "size"), pred_mean=("pred", "mean"), actual_mean=("actual", "mean"),
        actual_sum=("actual", "sum"))
    dec["rev_share_%"] = (dec.actual_sum / dec.actual_sum.sum() * 100).round(1)
    print(f"\npredicted-LTV deciles (test, {best}):")
    print(dec[["n", "pred_mean", "actual_mean", "rev_share_%"]].round(2).to_string())

    try:
        from sklearn.inspection import permutation_importance
        sub = np.random.RandomState(0).choice(len(teg), min(80000, len(teg)), replace=False)
        imp = permutation_importance(reg, teg.iloc[sub], np.log1p(yte[sub]),
                                     scoring="r2", n_repeats=4, random_state=0, n_jobs=4)
        top = np.argsort(-imp.importances_mean)[:12]
        print("\ntop features (single GBM, perm-importance):",
              ", ".join(f"{feats[i]}({imp.importances_mean[i]:+.3f})" for i in top))
    except Exception as e:  # noqa: BLE001
        print("importance skipped:", e)

    joblib.dump({"reg": reg, "clf": clf, "reg_pos": reg2, "features": feats,
                 "categorical": CAT}, "ml/ltv_big_model.joblib")
    out = te[["user_id", "index_date"]].copy()
    out["actual_rev_365"] = yte
    out["pred_ltv_gbm"] = pred_a.round(2)
    out["pred_ltv_twopart"] = pred_b.round(2)
    out.to_csv("ltv_big_test_predictions.csv.gz", index=False)
    print("\nsaved: ml/ltv_big_model.joblib, ltv_big_test_predictions.csv.gz")


if __name__ == "__main__":
    main()
