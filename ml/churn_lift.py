#!/usr/bin/env python3
"""Lift test: does adding content-affinity features improve the churn model?

Ingests content for the churn-dataset subscribers, joins it onto train_churn,
and compares churn GBM AUC WITHOUT vs WITH content features on identical rows
(user-disjoint split). Caveat: content is a NOW snapshot vs historical churn
snapshots — a mild leak (content type is sticky), same caveat as mobile_share_now;
directional evidence, not a production-clean gain.
"""
import functools
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

import content_ingest as CI

print = functools.partial(print, flush=True)

BASE_CAT = ["country", "last_sub_event", "last_plan_id"]
CONTENT_NUM = ["storage_gb", "library_gb", "n_files", "largest_file_gb",
               "share_video", "share_audio", "share_ebook", "share_software",
               "n_video", "n_audio", "days_since_last_add", "bandwidth_used_gb",
               "last_signin_day", "account_age_days", "is_empty"]
CONTENT_CAT = ["content_persona"]


def main():
    ch = pd.read_csv("train_churn.csv.gz", dtype={"country": "string", "last_sub_event": "string", "last_plan_id": "string"})
    for c in BASE_CAT:
        ch[c] = ch[c].fillna("none")
    base_feats = [c for c in ch.columns if c not in ("user_id", "snapshot_date", "label_churn_30")]

    uids = ch.user_id.unique().tolist()
    print(f"ingesting content for {len(uids):,} churn subscribers...")
    cmap = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for feat in ex.map(lambda u: CI.features(u, CI.fetch(u)), uids):
            cmap[feat["user_id"]] = feat
    cdf = pd.DataFrame([c for c in cmap.values() if c.get("content_status") == "ok"])
    cdf["is_empty"] = (cdf.n_files == 0).astype(int)
    cdf["content_persona"] = cdf["content_persona"].fillna("none")
    print(f"content ok for {len(cdf):,} subscribers ({len(cdf)/len(uids)*100:.0f}% of churn users)")

    df = ch.merge(cdf[["user_id"] + CONTENT_NUM + CONTENT_CAT], on="user_id", how="inner")
    print(f"churn rows WITH content: {len(df):,} ({df.label_churn_30.mean()*100:.1f}% churn)")

    rng = np.random.RandomState(42)
    tu = set(rng.choice(df.user_id.unique(), int(df.user_id.nunique() * 0.2), replace=False))
    tr, te = df[~df.user_id.isin(tu)], df[df.user_id.isin(tu)]
    ytr, yte = tr.label_churn_30.to_numpy(), te.label_churn_30.to_numpy()

    def run(feats, cats, tag):
        Xtr, Xte = tr[feats].copy(), te[feats].copy()
        for c in cats:
            Xtr[c] = Xtr[c].astype("category")
            Xte[c] = pd.Categorical(Xte[c], categories=Xtr[c].cat.categories)
        g = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                           min_samples_leaf=30, l2_regularization=2.0,
                                           categorical_features=cats, random_state=42).fit(Xtr, ytr)
        auc = roc_auc_score(yte, g.predict_proba(Xte)[:, 1])
        print(f"  {tag:<28} AUC {auc:.4f}")
        return auc, g

    print(f"\ntrain {len(tr):,} / test {len(te):,} (user-disjoint):")
    a0, _ = run(base_feats, BASE_CAT, "baseline (no content)")
    a1, g1 = run(base_feats + CONTENT_NUM + CONTENT_CAT, BASE_CAT + CONTENT_CAT, "+ content features")
    print(f"\nLIFT: AUC {a0:.3f} -> {a1:.3f}  ({a1-a0:+.3f})")

    try:
        from sklearn.inspection import permutation_importance
        Xte = te[base_feats + CONTENT_NUM + CONTENT_CAT].copy()
        for c in BASE_CAT + CONTENT_CAT:
            Xte[c] = pd.Categorical(Xte[c], categories=tr[c].astype("category").cat.categories)
        imp = permutation_importance(g1, Xte, yte, scoring="roc_auc", n_repeats=4, random_state=0, n_jobs=4)
        feats_all = base_feats + CONTENT_NUM + CONTENT_CAT
        top = np.argsort(-imp.importances_mean)[:12]
        print("\ntop features (with content):", ", ".join(
            f"{feats_all[i]}({imp.importances_mean[i]:+.3f})" for i in top))
        cset = set(CONTENT_NUM + CONTENT_CAT)
        print("content features in top-12:", [feats_all[i] for i in top if feats_all[i] in cset])
    except Exception as e:  # noqa: BLE001
        print("importance skipped:", e)


if __name__ == "__main__":
    main()
