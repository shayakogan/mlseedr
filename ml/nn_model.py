"""Shared backbone + swappable per-segment head, for Seedr email-conversion.

Architecture (the design the user asked for):

    features --> [ BACKBONE ]  -->  embedding(32)  -->  [ HEAD ]  --> p(convert)
                 shared, trained                         per-segment,
                 on ALL data                             swappable & fine-tuned

The backbone is trained once on the global conversion task. For a segment you
freeze the backbone, drop in a fresh (or copied) head, and fine-tune only the
head (optionally the last backbone block) on that segment's rows. Heads are
tiny (33 floats), so every segment campaign gets its own cheap adapter while
sharing one representation.

This module holds the model + the preprocessing so train/finetune/serve agree.
"""
import numpy as np
import torch
import torch.nn as nn
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

import train as T  # clean(), load(), signed_log1p, CATEGORICAL, lift_at

EMB_DIM = 32


def build_preprocessor(num_cols):
    """Dense float matrix: numeric (impute→signed-log1p→scale) + one-hot cats."""
    return ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("log", FunctionTransformer(T.signed_log1p)),
            ("scale", StandardScaler()),
        ]), num_cols),
        ("cat", OneHotEncoder(handle_unknown="ignore", dtype=np.float32), T.CATEGORICAL),
    ], sparse_threshold=0.0)


class Backbone(nn.Module):
    """Shared feature extractor: input -> 128 -> 64 -> embedding(EMB_DIM)."""

    def __init__(self, n_in, emb=EMB_DIM):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_in, 128), nn.BatchNorm1d(128), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, 64), nn.BatchNorm1d(64), nn.ReLU(), nn.Dropout(0.2),
            nn.Linear(64, emb), nn.ReLU(),
        )

    def forward(self, x):
        return self.net(x)


class Head(nn.Module):
    """The swappable last layer: embedding -> logit. 33 params for EMB_DIM=32."""

    def __init__(self, emb=EMB_DIM):
        super().__init__()
        self.fc = nn.Linear(emb, 1)

    def forward(self, z):
        return self.fc(z).squeeze(-1)


class Net(nn.Module):
    def __init__(self, n_in, emb=EMB_DIM):
        super().__init__()
        self.backbone = Backbone(n_in, emb)
        self.head = Head(emb)

    def forward(self, x):
        return self.head(self.backbone(x))


def to_tensor(x):
    return torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))


def predict_proba(net, X, batch=100_000, device="cpu"):
    net.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(X), batch):
            xb = to_tensor(X[i:i + batch]).to(device)
            out.append(torch.sigmoid(net(xb)).cpu().numpy())
    return np.concatenate(out)


def train_loop(net, Xtr, ytr, wtr, *, epochs, lr, params=None, pos_weight=None,
               batch=8192, device="cpu", seed=0, log_prefix="", eval_module=None):
    """Weighted-BCE training. `params` limits what is optimized (head-only when
    fine-tuning); None = whole net. `wtr` are per-row sample weights.
    `eval_module` is kept in eval() every step (freeze its BatchNorm/Dropout) —
    pass the backbone when fine-tuning a head so its running stats don't move."""
    torch.manual_seed(seed)
    net.to(device)
    opt = torch.optim.Adam(params if params is not None else net.parameters(), lr=lr)
    pw = None if pos_weight is None else torch.tensor([pos_weight], device=device)
    lossf = nn.BCEWithLogitsLoss(reduction="none", pos_weight=pw)

    Xt, yt, wt = to_tensor(Xtr), to_tensor(ytr), to_tensor(wtr)
    n = len(Xt)
    rng = np.random.RandomState(seed)
    for ep in range(epochs):
        net.train()
        if eval_module is not None:
            eval_module.eval()
        perm = rng.permutation(n)
        tot = 0.0
        for i in range(0, n, batch):
            idx = perm[i:i + batch]
            xb = Xt[idx].to(device); yb = yt[idx].to(device); wb = wt[idx].to(device)
            opt.zero_grad()
            logit = net(xb)
            loss = (lossf(logit, yb) * wb).sum() / wb.sum()
            loss.backward()
            opt.step()
            tot += loss.item() * len(idx)
        if log_prefix:
            print(f"  {log_prefix} epoch {ep+1}/{epochs}  loss {tot/n:.5f}")
    return net
