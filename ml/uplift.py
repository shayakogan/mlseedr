#!/usr/bin/env python3
"""Quasi-uplift per segment from observational data (train_uplift.csv.gz).

NOT a randomized experiment. Treatment = a marketing email was sent that day;
control = a web-active day for the same population with NO email within ±14d.
Two estimates per segment:
  1) naive  = weighted conv(treatment) - weighted conv(control)  [confounded]
  2) S-learner = one GBM with `treatment` as a feature; uplift = mean over
     segment rows of pred(treatment=1) - pred(treatment=0)  [adjusts observed]
Heavy caveats: no randomization, control is scarce and drawn from less-emailed
users (selection), unobserved confounders remain. Directional only.
"""
import functools

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

print = functools.partial(print, flush=True)  # noqa: A001 - unbuffer logs (CPU bg run)

DATA = "train_uplift.csv.gz"
TARGET = "label_conv_14d"
CATEGORICAL = ["country", "last_sub_event"]
SENTINEL = ["days_since_last_send", "days_since_last_open", "days_since_web_activity",
            "days_since_last_txn", "days_since_sub_event", "tenure_days"]
DROP = ["user_id", "send_date", "era", "mobile_share_now",
        "label_payment_14d", "label_sub_started_14d", "days_to_payment", "first_payment_usd"]


def wmean(y, w):
    return float(np.average(y, weights=w)) if len(y) else float("nan")


def main():
    df = pd.read_csv(DATA, dtype={"country": "string", "last_sub_event": "string", "era": "string"})
    print(f"loaded {len(df):,} rows | treatment {int((df.treatment==1).sum()):,} "
          f"control {int((df.treatment==0).sum()):,}")
    segs = [c for c in df.columns if c.startswith("seg_")]

    for c in SENTINEL:
        df[c] = df[c].astype("float32").where(df[c] >= 0)
    df["country"] = df["country"].fillna("unknown").replace("", "unknown")
    df["last_sub_event"] = df["last_sub_event"].fillna("none").replace("", "none")
    for c in CATEGORICAL:
        df[c] = df[c].astype("category")

    feats = [c for c in df.columns if c not in DROP + segs + [TARGET, "sample_weight"]]
    # `treatment` stays in feats (the S-learner's key feature)
    num = [c for c in feats if c not in CATEGORICAL]

    # ---- naive per-segment difference ----
    print(f"\n{'segment':<22}{'Treat n/conv':>20}{'Control n/conv':>22}{'naive uplift':>14}")
    rows = {}
    for s in segs:
        sub = df[df[s] == 1]
        t, c = sub[sub.treatment == 1], sub[sub.treatment == 0]
        if len(c) < 50 or len(t) < 50:
            print(f"{s:<22}{'(control too small)':>40}")
            continue
        ct = wmean(t[TARGET].to_numpy(), t.sample_weight.to_numpy())
        cc = wmean(c[TARGET].to_numpy(), c.sample_weight.to_numpy())
        rows[s] = (len(t), ct, len(c), cc)
        print(f"{s:<22}{len(t):>9,}/{ct*100:>7.3f}%{len(c):>12,}/{cc*100:>8.3f}%"
              f"{(ct-cc)*100:>+12.3f}pp")

    # ---- S-learner adjusted uplift ----
    print("\ntraining S-learner (GBM, treatment as feature)...")
    X = df[feats].copy()
    for c in CATEGORICAL:
        X[c] = X[c].astype("category")
    y = df[TARGET].to_numpy("float32")
    w = df.sample_weight.to_numpy("float64")
    gb = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.1, max_leaf_nodes=31,
                                        min_samples_leaf=100, l2_regularization=5.0,
                                        categorical_features=CATEGORICAL, random_state=42)
    gb.fit(X, y, sample_weight=w)

    # counterfactual predictions WITHOUT copying X (memory): flip treatment in place
    orig = X["treatment"].to_numpy().copy()
    X["treatment"] = 1
    df["_p1"] = gb.predict_proba(X)[:, 1]
    X["treatment"] = 0
    df["_p0"] = gb.predict_proba(X)[:, 1]
    X["treatment"] = orig
    df["_tau"] = df["_p1"] - df["_p0"]

    print(f"\n{'segment':<22}{'S-learner uplift':>18}{'  (pred conv if emailed → if not)':>34}")
    for s in segs:
        sub = df[df[s] == 1]
        if len(sub) < 100:
            continue
        tau = wmean(sub["_tau"].to_numpy(), sub.sample_weight.to_numpy())
        p1 = wmean(sub["_p1"].to_numpy(), sub.sample_weight.to_numpy())
        p0 = wmean(sub["_p0"].to_numpy(), sub.sample_weight.to_numpy())
        print(f"{s:<22}{tau*100:>+16.3f}pp{p1*100:>22.3f}% → {p0*100:.3f}%")

    overall_tau = wmean(df["_tau"].to_numpy(), df.sample_weight.to_numpy())
    print(f"\noverall S-learner uplift: {overall_tau*100:+.3f} pp"
          " (≈0 is an artifact: treatment is 98% of rows → GBM never splits on it)")

    # ---- T-learner: separate models on treatment and control (can't collapse to 0) ----
    print("\ntraining T-learner (separate GBM per arm; control has ~%d positives)..."
          % int(df.loc[df.treatment == 0, TARGET].sum()))
    feats_noT = [c for c in feats if c != "treatment"]
    Xc = X[feats_noT]
    common = dict(max_iter=200, learning_rate=0.1, max_leaf_nodes=31,
                  min_samples_leaf=50, l2_regularization=5.0,
                  categorical_features=CATEGORICAL, random_state=42)
    mask_t, mask_c = (df.treatment == 1).to_numpy(), (df.treatment == 0).to_numpy()
    gT = HistGradientBoostingClassifier(**common).fit(Xc[mask_t], y[mask_t], sample_weight=w[mask_t])
    gC = HistGradientBoostingClassifier(**common).fit(Xc[mask_c], y[mask_c], sample_weight=w[mask_c])
    df["_tT"] = gT.predict_proba(Xc)[:, 1] - gC.predict_proba(Xc)[:, 1]
    print(f"\n{'segment':<22}{'T-learner uplift':>18}")
    for s in segs:
        sub = df[df[s] == 1]
        if len(sub) < 100:
            continue
        print(f"{s:<22}{wmean(sub['_tT'].to_numpy(), sub.sample_weight.to_numpy())*100:>+16.3f}pp")

    print("\n⚠ observational, NOT randomized. Control is scarce (76K vs 3.6M), drawn from "
          "less-emailed users (selection), and overlap is poor — so the naive negative\n"
          "uplifts reflect bias, the S-learner collapses to 0 (imbalance), and the T-learner\n"
          "control arm is data-starved. CONCLUSION: a randomized holdout is required; these\n"
          "observational numbers cannot identify the email's causal effect. Directional only.")


if __name__ == "__main__":
    main()
