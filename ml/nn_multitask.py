#!/usr/bin/env python3
"""Multi-task backbone: ONE shared trunk, several co-trained heads.

This is the architecturally-correct version of "one main model + swappable
heads": the backbone is trained jointly on conversion AND renewal, so the
shared embedding carries signal for both. Per-segment heads then fine-tune on
top of a representation that actually knows the relevant tasks (the v1
frozen-conversion backbone gave a near-random renewal head — AUC 0.515).

Saves ml/nn/backbone_mt.pt + head_conv.pt + head_renewal.pt.
"""
import gc
import os

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import roc_auc_score

import nn_model as M
import train as T

OUT = "ml/nn"
TASKS = {"conv": "label_conv_14d", "renewal": "label_payment_14d"}


class MultiTaskNet(nn.Module):
    def __init__(self, n_in, tasks):
        super().__init__()
        self.backbone = M.Backbone(n_in)
        self.heads = nn.ModuleDict({t: M.Head() for t in tasks})

    def forward(self, x):
        z = self.backbone(x)
        return {t: h(z).squeeze(-1) for t, h in self.heads.items()}


def main():
    torch.set_num_threads(8)
    os.makedirs(OUT, exist_ok=True)
    meta = joblib.load(f"{OUT}/meta.joblib")
    prep = joblib.load(f"{OUT}/preprocessor.joblib")
    feats, n_in = meta["features"], meta["n_in"]

    df = T.clean(T.load())
    df["label_payment_14d"] = pd.read_csv(T.DATA, usecols=["label_payment_14d"])["label_payment_14d"].to_numpy()
    cut = df["send_date"].quantile(0.8, interpolation="lower")
    tr, te = df[df.send_date <= cut], df[df.send_date > cut]

    Xtr = prep.transform(tr[feats]).astype("float32")
    Xte = prep.transform(te[feats]).astype("float32")
    ytr = {t: tr[c].to_numpy("float32") for t, c in TASKS.items()}
    yte = {t: te[c].to_numpy("float32") for t, c in TASKS.items()}
    wtr = tr.sample_weight.to_numpy("float32")
    wte = te.sample_weight.to_numpy("float32")
    posw = {t: (lambda p: (1 - p) / p)(np.average(ytr[t], weights=wtr)) for t in TASKS}
    print(f"matrix {Xtr.shape} | pos_weight " + " ".join(f"{t}={posw[t]:.0f}" for t in TASKS))
    del df, tr; gc.collect()

    net = MultiTaskNet(n_in, TASKS)
    Xt = M.to_tensor(Xtr); wt = M.to_tensor(wtr)
    yt = {t: M.to_tensor(ytr[t]) for t in TASKS}
    losses = {t: nn.BCEWithLogitsLoss(reduction="none",
                                      pos_weight=torch.tensor([posw[t]])) for t in TASKS}
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    n, batch = len(Xt), 8192
    rng = np.random.RandomState(0)
    for ep in range(8):
        net.train()
        perm = rng.permutation(n); tot = 0.0
        for i in range(0, n, batch):
            idx = perm[i:i + batch]
            xb = Xt[idx]; wb = wt[idx]
            opt.zero_grad()
            out = net(xb)
            loss = sum((losses[t](out[t], yt[t][idx]) * wb).sum() / wb.sum() for t in TASKS)
            loss.backward(); opt.step()
            tot += loss.item() * len(idx)
        print(f"  mt epoch {ep+1}/8  loss {tot/n:.5f}")

    net.eval()
    with torch.no_grad():
        zt = net.backbone(M.to_tensor(Xte))
        for t in TASKS:
            s = torch.sigmoid(net.heads[t](zt).squeeze(-1)).numpy()
            auc = roc_auc_score(yte[t], s, sample_weight=wte)
            print(f"\n[mt head '{t}' / test]  ROC-AUC {auc:.4f}")
            for pct in (1, 5, 10):
                print(f"  lift@top-{pct:>2}%: x{T.lift_at(yte[t], s, wte, pct):.1f}")

    torch.save(net.backbone.state_dict(), f"{OUT}/backbone_mt.pt")
    for t in TASKS:
        torch.save(net.heads[t].state_dict(), f"{OUT}/head_{t}.pt")
    print(f"\nsaved multi-task backbone + {len(TASKS)} heads to {OUT}/")


if __name__ == "__main__":
    main()
