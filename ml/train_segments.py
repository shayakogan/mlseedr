#!/usr/bin/env python3
"""Per-segment models vs the global model, on identical segment test rows.

For each segment we train a fresh LogisticRegression on that segment's rows
only, then compare it against the global model (ml/model_conv14.joblib) scored
on the SAME segment test rows. This answers the real question: does
specializing per segment beat one global ranker?

Reuses train.py's cleaning so features line up exactly with the global model.
"""
import os
import warnings

import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

import train as T  # clean(), load(), signed_log1p, lift_at, evaluate, CATEGORICAL

warnings.filterwarnings("ignore")

# segment -> target. Free segments predict conversion; loyal predicts renewal.
SEG_TARGET = {
    "seg_heavy_downloader": "label_conv_14d",
    "seg_winback_active": "label_conv_14d",
    "seg_dormant_payer": "label_conv_14d",
    "seg_soft_cancel": "label_conv_14d",
    "seg_cart_abandoner": "label_conv_14d",
    "seg_monthly_loyal": "label_payment_14d",
}
MIN_TEST_POS = 10  # below this, metrics are noise — report but don't trust


def make_logreg(num_cols):
    prep = ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("log", FunctionTransformer(T.signed_log1p)),
            ("scale", StandardScaler()),
        ]), num_cols),
        ("cat", OneHotEncoder(handle_unknown="ignore", dtype=np.float32), T.CATEGORICAL),
    ], sparse_threshold=0.0)
    return Pipeline([("prep", prep),
                     ("clf", LogisticRegression(max_iter=2000, C=1.0,
                                                class_weight="balanced"))])


def metrics(y, score, w):
    from sklearn.metrics import roc_auc_score
    if len(np.unique(y)) < 2:
        return None, None
    auc = roc_auc_score(y, score, sample_weight=w)
    lift = T.lift_at(y, score, w, 20)  # top-20%: stable enough for small segments
    return auc, lift


def main():
    # The global model was pickled referencing __main__.signed_log1p (train.py
    # ran as __main__); expose it here so joblib.load resolves it.
    import __main__
    __main__.signed_log1p = T.signed_log1p

    # T.load() drops leak cols at read; T.clean() adds missing flags + drops constants.
    df = T.clean(T.load())
    # label_payment_14d is dropped by T.load() (leak list) but is the monthly_loyal
    # target — re-attach it by row order (clean() never drops/reorders rows).
    import pandas as pd
    df["label_payment_14d"] = pd.read_csv(T.DATA, usecols=["label_payment_14d"])["label_payment_14d"].to_numpy()
    glob = joblib.load("ml/model_conv14.joblib")
    g_feat = glob["features"]
    g_logreg = glob["logreg"]
    os.makedirs("ml/segments/models", exist_ok=True)

    print(f"\n{'segment':<22}{'target':<16}{'test+':>6}"
          f"{'  seg_AUC seg_lift':>18}{'  glob_AUC glob_lift':>20}{'  winner':>9}")
    rows = []
    for seg, tgt in SEG_TARGET.items():
        if seg not in df.columns:
            print(f"{seg:<22}empty in window — skipped")
            continue
        sub = df[df[seg] == 1].copy()
        cut = sub["send_date"].quantile(0.8, interpolation="lower")
        tr, te = sub[sub.send_date <= cut], sub[sub.send_date > cut]
        y_tr, y_te = tr[tgt].to_numpy(), te[tgt].to_numpy()
        w_tr, w_te = tr.sample_weight.to_numpy("float64"), te.sample_weight.to_numpy("float64")

        feats = [c for c in g_feat if c in sub.columns]
        num_cols = [c for c in feats if c not in T.CATEGORICAL]

        # per-segment model
        seg_auc = seg_lift = None
        if len(np.unique(y_tr)) >= 2:
            m = make_logreg(num_cols)
            m.fit(tr[feats], y_tr, clf__sample_weight=w_tr)
            seg_auc, seg_lift = metrics(y_te, m.predict_proba(te[feats])[:, 1], w_te)
            joblib.dump({"model": m, "features": feats, "target": tgt},
                        f"ml/segments/models/{seg}.joblib")

        # global model on the SAME segment test rows (only for conversion target)
        g_auc = g_lift = None
        if tgt == "label_conv_14d":
            te_g = te.reindex(columns=g_feat)
            for c in T.CATEGORICAL:  # align categorical dtypes to the trained model
                te_g[c] = te_g[c].astype("category")
            g_auc, g_lift = metrics(y_te, g_logreg.predict_proba(te_g)[:, 1], w_te)

        n_pos = int(y_te.sum())
        flag = "" if n_pos >= MIN_TEST_POS else "  ⚠noisy"
        win = "—"
        if seg_auc is not None and g_auc is not None:
            win = "segment" if seg_auc > g_auc else "global"
        sa = f"{seg_auc:.3f}/x{seg_lift:.1f}" if seg_auc else "n/a"
        ga = f"{g_auc:.3f}/x{g_lift:.1f}" if g_auc else "n/a (renewal)"
        print(f"{seg:<22}{tgt:<16}{n_pos:>6}{sa:>18}{ga:>20}{win:>9}{flag}")
        rows.append((seg, tgt, n_pos, seg_auc, seg_lift, g_auc, g_lift))

    print("\nseg_AUC/seg_lift = модель на сегмент; glob_* = общая модель на тех же строках.")
    print("lift@top-20% внутри сегмента. ⚠noisy = <10 позитивов в тесте, метрика нестабильна.")
    print("Модели сохранены в ml/segments/models/.")


if __name__ == "__main__":
    main()
