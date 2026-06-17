#!/usr/bin/env python3
"""Train churn models on train_churn.csv.gz (active-subscriber 30-day churn).

Two models:
  A) full population — operational risk scoring (keeps had_cancel_sched_30, a
     strong leading signal).
  B) pre-emptive — only subscribers who have NOT yet clicked cancel
     (had_cancel_sched_30==0); predict churn BEFORE the user signals it. This is
     the proactive-retention model.

Chronological split by snapshot_date (last 2 snapshots = test). Churn is ~20%
(balanced), so we report ROC-AUC, PR-AUC, lift@k AND precision/recall@top-decile
+ calibration (ECE/Brier).
"""
import functools

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

print = functools.partial(print, flush=True)

DATA = "train_churn.csv.gz"
TARGET = "label_churn_30"
CAT = ["country", "last_sub_event"]
DROP = ["user_id", "snapshot_date", TARGET]


def slog1p(x):
    return np.sign(x) * np.log1p(np.abs(x))


def ece(y, p, bins=10):
    e = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, e) - 1, 0, bins - 1)
    out = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            out += m.mean() * abs(y[m].mean() - p[m].mean())
    return out


def lift_at(y, s, pct):
    k = max(int(len(s) * pct / 100), 1)
    top = np.argsort(-s)[:k]
    return y[top].mean() / y.mean()


def evaluate(tag, y, s):
    auc = roc_auc_score(y, s)
    ap = average_precision_score(y, s)
    # precision/recall at top-decile
    k = max(int(len(s) * 0.1), 1)
    top = np.argsort(-s)[:k]
    prec = y[top].mean()
    rec = y[top].sum() / y.sum()
    print(f"[{tag}] AUC {auc:.3f}  PR-AUC {ap:.3f}  base {y.mean()*100:.1f}%"
          f"  | top-10%: precision {prec*100:.0f}% recall {rec*100:.0f}% (lift ×{lift_at(y,s,10):.1f})"
          f"  | Brier {brier_score_loss(y,s):.4f} ECE {ece(y,s):.3f}")
    return auc


def run(df, label, drop_cs):
    if drop_cs:
        df = df[df.had_cancel_sched_30 == 0].copy()
        df = df.drop(columns=["had_cancel_sched_30"])
    snaps = sorted(df.snapshot_date.unique())
    test_snaps = set(snaps[-2:])
    tr = df[~df.snapshot_date.isin(test_snaps)]
    te = df[df.snapshot_date.isin(test_snaps)]
    drop = [c for c in DROP if c in df.columns]
    feats = [c for c in df.columns if c not in drop]
    num = [c for c in feats if c not in CAT]
    ytr, yte = tr[TARGET].to_numpy(), te[TARGET].to_numpy()
    print(f"\n=== {label} ===")
    print(f"train {len(tr):,} ({ytr.mean()*100:.1f}% churn) · test {len(te):,} "
          f"({yte.mean()*100:.1f}% churn) · {len(feats)} feats · test snaps {sorted(test_snaps)}")

    # logistic baseline
    prep = ColumnTransformer([
        ("num", Pipeline([("imp", SimpleImputer(strategy="median")),
                          ("log", FunctionTransformer(slog1p)), ("sc", StandardScaler())]), num),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT)])
    lr = Pipeline([("p", prep), ("c", LogisticRegression(max_iter=2000, class_weight="balanced"))])
    lr.fit(tr[feats], ytr)
    evaluate("LogReg", yte, lr.predict_proba(te[feats])[:, 1])

    # gradient boosting (native cats/NaN)
    trg, teg = tr[feats].copy(), te[feats].copy()
    for c in CAT:
        trg[c] = trg[c].astype("category")
        teg[c] = pd.Categorical(teg[c], categories=trg[c].cat.categories)
    gb = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                        min_samples_leaf=30, l2_regularization=2.0,
                                        categorical_features=CAT, random_state=42)
    gb.fit(trg, ytr)
    s = gb.predict_proba(teg)[:, 1]
    evaluate("GBM", yte, s)

    try:
        from sklearn.inspection import permutation_importance
        imp = permutation_importance(gb, teg, yte, scoring="roc_auc", n_repeats=5, random_state=0, n_jobs=4)
        top = np.argsort(-imp.importances_mean)[:10]
        print("  top features:", ", ".join(f"{feats[i]}({imp.importances_mean[i]:+.3f})" for i in top))
    except Exception as e:  # noqa: BLE001
        print("  importance skipped:", e)
    return gb


def main():
    df = pd.read_csv(DATA, dtype={"country": "string", "last_sub_event": "string",
                                  "snapshot_date": "string"})
    df["country"] = df["country"].fillna("unknown").replace("", "unknown")
    df["last_sub_event"] = df["last_sub_event"].fillna("none")
    print(f"loaded {len(df):,} rows, {df[TARGET].mean()*100:.1f}% churn")
    run(df, "MODEL A — full population (operational risk score)", drop_cs=False)
    run(df, "MODEL B — pre-emptive (before the cancel click)", drop_cs=True)


if __name__ == "__main__":
    main()
