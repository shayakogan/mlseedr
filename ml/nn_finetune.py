#!/usr/bin/env python3
"""Per-segment heads on the frozen shared backbone.

For each segment: freeze the backbone, warm-start a fresh head from the global
head, fine-tune ONLY the head on that segment's rows, and compare it to the
global head on the same segment test rows. Saves ml/nn/head_<segment>.pt.

This is the "swap the last layer + fine-tune per segment" capability.
"""
import copy
import os

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import roc_auc_score

import nn_model as M
import train as T

OUT = "ml/nn"
SEG_TARGET = {
    "seg_heavy_downloader": "label_conv_14d",
    "seg_winback_active": "label_conv_14d",
    "seg_dormant_payer": "label_conv_14d",
    "seg_soft_cancel": "label_conv_14d",
    "seg_cart_abandoner": "label_conv_14d",
    "seg_monthly_loyal": "label_payment_14d",
}
MIN_TEST_POS = 10


def evalw(y, score, w):
    if len(np.unique(y)) < 2:
        return None, None
    return roc_auc_score(y, score, sample_weight=w), T.lift_at(y, score, w, 20)


def main():
    torch.set_num_threads(8)
    meta = joblib.load(f"{OUT}/meta.joblib")
    prep = joblib.load(f"{OUT}/preprocessor.joblib")
    feats, n_in = meta["features"], meta["n_in"]

    backbone = M.Backbone(n_in)
    backbone.load_state_dict(torch.load(f"{OUT}/backbone.pt"))
    backbone.eval()
    for p in backbone.parameters():       # freeze the shared representation
        p.requires_grad_(False)
    global_head = M.Head()
    global_head.load_state_dict(torch.load(f"{OUT}/head_global.pt"))

    df = T.clean(T.load())
    df["label_payment_14d"] = pd.read_csv(T.DATA, usecols=["label_payment_14d"])["label_payment_14d"].to_numpy()

    print(f"\n{'segment':<22}{'target':<16}{'test+':>6}"
          f"{'  seg-head AUC/lift':>20}{'  glob-head AUC/lift':>22}{'  ΔAUC':>8}")
    for seg, tgt in SEG_TARGET.items():
        if seg not in df.columns:
            print(f"{seg:<22}empty in window — skipped"); continue
        sub = df[df[seg] == 1].copy()
        cut = sub["send_date"].quantile(0.8, interpolation="lower")
        tr, te = sub[sub.send_date <= cut], sub[sub.send_date > cut]
        Xtr = prep.transform(tr[feats]).astype("float32")
        Xte = prep.transform(te[feats]).astype("float32")
        ytr, yte = tr[tgt].to_numpy("float32"), te[tgt].to_numpy("float32")
        wtr, wte = tr.sample_weight.to_numpy("float32"), te.sample_weight.to_numpy("float32")

        n_pos = int(yte.sum())
        if len(np.unique(ytr)) < 2:
            print(f"{seg:<22}{tgt:<16}{n_pos:>6}   no positive in train — skipped")
            continue

        # swap in a fresh head, warm-started from the global head
        net = M.Net(n_in)
        net.backbone = backbone
        net.head = copy.deepcopy(global_head)
        pos = np.average(ytr, weights=wtr)
        pos_weight = (1 - pos) / max(pos, 1e-9)
        M.train_loop(net, Xtr, ytr, wtr, epochs=15, lr=5e-3,
                     params=net.head.parameters(), pos_weight=pos_weight,
                     eval_module=net.backbone, seed=0)

        seg_auc, seg_lift = evalw(yte, M.predict_proba(net, Xte), wte)
        # global head on the same backbone embeddings / same rows
        gnet = M.Net(n_in); gnet.backbone = backbone; gnet.head = global_head
        g_auc, g_lift = (None, None)
        if tgt == "label_conv_14d":
            g_auc, g_lift = evalw(yte, M.predict_proba(gnet, Xte), wte)

        torch.save(net.head.state_dict(), f"{OUT}/head_{seg}.pt")
        flag = "" if n_pos >= MIN_TEST_POS else "  ⚠noisy"
        sa = f"{seg_auc:.3f}/x{seg_lift:.1f}" if seg_auc else "n/a"
        ga = f"{g_auc:.3f}/x{g_lift:.1f}" if g_auc else "n/a (renewal task)"
        d = f"{seg_auc-g_auc:+.3f}" if (seg_auc and g_auc) else "—"
        print(f"{seg:<22}{tgt:<16}{n_pos:>6}{sa:>20}{ga:>22}{d:>8}{flag}")

    print("\nseg-head = backbone(frozen)+fine-tuned per-segment head; "
          "glob-head = same backbone + global head.")
    print("Heads saved to ml/nn/head_<segment>.pt (≈tiny). Serve: backbone.pt + chosen head.")


if __name__ == "__main__":
    main()
