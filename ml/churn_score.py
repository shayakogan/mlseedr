#!/usr/bin/env python3
"""Score the current active subscriber base for 30-day churn risk → ml.churn_scores.

Reconstructs who is an active subscriber as of today (latest subscription.* event
∈ created/reactivated/billing_plan_change/cancellation_scheduled), computes the
SAME features as churn_dataset.py for S=today, trains the operational churn model
(GBM, Model A) on train_churn.csv.gz, and predicts churn_risk_30d. Left-censoring
applies: only subscribers with a subscription event since 2026-01-12 are scorable.
"""
import csv
import functools
from datetime import date

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

print = functools.partial(print, flush=True)
CACHE = "dataset_cache"
EPOCH = date(1970, 1, 1)
TODAY = (date(2026, 6, 18) - EPOCH).days
ACTIVE = {"subscription.created", "subscription.reactivated",
          "subscription.billing_plan_change", "subscription.cancellation_scheduled"}
CHURN = {"subscription.canceled", "subscription.expired"}
FEATS = ["em_sent_30", "em_sent_90", "em_opened_90", "em_clicked_90", "em_open_rate_90",
         "days_since_last_send", "days_since_last_open", "web_active_days_30", "pageviews_30",
         "file_dl_30", "file_dl_90", "archive_dl_30", "streams_90", "pricing_views_30",
         "days_since_web_activity", "ltv_before_usd", "txns_before", "days_since_last_txn",
         "last_txn_amount", "txns_365d", "prior_txn_gap_median", "cycle_position",
         "days_to_renewal", "last_plan_id", "is_annual",
         "sub_tenure_days", "days_since_sub_event", "last_sub_event", "prior_cancels",
         "prior_reactivations", "had_cancel_sched_30", "country", "tenure_days", "devices",
         "mobile_share", "snapshot_age"]
CAT = ["country", "last_sub_event", "last_plan_id"]


