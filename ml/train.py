#!/usr/bin/env python3
"""Train "conversion after a marketing email" models on the Seedr dataset.

Pipeline (see SEEDR_ML_DATASET.md for the data dictionary):
  1. Clean:      -1 sentinels -> NaN (+ missing flags), empty categoricals ->
                 named level, drop documented-leak and zero-variance columns.
  2. Normalize:  log1p on heavy-tailed counts + StandardScaler
                 (for the linear baseline; trees are scale-invariant).
  3. Split:      80/20 CHRONOLOGICAL by send_date. A random split would leak
                 user-level future information across folds.
  4. Train:      LogisticRegression baseline (on a 1M subsample) +
                 HistGradientBoosting on the full train split.
  5. Evaluate:   weighted ROC-AUC / PR-AUC (sample_weight restores the true
                 population after 25% negative downsampling) + lift@k%,
                 which is the metric marketing actually acts on.

Target: label_conv_14d (payment within 14d by a non-premium user).

The box has ~5GB free RAM, so dtypes are pinned to float32/int8 at read time
and intermediate frames are freed eagerly.
"""

import gc
import sys
import time

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

DATA = "train_email_conversion.csv.gz"
TARGET = "label_conv_14d"

# Documented mild leak (measured "now", not at send time) + alternative
# labels / post-outcome columns that must never be features. `era` is
# constant in the current window.
DROP_AT_READ = [
    "mobile_share_now", "era",
    "label_payment_14d", "label_sub_started_14d",
    "days_to_payment", "first_payment_usd",
]
SENTINEL_COLS = [  # -1 means "never happened" -> NaN + missing indicator
    "days_since_last_send", "days_since_last_open", "days_since_web_activity",
    "days_since_last_txn", "days_since_sub_event", "tenure_days", "days_since_promo_sub",
]
CATEGORICAL = ["country", "last_sub_event"]
TOP_COUNTRIES = 30
LOGREG_SAMPLE = 1_000_000


def load() -> pd.DataFrame:
    head = pd.read_csv(DATA, nrows=0).columns
    usecols = [c for c in head if c not in DROP_AT_READ]
    dtypes = {}
    for c in usecols:
        if c in ("country", "last_sub_event"):
            dtypes[c] = "category"   # big memory save vs object strings at 7M rows
        elif c == "send_date":
            dtypes[c] = "string"
        elif c == "user_id":
            dtypes[c] = "int64"
        elif c == TARGET or c.startswith("seg_") or c.endswith("_observable") \
                or c in ("ever_paid", "premium_at_send"):
            dtypes[c] = "int8"
        else:
            dtypes[c] = "float32"
    t0 = time.time()
    # low_memory=False: single-block parse so category dtype doesn't hit the
    # per-chunk union_categoricals "dtype of categories must be the same" error.
    df = pd.read_csv(DATA, usecols=usecols, dtype=dtypes, low_memory=False)
    print(f"loaded {len(df):,} rows x {df.shape[1]} cols in {time.time()-t0:.0f}s "
          f"({df.memory_usage(deep=True).sum()/1e9:.2f} GB)")
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    for c in SENTINEL_COLS:
        miss = df[c] < 0
        df[c] = df[c].where(~miss)
        df[f"{c}_missing"] = miss.astype("int8")

    # category-safe cleaning (cols are read as category for memory): go via
    # string for the fillna/top-N collapse, then recompress to category.
    cs = df["country"].astype("string").fillna("unknown").replace("", "unknown")
    top = cs.value_counts().head(TOP_COUNTRIES).index
    df["country"] = cs.where(cs.isin(top), "other").astype("category")
    df["last_sub_event"] = (df["last_sub_event"].astype("string")
                            .fillna("none").replace("", "none").astype("category"))

    # Drop zero-variance columns (streams/task/storage cols in this window, ...)
    const = [c for c in df.columns
             if c != "send_date" and df[c].nunique(dropna=False) <= 1]
    if const:
        print("dropping constant columns:", ", ".join(sorted(const)))
        df = df.drop(columns=const)
    return df


def time_split(df: pd.DataFrame, frac=0.8):
    cutoff = df["send_date"].quantile(frac, interpolation="lower")
    mask = df["send_date"] <= cutoff
    train, test = df[mask], df[~mask]
    print(f"time split at {cutoff}: train {len(train):,} "
          f"({train['send_date'].min()}..{cutoff}), test {len(test):,} "
          f"(..{test['send_date'].max()})")
    return train, test


def signed_log1p(x):
    """log1p that tolerates negatives (refund transactions make
    last_txn_amount / ltv_before_usd negative for a few users)."""
    return np.sign(x) * np.log1p(np.abs(x))


def lift_at(y, score, weight, pct) -> float:
    """Conversion rate in the top pct% by score vs the base rate (weighted)."""
    order = np.argsort(-score)
    w = np.asarray(weight)[order]
    yv = np.asarray(y)[order]
    cum_w = np.cumsum(w)
    k = np.searchsorted(cum_w, cum_w[-1] * pct / 100.0)
    top = slice(0, max(k, 1))
    top_rate = np.average(yv[top], weights=w[top])
    base = np.average(yv, weights=w)
    return top_rate / base


def evaluate(name, y, score, weight):
    auc = roc_auc_score(y, score, sample_weight=weight)
    ap = average_precision_score(y, score, sample_weight=weight)
    base = np.average(y, weights=weight)
    print(f"\n[{name}]")
    print(f"  ROC-AUC (weighted):  {auc:.4f}")
    print(f"  PR-AUC  (weighted):  {ap:.4f}  (base rate {base*100:.4f}%)")
    for pct in (1, 5, 10):
        print(f"  lift@top-{pct:>2}%:        x{lift_at(y, score, weight, pct):.1f}")
    return auc, ap


