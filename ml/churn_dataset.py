#!/usr/bin/env python3
"""Build the churn training set from the local cache (dataset_cache/).

Problem: predict whether an ACTIVE subscriber cancels/expires within 30 days of
a snapshot date. user_subscription_state is churn-blind (no history), so we
RECONSTRUCT subscriber status at each past snapshot from the subscription event
log: a user is "active premium" at date S if their latest subscription.* event
before S is created / reactivated / billing_plan_change / cancellation_scheduled
(the last still means paying until period end) — i.e. NOT canceled/expired.

Sample unit: (user_id, snapshot_date) for every active subscriber at S.
Label: subscription.canceled OR subscription.expired in (S, S+30].
Features: computed strictly before S (no leakage), reusing the same window
semantics as cmd/dataset. `had_cancel_sched_30` flags the near-label cohort
(users who already clicked cancel) so churn can be analysed with/without them.

Caveats baked in / noted: task.* & storage_warning streams start after all
churn snapshots (≤2026-05-10) → those features are ~0 and dropped at train;
plan tier is proxied by last_txn_amount (cache has no billing_plan_id).
"""
import csv
import glob
import gzip
import os
from datetime import date, timedelta

CACHE = "dataset_cache"
OUT = "train_churn.csv.gz"
HORIZON = 30
EPOCH = date(1970, 1, 1)

ACTIVE = {"subscription.created", "subscription.reactivated",
          "subscription.billing_plan_change", "subscription.cancellation_scheduled"}
CHURN = {"subscription.canceled", "subscription.expired"}


def eday(d: date) -> int:
    return (d - EPOCH).days


def snapshots():
    out, d = [], date(2026, 2, 1)
    last = date(2026, 6, 17) - timedelta(days=HORIZON + 1)
    while d <= last:
        out.append(eday(d))
        d += timedelta(days=14)
    return out


def parse_day(s):  # 'YYYY-MM-DD' -> epoch day
    y, m, dd = s.split("-")
    return eday(date(int(y), int(m), int(dd)))