def feats_for(sev, wrecs, erecs, txns, prof, mob, S):
    """Compute churn features for one subscriber as of S (matches churn_dataset.py).
    Returns (is_active, dict) — is_active False means not subscribed at S."""
    before = [(d, t) for d, t in sev if d < S]
    if not before:
        return False, None
    last_day, last_type = before[-1]
    if last_type not in ACTIVE or not any(t in ("subscription.created", "subscription.reactivated") for _, t in before):
        return False, None
    sub_start = next((d for d, t in before if t in ("subscription.created", "subscription.reactivated")), last_day)
    prior_cancels = sum(1 for d, t in before if t in CHURN)
    prior_react = sum(1 for d, t in before if t == "subscription.reactivated")
    had_cs30 = 1 if any(t == "subscription.cancellation_scheduled" and S - 30 <= d < S for d, t in before) else 0
    s30 = s90 = o90 = c90 = 0; last_send = last_open = -1
    for d, ms, isent, op, cl in reversed(erecs):
        if d >= S:
            continue
        dd = S - d
        if dd > 90:
            break
        sent = ms + isent
        if sent and last_send < 0:
            last_send = dd
        if op and last_open < 0:
            last_open = dd
        s90 += sent; o90 += op; c90 += cl
        if dd <= 30:
            s30 += sent
    open_rate = round(o90 / s90, 4) if s90 else 0.0
    wa30 = pv30 = fd30 = fd90 = ad30 = st90 = pr30 = 0; last_web = -1
    for rec in reversed(wrecs):
        d = rec[0]
        if d >= S:
            continue
        dd = S - d
        if dd > 90:
            break
        if last_web < 0:
            last_web = dd
        fd90 += rec[2]; st90 += rec[5]
        if dd <= 30:
            wa30 += 1; pv30 += rec[1]; fd30 += rec[2]; ad30 += rec[3]; pr30 += rec[6]
    ltv = 0.0; ntx = t365 = 0; last_txn_day = -1; last_amt = 0.0; last_plan = "0"; pdays = []
    for d, a, pl in txns:
        if d >= S:
            break
        ltv += a; ntx += 1; last_txn_day = d; last_amt = a; last_plan = pl; pdays.append(d)
        if S - d <= 365:
            t365 += 1
    dstx = S - last_txn_day if last_txn_day >= 0 else -1
    med_gap = sorted(pdays[i] - pdays[i - 1] for i in range(1, len(pdays)))[max(len(pdays) - 1, 1) // 2] if len(pdays) >= 2 else -1
    cyc = round(dstx / med_gap, 3) if med_gap > 0 and dstx >= 0 else -1.0
    days_to_renewal = (med_gap - dstx) if (med_gap > 0 and dstx >= 0) else -999
    is_annual = 1 if (med_gap > 180 or last_amt >= 60) else 0
    country, tenure, devices = "", -1, 0
    if prof:
        country, devices = prof[0], prof[2]
        first = min(prof[1], txns[0][0]) if txns else prof[1]
        tenure = S - first
    row = dict(em_sent_30=s30, em_sent_90=s90, em_opened_90=o90, em_clicked_90=c90,
               em_open_rate_90=open_rate, days_since_last_send=last_send, days_since_last_open=last_open,
               web_active_days_30=wa30, pageviews_30=pv30, file_dl_30=fd30, file_dl_90=fd90,
               archive_dl_30=ad30, streams_90=st90, pricing_views_30=pr30, days_since_web_activity=last_web,
               ltv_before_usd=round(ltv, 2), txns_before=ntx, days_since_last_txn=dstx,
               last_txn_amount=round(last_amt, 2), txns_365d=t365, prior_txn_gap_median=med_gap,
               cycle_position=cyc, days_to_renewal=days_to_renewal, last_plan_id=last_plan,
               is_annual=is_annual, sub_tenure_days=S - sub_start,
               days_since_sub_event=S - last_day, last_sub_event=last_type.replace("subscription.", ""),
               prior_cancels=prior_cancels, prior_reactivations=prior_react, had_cancel_sched_30=had_cs30,
               country=country if country else "unknown", tenure_days=tenure, devices=devices,
               mobile_share=round(mob, 3), snapshot_age=S - ((date(2026, 1, 12) - EPOCH).days))
    return True, row


def main():
    subs = {}
    for line in open(f"{CACHE}/sub_events.tsv"):
        u, ts, typ = line.rstrip("\n").split("\t")
        subs.setdefault(int(u), []).append((int(ts) // 86400, typ))
    for u in subs:
        subs[u].sort()
    keep = set(subs)

    from datetime import date as _d
    def pday(s):
        y, m, dd = s.split("-"); return (_d(int(y), int(m), int(dd)) - EPOCH).days
    web, email = {}, {}
    import glob
    for path in sorted(glob.glob(f"{CACHE}/web_*.tsv")):
        for line in open(path):
            p = line.rstrip("\n").split("\t"); u = int(p[0])
            if u in keep:
                web.setdefault(u, []).append((pday(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5]), int(p[6]), int(p[7])))
    for path in sorted(glob.glob(f"{CACHE}/email_*.tsv")):
        for line in open(path):
            p = line.rstrip("\n").split("\t"); u = int(p[0])
            if u in keep:
                email.setdefault(u, []).append((pday(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5])))
    for u in web: web[u].sort()
    for u in email: email[u].sort()
    revenue = {}
    for line in open(f"{CACHE}/revenue_full.tsv"):
        p = line.rstrip("\n").split("\t"); u = int(p[0])
        if u in keep:
            revenue.setdefault(u, []).append((int(p[1]) // 86400, float(p[2]), p[3]))
    for u in revenue: revenue[u].sort()
    profile, mobile = {}, {}
    for line in open(f"{CACHE}/profile.tsv"):
        p = line.rstrip("\n").split("\t"); u = int(p[0])
        if u in keep:
            profile[u] = (p[1], int(p[2]) // 86400, int(p[3]))
    for line in open(f"{CACHE}/mobile.tsv"):
        p = line.rstrip("\n").split("\t"); u = int(p[0])
        if u in keep:
            mobile[u] = float(p[1])

    # build today rows for active subscribers
    uids, rows = [], []
    for u, sev in subs.items():
        ok, row = feats_for(sev, web.get(u, []), email.get(u, []), revenue.get(u, []),
                            profile.get(u), mobile.get(u, 0.0), TODAY)
        if ok:
            uids.append(u); rows.append(row)
    X = pd.DataFrame(rows)[FEATS]
    print(f"active subscribers scorable today: {len(X):,}")

    # train operational churn model (Model A) on the historical churn dataset
    tr = pd.read_csv("train_churn.csv.gz", dtype={"country": "string", "last_sub_event": "string", "last_plan_id": "string"})
    tr["country"] = tr["country"].fillna("unknown"); tr["last_sub_event"] = tr["last_sub_event"].fillna("none")
    tr["last_plan_id"] = tr["last_plan_id"].fillna("0")
    Xtr = tr[FEATS].copy()
    for c in CAT:
        Xtr[c] = Xtr[c].astype("category")
    gb = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                        min_samples_leaf=30, l2_regularization=2.0,
                                        categorical_features=CAT, random_state=42)
    gb.fit(Xtr, tr["label_churn_30"])

    Xs = X.copy()
    for c in CAT:
        Xs[c] = pd.Categorical(Xs[c].fillna("unknown" if c == "country" else "none"),
                               categories=Xtr[c].cat.categories)
    risk = gb.predict_proba(Xs)[:, 1]

    # ---- exact-billing-date override (Partytime `expires_on` + payment method) ----
    # expires_on exists only since June 2026 (postdates the training window, so the
    # MODEL can't learn it yet), but it is a near-deterministic involuntary-churn
    # signal: 120/120 already-expired subscribers had has_active_payment_method=0.
    # We blend it into the score as a rule override for the users who have it.
    exp = {}  # user -> (latest expires_day, has_pm)
    from datetime import datetime
    for line in open(f"{CACHE}/sub_events_full.tsv"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 6 or p[3] == "":
            continue
        u = int(p[0])
        try:
            ed = (datetime.strptime(p[3][:10], "%Y-%m-%d").date() - date(1970, 1, 1)).days
        except ValueError:
            continue
        # keep the latest event (file is sorted by user, ts ascending)
        exp[u] = (ed, int(p[4]))
    cs_flag = X.had_cancel_sched_30.to_numpy()
    d2e = []; haspm = []; brisk = []
    for i, u in enumerate(uids):
        if u in exp:
            ed, pm = exp[u]; dte = ed - TODAY
            haspm.append(pm); d2e.append(dte)
            if -7 <= dte <= 30 and pm == 0:
                brisk.append(0.95)          # imminent involuntary expiry (validated 120/120)
            elif 0 <= dte <= 30 and cs_flag[i] == 1:
                brisk.append(0.90)          # imminent voluntary
            elif 0 <= dte <= 15:
                brisk.append(0.50)          # renewal imminent, has payment method
            else:
                brisk.append(0.0)
        else:
            d2e.append(-999); haspm.append(-1); brisk.append(0.0)
    final_risk = np.maximum(risk, np.array(brisk))
    risk_source = ["billing_rule" if b > r else "model" for b, r in zip(brisk, risk)]

    out = pd.DataFrame({"user_id": uids, "churn_risk_30d": final_risk.round(4),
                        "model_risk": risk.round(4), "days_to_expires": d2e,
                        "has_payment_method": haspm, "risk_source": risk_source,
                        "had_cancel_sched": X.had_cancel_sched_30.values,
                        "days_since_last_txn": X.days_since_last_txn.values,
                        "cycle_position": X.cycle_position.values,
                        "sub_tenure_days": X.sub_tenure_days.values,
                        "last_sub_event": X.last_sub_event.values,
                        "prior_reactivations": X.prior_reactivations.values,
                        "ltv_before_usd": X.ltv_before_usd.values,
                        "is_annual": X.is_annual.values, "country": X.country.values})
    out = out.sort_values("churn_risk_30d", ascending=False)
    out.to_csv("churn_scores_current.csv.gz", index=False)
    print(f"mean churn_risk {risk.mean():.3f} | flagged cancellation_scheduled: {int(out.had_cancel_sched.sum())}")
    print(f"high risk (>0.5): {int((risk>0.5).sum())} | saved churn_scores_current.csv.gz")


if __name__ == "__main__":
    main()