CAP = 4_000_000  # memory cap for training (the full dataset stays on disk / in CH)


def main():
    df = clean(load())
    # Bigger datasets live on disk/ClickHouse; for the in-RAM model we cap to CAP
    # rows = ALL positives + sampled negatives, scaling negative weights so the
    # population is still represented (positives are the scarce, valuable rows).
    if len(df) > CAP:
        pos = df[df[TARGET] == 1]
        neg = df[df[TARGET] == 0]
        keep = CAP - len(pos)
        frac = keep / len(neg)
        neg = neg.sample(n=keep, random_state=42).copy()
        neg["sample_weight"] = neg["sample_weight"] / frac
        df = pd.concat([pos, neg]).reset_index(drop=True)
        del pos, neg
        gc.collect()
        print(f"capped to {len(df):,} rows for training (all {int(df[TARGET].sum()):,} positives "
              f"+ {keep:,} negatives, weights rescaled ×{1/frac:.2f})")
    train, test = time_split(df)
    del df
    gc.collect()

    drop = ["send_date", "user_id", TARGET, "sample_weight"]
    y_tr = train[TARGET].to_numpy()
    y_te = test[TARGET].to_numpy()
    w_tr = train["sample_weight"].to_numpy(dtype="float64")
    w_te = test["sample_weight"].to_numpy(dtype="float64")
    x_tr = train.drop(columns=drop)
    x_te = test.drop(columns=drop)
    test_ids = test[["user_id", "send_date"]].copy()
    del train, test
    gc.collect()

    num_cols = [c for c in x_tr.columns if c not in CATEGORICAL]
    print(f"features: {len(num_cols)} numeric + {len(CATEGORICAL)} categorical | "
          f"train positives {int(y_tr.sum()):,} | test positives {int(y_te.sum()):,}")

    # --- baseline: logistic regression (log1p + scaling) on a subsample ---
    rng = np.random.RandomState(42)
    idx = rng.choice(len(x_tr), size=min(LOGREG_SAMPLE, len(x_tr)), replace=False)
    prep = ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("log", FunctionTransformer(signed_log1p)),
            ("scale", StandardScaler()),
        ]), num_cols),
        ("cat", OneHotEncoder(handle_unknown="ignore", dtype=np.float32), CATEGORICAL),
    ], sparse_threshold=0.0)
    logreg = Pipeline([("prep", prep),
                       ("clf", LogisticRegression(max_iter=2000, C=1.0))])
    t0 = time.time()
    logreg.fit(x_tr.iloc[idx], y_tr[idx], clf__sample_weight=w_tr[idx])
    print(f"\nlogistic regression trained on {len(idx):,} rows in {time.time()-t0:.0f}s")
    evaluate("LogisticRegression / test", y_te, logreg.predict_proba(x_te)[:, 1], w_te)
    gc.collect()

    # --- main model: gradient boosting (handles NaN + categories natively) ---
    # NB: early stopping on log-loss is useless at a 0.06% base rate — it
    # halts after ~10 iterations with a hopelessly undertrained model
    # (AUC 0.68 vs 0.95 for the linear baseline). Fixed iteration count.
    gb = HistGradientBoostingClassifier(
        max_iter=600, learning_rate=0.2, max_leaf_nodes=31,
        min_samples_leaf=50, l2_regularization=10.0,
        categorical_features=CATEGORICAL,
        early_stopping=False,
        random_state=42)
    t0 = time.time()
    gb.fit(x_tr, y_tr, sample_weight=w_tr)
    print(f"\ngradient boosting trained in {time.time()-t0:.0f}s ({gb.n_iter_} iters)")
    sub_tr = rng.choice(len(x_tr), size=min(400_000, len(x_tr)), replace=False)
    auc_tr = roc_auc_score(y_tr[sub_tr], gb.predict_proba(x_tr.iloc[sub_tr])[:, 1],
                           sample_weight=w_tr[sub_tr])
    print(f"  train ROC-AUC (400K subsample): {auc_tr:.4f}  <- under/overfit diagnostic")
    score_te = gb.predict_proba(x_te)[:, 1].astype("float32")
    evaluate("HistGradientBoosting / test", y_te, score_te, w_te)

    try:
        from sklearn.inspection import permutation_importance
        sub = rng.choice(len(x_te), size=min(100_000, len(x_te)), replace=False)
        t0 = time.time()
        imp = permutation_importance(gb, x_te.iloc[sub], y_te[sub],
                                     sample_weight=w_te[sub],
                                     scoring="roc_auc", n_repeats=3,
                                     random_state=0, n_jobs=2)
        top = np.argsort(-imp.importances_mean)[:15]
        print(f"\ntop-15 features by permutation importance (AUC drop, {time.time()-t0:.0f}s):")
        for i in top:
            print(f"  {x_te.columns[i]:<28} {imp.importances_mean[i]:+.4f}")
    except Exception as e:  # noqa: BLE001 - importance is optional reporting
        print("permutation importance skipped:", e)

    joblib.dump({"logreg": logreg, "gb": gb, "features": list(x_tr.columns),
                 "categorical": CATEGORICAL, "target": TARGET},
                "ml/model_conv14.joblib")
    test_ids["y_true"] = y_te
    test_ids["score_gb"] = score_te
    test_ids.to_csv("ml/test_predictions.csv.gz", index=False)
    print("\nsaved: ml/model_conv14.joblib, ml/test_predictions.csv.gz")


if __name__ == "__main__":
    sys.exit(main())
