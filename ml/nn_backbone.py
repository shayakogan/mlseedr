#!/usr/bin/env python3
"""Train the shared backbone + global head on ALL data (conversion task).

Saves: ml/nn/preprocessor.joblib, ml/nn/backbone.pt, ml/nn/head_global.pt,
plus the feature list. Per-segment heads are produced by nn_finetune.py.
"""
import os

import joblib
import numpy as np
import torch
from sklearn.metrics import roc_auc_score

import nn_model as M
import train as T

TARGET = "label_conv_14d"
OUT = "ml/nn"


def main():
    torch.set_num_threads(8)
    os.makedirs(OUT, exist_ok=True)

    df = T.clean(T.load())
    cut = df["send_date"].quantile(0.8, interpolation="lower")
    tr, te = df[df.send_date <= cut], df[df.send_date > cut]
    print(f"time split at {cut}: train {len(tr):,}, test {len(te):,}")

    drop = ["send_date", "user_id", TARGET, "sample_weight"]
    feats = [c for c in df.columns if c not in drop and not c.startswith("label_")]
    num_cols = [c for c in feats if c not in M.T.CATEGORICAL]

    prep = M.build_preprocessor(num_cols)
    Xtr = prep.fit_transform(tr[feats]).astype("float32")
    Xte = prep.transform(te[feats]).astype("float32")
    ytr, yte = tr[TARGET].to_numpy("float32"), te[TARGET].to_numpy("float32")
    wtr, wte = tr.sample_weight.to_numpy("float32"), te.sample_weight.to_numpy("float32")
    n_in = Xtr.shape[1]
    print(f"feature matrix: {Xtr.shape} ({Xtr.nbytes/1e9:.2f} GB) | n_in={n_in}")

    # pos_weight from the TRUE (weighted) prevalence so the rare class counts.
    pos = np.average(ytr, weights=wtr)
    pos_weight = (1 - pos) / pos
    print(f"weighted base rate {pos*100:.4f}% -> pos_weight {pos_weight:.0f}")

    del df, tr  # free RAM before training
    import gc; gc.collect()

    net = M.Net(n_in)
    M.train_loop(net, Xtr, ytr, wtr, epochs=8, lr=1e-3,
                 pos_weight=pos_weight, log_prefix="backbone")

    score = M.predict_proba(net, Xte)
    auc = roc_auc_score(yte, score, sample_weight=wte)
    base = np.average(yte, weights=wte)
    print(f"\n[backbone+global head / test]  ROC-AUC {auc:.4f}  (base {base*100:.4f}%)")
    for pct in (1, 5, 10):
        print(f"  lift@top-{pct:>2}%: x{T.lift_at(yte, score, wte, pct):.1f}")

    joblib.dump(prep, f"{OUT}/preprocessor.joblib")
    joblib.dump({"features": feats, "num_cols": num_cols, "n_in": n_in,
                 "categorical": M.T.CATEGORICAL, "target": TARGET},
                f"{OUT}/meta.joblib")
    torch.save(net.backbone.state_dict(), f"{OUT}/backbone.pt")
    torch.save(net.head.state_dict(), f"{OUT}/head_global.pt")
    print(f"\nsaved backbone + global head + preprocessor to {OUT}/")


if __name__ == "__main__":
    main()