def main():
    # ---- pass 1: subscription events → subscriber set + per-user event list ----
    subs = {}  # user -> list[(day, type)]
    with open(f"{CACHE}/sub_events.tsv") as f:
        for line in f:
            u, ts, typ = line.rstrip("\n").split("\t")
            subs.setdefault(int(u), []).append((int(ts) // 86400, typ))
    for u in subs:
        subs[u].sort()
    keep = set(subs)
    print(f"subscribers (ever a subscription event): {len(keep):,}")

    # ---- pass 2: load feature caches, filtered to subscribers only ----
    def load_glob(prefix, cols):
        data = {}
        for path in sorted(glob.glob(f"{CACHE}/{prefix}_*.tsv")):
            with open(path) as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    u = int(p[0])
                    if u not in keep:
                        continue
                    rec = (parse_day(p[1]),) + tuple(int(x) for x in p[2:])
                    data.setdefault(u, []).append(rec)
        for u in data:
            data[u].sort()
        return data

    # web: d, pageviews, file_dl, archive_dl, file_views, streams, pricing_views, goal4
    web = load_glob("web", 8)
    # email: d, mautic_sent, internal_sent, opened, clicked
    email = load_glob("email", 5)

    revenue = {}  # user -> [(day, amount)]
    with open(f"{CACHE}/revenue.tsv") as f:
        for line in f:
            u, ts, amt = line.rstrip("\n").split("\t")
            u = int(u)
            if u in keep:
                revenue.setdefault(u, []).append((int(ts) // 86400, float(amt)))
    for u in revenue:
        revenue[u].sort()

    profile = {}  # user -> (country, first_day, devices)
    with open(f"{CACHE}/profile.tsv") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            u = int(p[0])
            if u in keep:
                profile[u] = (p[1], int(p[2]) // 86400, int(p[3]))
    mobile = {}
    with open(f"{CACHE}/mobile.tsv") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            u = int(p[0])
            if u in keep:
                mobile[u] = float(p[1])

    print(f"loaded caches (subscriber-filtered): web={sum(len(v) for v in web.values()):,} "
          f"email={sum(len(v) for v in email.values()):,} payers={len(revenue):,}")

    cols = ["user_id", "snapshot_date",
            "em_sent_30", "em_sent_90", "em_opened_90", "em_clicked_90", "em_open_rate_90",
            "days_since_last_send", "days_since_last_open",
            "web_active_days_30", "pageviews_30", "file_dl_30", "file_dl_90",
            "archive_dl_30", "streams_90", "pricing_views_30", "days_since_web_activity",
            "ltv_before_usd", "txns_before", "days_since_last_txn", "last_txn_amount", "txns_365d",
            "prior_txn_gap_median", "cycle_position", "is_annual",
            "sub_tenure_days", "days_since_sub_event", "last_sub_event",
            "prior_cancels", "prior_reactivations", "had_cancel_sched_30",
            "country", "tenure_days", "devices", "mobile_share", "snapshot_age",
            "label_churn_30"]

    snaps = snapshots()
    print(f"snapshots: {len(snaps)} ({date.fromordinal(snaps[0]+date(1970,1,1).toordinal())} "
          f"… {date.fromordinal(snaps[-1]+date(1970,1,1).toordinal())}), horizon {HORIZON}d")

    n_rows = n_pos = n_cancsched = 0
    f = gzip.open(OUT, "wt", newline="")
    w = csv.writer(f)
    w.writerow(cols)

    for u, sev in subs.items():
        wrecs = web.get(u, [])
        erecs = email.get(u, [])
        txns = revenue.get(u, [])
        prof = profile.get(u)
        for S in snaps:
            before = [(d, t) for d, t in sev if d < S]
            if not before:
                continue
            last_day, last_type = before[-1]
            if last_type not in ACTIVE:
                continue  # not active at S (already churned) → not at risk
            if not any(t in ("subscription.created", "subscription.reactivated") for _, t in before):
                continue  # never truly subscribed

            churn = 1 if any(t in CHURN and S < d <= S + HORIZON for d, t in sev) else 0

            # subscription-state features
            sub_start = next((d for d, t in before
                              if t in ("subscription.created", "subscription.reactivated")), last_day)
            prior_cancels = sum(1 for d, t in before if t in CHURN)
            prior_react = sum(1 for d, t in before if t == "subscription.reactivated")
            had_cs30 = 1 if any(t == "subscription.cancellation_scheduled" and S - 30 <= d < S
                                for d, t in before) else 0

            # email windows (strict < S)
            s30 = s90 = o90 = c90 = 0
            last_send = last_open = -1
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

            # web windows
            wa30 = pv30 = fd30 = fd90 = ad30 = st90 = pr30 = 0
            last_web = -1
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

            # monetary (strict < S)
            ltv = 0.0; ntx = t365 = 0; last_txn_day = -1; last_amt = 0.0
            prior_days = []
            for d, a in txns:
                if d >= S:
                    break
                ltv += a; ntx += 1; last_txn_day = d; last_amt = a
                prior_days.append(d)
                if S - d <= 365:
                    t365 += 1
            days_since_txn = S - last_txn_day if last_txn_day >= 0 else -1
            # billing-cycle position: how far into the current period (the dominant
            # churn signal). median gap between prior txns = the user's term.
            if len(prior_days) >= 2:
                gaps = sorted(prior_days[i] - prior_days[i - 1] for i in range(1, len(prior_days)))
                med_gap = gaps[len(gaps) // 2]
            else:
                med_gap = -1
            cycle_pos = round(days_since_txn / med_gap, 3) if med_gap > 0 and days_since_txn >= 0 else -1.0
            is_annual = 1 if (med_gap > 180 or last_amt >= 60) else 0

            country, tenure, devices = "", -1, 0
            if prof:
                country, devices = prof[0], prof[2]
                first = min(prof[1], txns[0][0]) if txns else prof[1]
                tenure = S - first

            w.writerow([
                u, date.fromordinal(S + EPOCH.toordinal()).isoformat(),
                s30, s90, o90, c90, open_rate, last_send, last_open,
                wa30, pv30, fd30, fd90, ad30, st90, pr30, last_web,
                round(ltv, 2), ntx, days_since_txn, round(last_amt, 2), t365,
                med_gap, cycle_pos, is_annual,
                S - sub_start, S - last_day, last_type.replace("subscription.", ""),
                prior_cancels, prior_react, had_cs30,
                country, tenure, devices, round(mobile.get(u, 0.0), 3),
                S - parse_day("2026-01-12"),
                churn,
            ])
            n_rows += 1; n_pos += churn; n_cancsched += had_cs30

    f.close()
    print(f"\nrows: {n_rows:,}  churn-positive: {n_pos:,} ({100*n_pos/max(n_rows,1):.2f}%)")
    print(f"  of which had cancellation_scheduled in prior 30d: {n_cancsched:,}")
    print(f"output: {OUT}")


if __name__ == "__main__":
    main()
