#!/usr/bin/env python3
"""Lift test: do content-affinity features improve the LTV model?
Joins the already-ingested content (content_features.csv.gz) onto train_ltv_big
and compares the P(pay) classifier AUC + regressor Spearman WITH vs WITHOUT
content, on identical rows (time split). Same content-is-now caveat as churn_lift.
"""
import functools
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier as C, HistGradientBoostingRegressor as R
from sklearn.metrics import roc_auc_score
from scipy.stats import spearmanr

print = functools.partial(print, flush=True)
BASE_CAT = ["provider", "country", "last_plan_id"]
CNUM = ["storage_gb", "library_gb", "n_files", "largest_file_gb", "share_video",
        "share_audio", "share_ebook", "share_software", "n_video", "days_since_last_add",
        "bandwidth_used_gb", "last_signin_day", "account_age_days", "is_empty"]
CCAT = ["content_persona"]


def main():
    df = pd.read_csv("train_ltv_big.csv.gz"); df["index_date"] = pd.to_datetime(df["index_date"]); df["last_plan_id"] = df.last_plan_id.astype(str)
    c = pd.read_csv("content_features.csv.gz")
    c = c[c.content_status == "ok"].copy(); c["is_empty"] = (c.n_files == 0).astype(int); c["content_persona"] = c.content_persona.fillna("none")
    df = df.merge(c[["user_id"] + CNUM + CCAT], on="user_id", how="inner")
    print(f"LTV rows WITH content: {len(df):,} | users {df.user_id.nunique():,}")
    base = [x for x in df.columns if x not in ["user_id", "index_date", "label_rev_365"] + CNUM + CCAT]
    cut = pd.Timestamp("2024-01-01"); tr, te = df[df.index_date < cut], df[df.index_date >= cut]
    ytr, yte = tr.label_rev_365.to_numpy(), te.label_rev_365.to_numpy()
    print(f"train {len(tr):,} / test {len(te):,}\n")

    def run(feats, cats, tag):
        Xtr, Xte = tr[feats].copy(), te[feats].copy()
        for col in cats:
            Xtr[col] = Xtr[col].astype("category"); Xte[col] = pd.Categorical(Xte[col], categories=Xtr[col].cat.categories)
        com = dict(max_iter=400, learning_rate=0.05, max_leaf_nodes=63, min_samples_leaf=50, categorical_features=cats, random_state=42)
        clf = C(**com).fit(Xtr, (ytr > 0).astype(int)); ppay = clf.predict_proba(Xte)[:, 1]
        reg = R(**com).fit(Xtr, np.log1p(ytr)); pred = np.expm1(reg.predict(Xte)).clip(min=0)
        print(f"  {tag:<26} P(pay) AUC {roc_auc_score((yte>0).astype(int),ppay):.4f} | Spearman {spearmanr(pred,yte).correlation:.4f}")
        return roc_auc_score((yte > 0).astype(int), ppay)

    a0 = run(base, BASE_CAT, "baseline (no content)")
    a1 = run(base + CNUM + CCAT, BASE_CAT + CCAT, "+ content features")
    print(f"\nLIFT (P(pay) AUC): {a0:.3f} -> {a1:.3f}  ({a1-a0:+.3f})")


if __name__ == "__main__":
    main()
