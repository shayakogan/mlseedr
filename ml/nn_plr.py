#!/usr/bin/env python3
"""v3: multi-task backbone with PERIODIC numerical embeddings (MLP-PLR) +
post-hoc calibration. Implements the top research-backed lever.

Research (verified, see SEEDR_ML_RESEARCH.md):
  * Gorishniy et al. NeurIPS'22 — periodic/PLE numerical embeddings are the
    highest-leverage tabular-DL design axis; "embedding choice matters more
    than the backbone"; MLP-PLR consistently beats vanilla MLP.
  * van den Goorbergh JAMIA'22 / Carriero StatMed'25 — reweighting (our
    pos_weight) distorts probabilities; recalibrate post-hoc. AUC/lift are
    rank metrics (calibration-invariant) so architecture comparison uses them;
    Brier/ECE measure the probability quality that calibration fixes.

Periodic embedding (per numeric feature x): v = ReLU(Linear([sin(2πcx),cos(2πcx)]))
with learnable frequencies c. Categoricals stay one-hot (already in the matrix).
"""
import functools
import gc
import time

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

import nn_model as M
import train as T

print = functools.partial(print, flush=True)  # noqa: A001 - unbuffer logs (CPU bg run)

OUT = "ml/nn"
TASKS = {"conv": "label_conv_14d", "renewal": "label_payment_14d"}
# leaner than v3-draft (was 24/8): per-feature einsum is the CPU bottleneck.
K_FREQ, D_OUT, SIGMA = 8, 4, 0.5
EPOCHS, BATCH = 7, 16384


class PeriodicEmbedding(nn.Module):
    """Per-feature periodic embedding -> ReLU(Linear). Output: n_num*D_OUT."""

    def __init__(self, n_num, k=K_FREQ, d=D_OUT, sigma=SIGMA):
        super().__init__()
        self.coeffs = nn.Parameter(torch.randn(n_num, k) * sigma)
        self.W = nn.Parameter(torch.randn(n_num, 2 * k, d) * (1.0 / (2 * k) ** 0.5))
        self.b = nn.Parameter(torch.zeros(n_num, d))
        self.n_out = n_num * d

    def forward(self, x_num):                       # (B, n_num)
        z = 2 * np.pi * x_num.unsqueeze(-1) * self.coeffs.unsqueeze(0)  # (B,n_num,k)
        feat = torch.cat([torch.sin(z), torch.cos(z)], dim=-1)         # (B,n_num,2k)
        out = torch.einsum("bnk,nkd->bnd", feat, self.W) + self.b      # (B,n_num,d)
        return torch.relu(out).flatten(1)                              # (B,n_num*d)


class PLRMultiTask(nn.Module):
    def __init__(self, n_num, n_cat, tasks):
        super().__init__()
        self.n_num = n_num
        self.emb = PeriodicEmbedding(n_num)
        n_in = self.emb.n_out + n_cat
        self.trunk = nn.Sequential(
            nn.Linear(n_in, 256), nn.BatchNorm1d(256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128), nn.BatchNorm1d(128), nn.ReLU(), nn.Dropout(0.2),
            nn.Linear(128, 32), nn.ReLU(),
        )
        self.heads = nn.ModuleDict({t: nn.Linear(32, 1) for t in tasks})

    def embed(self, x):
        xe = self.emb(x[:, :self.n_num])
        return self.trunk(torch.cat([xe, x[:, self.n_num:]], dim=1))

    def forward(self, x):
        z = self.embed(x)
        return {t: h(z).squeeze(-1) for t, h in self.heads.items()}


def ece(y, p, w, bins=15):
    """Weighted expected calibration error."""
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    tot = w.sum(); e = 0.0
    for b in range(bins):
        m = idx == b
        if not m.any():
            continue
        wb = w[m].sum()
        e += wb / tot * abs(np.average(y[m], weights=w[m]) - np.average(p[m], weights=w[m]))
    return e


