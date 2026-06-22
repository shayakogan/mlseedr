#!/usr/bin/env python3
"""Ablation: do the NEW features (storage_used_pct, content persona, edge/QoS,
task usage) lift churn AUC over the baseline churn feature set?

Honest caveat: the churn dataset is HISTORICAL weekly snapshots, but the new
features are a NOW snapshot (ml.user_*). Joining now-features onto past rows is a
temporal mismatch — so any retro lift here is a LOWER BOUND; the real lift comes
once we train on live customer_360_history snapshots (~30 days out).

Same protocol as churn_train.py: user-disjoint 80/20 split, GBM, MODEL B
(pre-emptive) is the actionable one. Reports AUC baseline vs +new (delta), plus
the discriminative power of persona / storage bucket on churn directly.
"""
import base64
import functools
import io
import os
import urllib.request

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

print = functools.partial(print, flush=True)

DATA = "train_churn.csv.gz"
TARGET = "label_churn_30"
BASE_CAT = ["country", "last_sub_event", "last_plan_id"]
DROP = ["user_id", "snapshot_date", TARGET]

NEW_NUM = ["storage_used_pct", "n_lost_files", "files_added_30d", "content_storage_gb",
           "share_video", "bandwidth_used_gb", "n_rate_limited_7d", "n_stall_7d",
           "stream_gb_7d", "downloads_30d", "task_failure_rate"]
NEW_CAT = ["content_persona"]


def ch_creds():
    u = p = None
    for line in open(os.path.expanduser("~/.clickhouse.seedr")):
        k, _, v = line.strip().partition("=")
        if k == "user": u = v
        elif k == "password": p = v
    return u, p


def ch(sql):
    u, p = ch_creds()
    auth = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
    req = urllib.request.Request("http://127.0.0.1:8123/", data=sql.encode())
    req.add_header("Authorization", auth)
    return urllib.request.urlopen(req, timeout=120).read().decode()


def fetch_new_features():
    sql = """
    SELECT uc.user_id AS user_id,
      ifNull(sq.storage_used_pct,-1) AS storage_used_pct,
      ifNull(uc.content_persona,'unknown') AS content_persona,
      ifNull(uc.n_lost_files,0) AS n_lost_files,
      ifNull(uc.files_added_30d,0) AS files_added_30d,
      ifNull(uc.storage_gb,-1) AS content_storage_gb,
      ifNull(uc.share_video,-1) AS share_video,
      ifNull(uc.bandwidth_used_gb,-1) AS bandwidth_used_gb,
      ifNull(e.n_rate_limited_7d,0) AS n_rate_limited_7d,
      ifNull(e.n_stall_7d,0) AS n_stall_7d,
      ifNull(e.stream_gb_7d,0) AS stream_gb_7d,
      ifNull(t.downloads_30d,0) AS downloads_30d,
      ifNull(t.task_failure_rate,0) AS task_failure_rate
    FROM ml.user_content uc
    LEFT JOIN ml.user_storage_quota sq ON uc.user_id=sq.user_id
    LEFT JOIN ml.user_edge e ON uc.user_id=e.user_id
    LEFT JOIN ml.user_tasks t ON uc.user_id=t.user_id
    SETTINGS join_use_nulls=1
    FORMAT TSVWithNames"""
    return pd.read_csv(io.StringIO(ch(sql)), sep="\t")


def split(df):
    rng = np.random.RandomState(42)
    users = df.user_id.unique()
    test_u = set(rng.choice(users, int(len(users) * 0.2), replace=False))
    return df[~df.user_id.isin(test_u)], df[df.user_id.isin(test_u)]


def gbm_auc(tr, te, feats, cat):
    trg, teg = tr[feats].copy(), te[feats].copy()
    for c in cat:
        trg[c] = trg[c].astype("category")
        teg[c] = pd.Categorical(teg[c], categories=trg[c].cat.categories)
    gb = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                        min_samples_leaf=30, l2_regularization=2.0,
                                        categorical_features=cat, random_state=42)
    gb.fit(trg, tr[TARGET].to_numpy())
    s = gb.predict_proba(teg)[:, 1]
    y = te[TARGET].to_numpy()
    k = max(int(len(s) * 0.1), 1)
    top = np.argsort(-s)[:k]
    return roc_auc_score(y, s), average_precision_score(y, s), y[top].mean() / y.mean(), gb, s


