#!/usr/bin/env python3
"""Build a LARGE forward-LTV (CLV) dataset from dataset_cache/revenue_full.tsv.

Bigger than v1: MONTHLY index dates 2017-01 … 2025-06, over the active customer
base (≥1 prior txn AND recency ≤ 730d), with a rich transaction-history feature
set (RFM + spend dynamics/trend + plan/provider/geo + refunds). Label = NET
revenue in the next 365 days. 10y of history → no extract beyond revenue_full.tsv.

`feature_row()` is the single source of truth for feature computation — imported
by ml/ltv2_score.py so the production scoring builds identical features.
"""
import csv
import gzip
from datetime import date, timedelta

CACHE = "dataset_cache/revenue_full.tsv"
OUT = "train_ltv_big.csv.gz"
EPOCH = date(1970, 1, 1)
H = 365              # forward label horizon
ACTIVE_WINDOW = 730  # only score payers active within 2y (the relevant base)

FEATURE_COLS = [
    "recency_days", "frequency", "monetary_sum", "monetary_avg", "monetary_std",
    "first_amount", "last_amount", "min_amount", "max_amount",
    "tenure_days", "months_since_first", "avg_monthly_rev",
    "gap_median", "gap_min", "gap_max",
    "txns_90", "txns_180", "txns_365", "rev_90", "rev_180", "rev_365",
    "rev_prev_365", "rev_trend", "is_annual", "n_plans", "last_plan_id",
    "provider", "country", "refund_count", "refund_amount",
]


def eday(d):
    return (d - EPOCH).days


def load_revenue(path=CACHE):
    rev = {}  # user -> list[(day, amount, plan, provider, country)]
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            u = int(p[0])
            rev.setdefault(u, []).append((int(p[1]) // 86400, float(p[2]), p[3], p[4], p[5]))
    for u in rev:
        rev[u].sort()
    return rev


def feature_row(txns, I, active_window=ACTIVE_WINDOW):
    """Features for one user as of index day I (strictly prior txns only).
    Returns a list aligned to FEATURE_COLS, or None if the user is not in the
    active base at I (no prior txn, or recency > active_window)."""
    days = [t[0] for t in txns]
    k = 0
    while k < len(days) and days[k] < I:
        k += 1
    if k == 0:
        return None
    pamts = [txns[i][1] for i in range(k)]
    pdays = days[:k]
    recency = I - pdays[-1]
    if recency > active_window:
        return None
    freq = k
    msum = sum(pamts)
    mavg = msum / freq
    mstd = (sum((a - mavg) ** 2 for a in pamts) / freq) ** 0.5
    tenure = I - pdays[0]
    months_since_first = tenure / 30.4
    avg_monthly = msum / max(months_since_first, 1)
    if freq >= 2:
        gaps = sorted(pdays[i] - pdays[i - 1] for i in range(1, freq))
        gmed, gmin, gmax = gaps[len(gaps) // 2], gaps[0], gaps[-1]
    else:
        gmed = gmin = gmax = -1
    t90 = sum(1 for d in pdays if I - d <= 90)
    t180 = sum(1 for d in pdays if I - d <= 180)
    t365 = sum(1 for d in pdays if I - d <= 365)
    r90 = sum(a for d, a in zip(pdays, pamts) if I - d <= 90)
    r180 = sum(a for d, a in zip(pdays, pamts) if I - d <= 180)
    r365 = sum(a for d, a in zip(pdays, pamts) if I - d <= 365)
    rprev = sum(a for d, a in zip(pdays, pamts) if 365 < I - d <= 730)
    rtrend = round(r365 / rprev, 3) if rprev > 0 else (-1.0)
    is_annual = 1 if (gmed > 180 or pamts[-1] >= 60) else 0
    n_plans = len(set(txns[i][2] for i in range(k)))
    refunds = [a for a in pamts if a < 0]
    return [recency, freq, round(msum, 2), round(mavg, 2), round(mstd, 2),
            round(pamts[0], 2), round(pamts[-1], 2), round(min(pamts), 2), round(max(pamts), 2),
            tenure, round(months_since_first, 1), round(avg_monthly, 2),
            gmed, gmin, gmax, t90, t180, t365,
            round(r90, 2), round(r180, 2), round(r365, 2), round(rprev, 2), rtrend,
            is_annual, n_plans, txns[k - 1][2], txns[k - 1][3], txns[k - 1][4],
            len(refunds), round(sum(refunds), 2)]


def index_dates():
    out, y, m = [], 2017, 1
    last = eday(date(2026, 6, 11)) - H
    while True:
        d = date(y, m, 1)
        if eday(d) > last:
            break
        out.append(eday(d))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def main():
    rev = load_revenue()
    print(f"payers: {len(rev):,}")
    idxs = index_dates()
    print(f"monthly index dates: {len(idxs)} "
          f"({date.fromordinal(idxs[0]+EPOCH.toordinal())} … {date.fromordinal(idxs[-1]+EPOCH.toordinal())})")

    f = gzip.open(OUT, "wt", newline="")
    w = csv.writer(f)
    w.writerow(["user_id", "index_date"] + FEATURE_COLS + ["label_rev_365"])
    n = npos = 0
    for u, txns in rev.items():
        days = [t[0] for t in txns]
        amts = [t[1] for t in txns]
        for I in idxs:
            fr = feature_row(txns, I)
            if fr is None:
                continue
            label = max(sum(a for d, a in zip(days, amts) if I < d <= I + H), 0.0)
            if label > 0:
                npos += 1
            w.writerow([u, date.fromordinal(I + EPOCH.toordinal()).isoformat(), *fr, round(label, 2)])
            n += 1
    f.close()
    print(f"rows: {n:,}  with future revenue: {npos:,} ({100*npos/max(n,1):.1f}%)")
    print(f"output: {OUT}")


if __name__ == "__main__":
    main()
