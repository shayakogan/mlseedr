#!/usr/bin/env python3
"""Build the forward-LTV (CLV) training set from dataset_cache/revenue.tsv.

Sample unit: (user_id, index_date) for every user with >=1 completed txn before
index_date. Features are RFM-style, computed strictly before index_date (10y of
revenue history → no new extract needed). Label = revenue in (index, index+365].

We use semi-annual index dates 2018-01 … 2025-01 (each with a full forward year
within data), so one user contributes many rows across their lifetime — a rich,
fully-historical training set. Behavioral features (web/email) are NOT included
here (they only exist from 2025-05); this is the revenue-only CLV core.
"""
import csv
import gzip
from datetime import date, timedelta

CACHE = "dataset_cache/revenue.tsv"
OUT = "train_ltv.csv.gz"
EPOCH = date(1970, 1, 1)
HORIZON = 365


def eday(d):
    return (d - EPOCH).days


def index_dates():
    out = []
    for y in range(2018, 2026):
        for mo in (1, 7):
            d = date(y, mo, 1)
            if eday(d) + HORIZON <= eday(date(2026, 6, 11)):  # full forward window in cache
                out.append(eday(d))
    return out


def main():
    rev = {}  # user -> [(day, amount)]
    with open(CACHE) as f:
        for line in f:
            u, ts, amt = line.rstrip("\n").split("\t")
            rev.setdefault(int(u), []).append((int(ts) // 86400, float(amt)))
    for u in rev:
        rev[u].sort()
    print(f"payers: {len(rev):,}")

    idxs = index_dates()
    print(f"index dates: {len(idxs)} "
          f"({date.fromordinal(idxs[0]+EPOCH.toordinal())} … {date.fromordinal(idxs[-1]+EPOCH.toordinal())})")

    cols = ["user_id", "index_date",
            "recency_days", "frequency", "monetary_sum", "monetary_avg", "last_amount",
            "tenure_days", "gap_median", "txns_365_prior", "rev_365_prior", "is_annual",
            "label_rev_365"]
    f = gzip.open(OUT, "wt", newline="")
    w = csv.writer(f)
    w.writerow(cols)

    n = 0; npos = 0
    for u, txns in rev.items():
        for I in idxs:
            prior = [(d, a) for d, a in txns if d < I]
            if not prior:
                continue
            days = [d for d, _ in prior]
            amts = [a for _, a in prior]
            freq = len(prior)
            msum = sum(amts)
            mavg = msum / freq
            last_amt = amts[-1]
            recency = I - days[-1]
            tenure = I - days[0]
            if freq >= 2:
                gaps = sorted(days[i] - days[i - 1] for i in range(1, freq))
                gap_med = gaps[len(gaps) // 2]
            else:
                gap_med = -1
            txns_365 = sum(1 for d in days if I - d <= 365)
            rev_365 = sum(a for d, a in prior if I - d <= 365)
            is_annual = 1 if (gap_med > 180 or last_amt >= 60) else 0
            label = sum(a for d, a in txns if I < d <= I + HORIZON)
            if label > 0:
                npos += 1
            w.writerow([u, date.fromordinal(I + EPOCH.toordinal()).isoformat(),
                        recency, freq, round(msum, 2), round(mavg, 2), round(last_amt, 2),
                        tenure, gap_med, txns_365, round(rev_365, 2), is_annual,
                        round(label, 2)])
            n += 1
    f.close()
    print(f"rows: {n:,}  with future revenue: {npos:,} ({100*npos/max(n,1):.1f}%)")
    print(f"output: {OUT}")


if __name__ == "__main__":
    main()
