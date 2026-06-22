#!/usr/bin/env python3
"""Randomized holdout assignment for campaign uplift measurement (Phase 3b).

For a given campaign (= customer_360.next_best_action), deterministically assign a
persistent ~10% control (holdout) that will NOT receive the email, so we can later
measure the CAUSAL effect: uplift = conv(treatment) - conv(holdout).

Writes ml.campaign_holdout (user_id, campaign, arm, assigned_date). Deterministic
hash on (user_id, campaign) → the same user stays in the same arm across re-runs.

Usage:  python ml/assign_holdout.py -campaign hd_upsell -pct 10
Measure later (after the campaign ran + label window):
  SELECT h.arm, count() users,
         avg(p.paid) conv_rate
  FROM ml.campaign_holdout h
  LEFT JOIN (<users who paid in (assigned_date, +14d]>) p USING user_id
  WHERE h.campaign='hd_upsell' GROUP BY h.arm;   -- uplift = treatment - holdout
"""
import argparse
import base64
import hashlib
import io
import urllib.request

CH = "http://127.0.0.1:8123/"


def creds():
    u = p = None
    import os
    for line in open(os.path.expanduser("~/.clickhouse.seedr")):
        k, _, v = line.strip().partition("=")
        if k == "user": u = v
        elif k == "password": p = v
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()


AUTH = creds()


def q(sql, data=None):
    req = urllib.request.Request(CH + ("?query=" + urllib.parse.quote(sql) if data else ""),
                                 data=data if data else sql.encode(), method="POST")
    req.add_header("Authorization", AUTH)
    return urllib.request.urlopen(req, timeout=120).read().decode()


import urllib.parse


def arm(user_id, campaign, pct):
    h = int(hashlib.md5(f"{user_id}:{campaign}".encode()).hexdigest()[:8], 16) % 100
    return "holdout" if h < pct else "treatment"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-campaign", required=True, help="next_best_action value")
    ap.add_argument("-pct", type=int, default=10, help="holdout percent")
    a = ap.parse_args()

    ids = [int(x) for x in q(
        f"SELECT user_id FROM ml.customer_360 WHERE next_best_action='{a.campaign}' FORMAT TSV").split()]
    rows = [(u, a.campaign, arm(u, a.campaign, a.pct)) for u in ids]
    tsv = "".join(f"{u}\t{c}\t{ar}\n" for u, c, ar in rows)

    q("CREATE TABLE IF NOT EXISTS ml.campaign_holdout (user_id UInt64, campaign String, "
      "arm LowCardinality(String), assigned_date Date DEFAULT today()) "
      "ENGINE=MergeTree ORDER BY (campaign, user_id)")
    q(f"ALTER TABLE ml.campaign_holdout DELETE WHERE campaign='{a.campaign}'")
    q(f"INSERT INTO ml.campaign_holdout (user_id, campaign, arm) FORMAT TSV", data=tsv.encode())

    n_hold = sum(1 for *_, ar in rows if ar == "holdout")
    print(f"campaign '{a.campaign}': {len(rows)} users → treatment {len(rows)-n_hold}, holdout {n_hold} ({n_hold/max(len(rows),1)*100:.0f}%)")
    print("written to ml.campaign_holdout. Send email to arm='treatment' only; measure uplift after the label window.")


if __name__ == "__main__":
    main()