def main():
    torch.set_num_threads(8)
    meta = joblib.load(f"{OUT}/meta.joblib")
    prep = joblib.load(f"{OUT}/preprocessor.joblib")
    feats, num_cols = meta["features"], meta["num_cols"]
    n_num = len(num_cols)

    df = T.clean(T.load())
    df["label_payment_14d"] = pd.read_csv(T.DATA, usecols=["label_payment_14d"])["label_payment_14d"].to_numpy()
    cut = df["send_date"].quantile(0.8, interpolation="lower")
    tr, te = df[df.send_date <= cut], df[df.send_date > cut]
    # carve last 10% of train (by time) as calibration/validation
    vcut = tr["send_date"].quantile(0.9, interpolation="lower")
    trn, val = tr[tr.send_date <= vcut], tr[tr.send_date > vcut]

    def mat(d):
        return prep.transform(d[feats]).astype("float32")
    Xtr, Xval, Xte = mat(trn), mat(val), mat(te)
    n_cat = Xtr.shape[1] - n_num
    ytr = {t: trn[c].to_numpy("float32") for t, c in TASKS.items()}
    yte = {t: te[c].to_numpy("float32") for t, c in TASKS.items()}
    yval = val["label_conv_14d"].to_numpy("float32")
    wtr = trn.sample_weight.to_numpy("float32")
    wte = te.sample_weight.to_numpy("float32")
    wval = val.sample_weight.to_numpy("float32")
    posw = {t: (lambda p: (1 - p) / p)(np.average(ytr[t], weights=wtr)) for t in TASKS}
    print(f"n_num={n_num} n_cat={n_cat} | train {len(Xtr):,} val {len(Xval):,} test {len(Xte):,}")
    del df, tr, trn; gc.collect()

    net = PLRMultiTask(n_num, n_cat, TASKS)
    Xt, wt = M.to_tensor(Xtr), M.to_tensor(wtr)
    yt = {t: M.to_tensor(ytr[t]) for t in TASKS}
    losses = {t: nn.BCEWithLogitsLoss(reduction="none",
                                      pos_weight=torch.tensor([posw[t]])) for t in TASKS}
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-5)
    n, batch = len(Xt), BATCH
    rng = np.random.RandomState(0)
    for ep in range(EPOCHS):
        net.train()
        t0 = time.time()
        perm = rng.permutation(n); tot = 0.0
        for i in range(0, n, batch):
            idx = perm[i:i + batch]
            xb, wb = Xt[idx], wt[idx]
            opt.zero_grad()
            out = net(xb)
            loss = sum((losses[t](out[t], yt[t][idx]) * wb).sum() / wb.sum() for t in TASKS)
            loss.backward(); opt.step()
            tot += loss.item() * len(idx)
        print(f"  plr epoch {ep+1}/{EPOCHS}  loss {tot/n:.5f}  ({time.time()-t0:.0f}s)")

    # evaluate (rank metrics) on test — BATCHED (full-tensor einsum OOMs on this box)
    def predict_heads(X, bs=50_000):
        net.eval()
        outs = {t: [] for t in TASKS}
        with torch.no_grad():
            for i in range(0, len(X), bs):
                z = net.embed(M.to_tensor(X[i:i + bs]))
                for t in TASKS:
                    outs[t].append(torch.sigmoid(net.heads[t](z).squeeze(-1)).numpy())
        return {t: np.concatenate(v) for t, v in outs.items()}

    scores = predict_heads(Xte)
    print("\n=== v3 PLR multi-task / test (vs v2 baseline conv 0.962 / renewal 0.991) ===")
    for t in TASKS:
        auc = roc_auc_score(yte[t], scores[t], sample_weight=wte)
        print(f"[{t}] ROC-AUC {auc:.4f}  lift@1% x{T.lift_at(yte[t], scores[t], wte, 1):.1f}"
              f"  lift@5% x{T.lift_at(yte[t], scores[t], wte, 5):.1f}")

    # post-hoc calibration of the conversion head (isotonic on val), batched
    pval = predict_heads(Xval)["conv"]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(pval, yval, sample_weight=wval)
    praw = scores["conv"]; pcal = iso.transform(praw)
    print("\ncalibration of conv head (weighted, on test):")
    print(f"  raw : Brier {brier_score_loss(yte['conv'], praw, sample_weight=wte):.5e}  ECE {ece(yte['conv'], praw, wte):.5f}")
    print(f"  cal : Brier {brier_score_loss(yte['conv'], pcal, sample_weight=wte):.5e}  ECE {ece(yte['conv'], pcal, wte):.5f}")
    print(f"  (AUC unchanged by calibration: {roc_auc_score(yte['conv'], pcal, sample_weight=wte):.4f})")

    torch.save(net.state_dict(), f"{OUT}/plr_multitask.pt")
    joblib.dump(iso, f"{OUT}/calibrator_conv.joblib")
    print(f"\nsaved {OUT}/plr_multitask.pt + calibrator_conv.joblib")


if __name__ == "__main__":
    main()