def run(df, label, drop_cs):
    if drop_cs:
        df = df[df.had_cancel_sched_30 == 0].copy().drop(columns=["had_cancel_sched_30"])
    tr, te = split(df)
    base_feats = [c for c in df.columns
                  if c not in DROP + NEW_NUM + NEW_CAT and c in df.columns]
    base_num_cat = [c for c in BASE_CAT if c in base_feats]
    print(f"\n=== {label} ===")
    print(f"train {len(tr):,} ({tr[TARGET].mean()*100:.1f}%) · test {len(te):,} "
          f"({te[TARGET].mean()*100:.1f}%) · user-disjoint")

    a0, p0, l0, *_ = gbm_auc(tr, te, base_feats, base_num_cat)
    print(f"  baseline ({len(base_feats)} feats):       AUC {a0:.4f}  PR-AUC {p0:.3f}  lift@10% ×{l0:.2f}")

    new_feats = base_feats + NEW_NUM + NEW_CAT
    new_cat = base_num_cat + NEW_CAT
    a1, p1, l1, gb, _ = gbm_auc(tr, te, new_feats, new_cat)
    print(f"  + new ({len(new_feats)} feats):           AUC {a1:.4f}  PR-AUC {p1:.3f}  lift@10% ×{l1:.2f}")
    print(f"  Δ from new features:               AUC {a1-a0:+.4f}  PR-AUC {p1-p0:+.3f}  lift {l1-l0:+.2f}")

    try:
        from sklearn.inspection import permutation_importance
        teg = te[new_feats].copy()
        for c in new_cat:
            teg[c] = pd.Categorical(teg[c], categories=tr[c].astype("category").cat.categories)
        imp = permutation_importance(gb, teg, te[TARGET].to_numpy(), scoring="roc_auc",
                                     n_repeats=5, random_state=0, n_jobs=4)
        order = np.argsort(-imp.importances_mean)
        newset = set(NEW_NUM + NEW_CAT)
        ranked_new = [(new_feats[i], imp.importances_mean[i], list(order).index(i) + 1)
                      for i in order if new_feats[i] in newset][:6]
        print("  new-feature importance (rank/total {}):".format(len(new_feats)))
        for name, val, rk in ranked_new:
            print(f"     #{rk:<2} {name:<20} {val:+.4f}")
    except Exception as e:  # noqa: BLE001
        print("  importance skipped:", e)


def discriminative(df):
    """Do personas / storage buckets separate churn at all? (raw, no model)"""
    print("\n=== discriminative power (raw churn rate, latest snapshot per user) ===")
    latest = df.sort_values("snapshot_date").groupby("user_id").tail(1)
    print("  churn rate by content_persona:")
    g = latest.groupby("content_persona")[TARGET].agg(["mean", "count"]).sort_values("mean", ascending=False)
    for persona, row in g.iterrows():
        if row["count"] >= 30:
            print(f"     {persona:<16} {row['mean']*100:5.1f}%  (n={int(row['count'])})")
    print("  churn rate by storage_used_pct bucket:")
    lat = latest[latest.storage_used_pct >= 0].copy()
    lat["bucket"] = pd.cut(lat.storage_used_pct, [0, 50, 80, 100, 1e9],
                           labels=["<50%", "50-80%", "80-100%", "100%+"])
    g2 = lat.groupby("bucket", observed=True)[TARGET].agg(["mean", "count"])
    for b, row in g2.iterrows():
        print(f"     {str(b):<10} {row['mean']*100:5.1f}%  (n={int(row['count'])})")


def main():
    df = pd.read_csv(DATA, dtype={"country": "string", "last_sub_event": "string",
                                  "snapshot_date": "string", "last_plan_id": "string"})
    df["country"] = df["country"].fillna("unknown").replace("", "unknown")
    df["last_sub_event"] = df["last_sub_event"].fillna("none")
    df["last_plan_id"] = df["last_plan_id"].fillna("0")
    nf = fetch_new_features()
    print(f"loaded {len(df):,} churn rows; new features for {len(nf):,} users; "
          f"matched {df.user_id.isin(nf.user_id).mean()*100:.0f}% of churn rows")
    df = df.merge(nf, on="user_id", how="left")
    for c in NEW_NUM:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["content_persona"] = df["content_persona"].fillna("unknown")

    run(df, "MODEL A — full population (operational)", drop_cs=False)
    run(df, "MODEL B — pre-emptive (before cancel click)", drop_cs=True)
    discriminative(df)


if __name__ == "__main__":
    main()
