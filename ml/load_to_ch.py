#!/usr/bin/env python3
"""Load all local datasets/artifacts into the ClickHouse `ml` database as tables.

We have read-write on ml.* (role shaya_rw). Schema is inferred from each CSV
header + a sample (with name-based overrides for dates/flags/floats). Data is
streamed to CH over HTTP; .gz files are sent with Content-Encoding: gzip so the
server decompresses (no local unzip).
"""
import base64
import glob
import gzip
import os
import sys
import urllib.parse
import urllib.request

import pandas as pd

CRED = os.path.expanduser("~/.clickhouse.seedr")
BASE = "http://127.0.0.1:8123/"
DB = "ml"


def creds():
    u = p = None
    for line in open(CRED):
        k, _, v = line.strip().partition("=")
        if k == "user":
            u = v
        elif k == "password":
            p = v
    return u, p


USER, PW = creds()
AUTH = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()


def ch(sql, data=None, gz=False, params=None):
    q = {"query": sql}
    if params:
        q.update(params)
    url = BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", AUTH)
    if gz:
        req.add_header("Content-Encoding", "gzip")
    with urllib.request.urlopen(req, timeout=1200) as r:
        return r.read().decode()


def ch_type(col, dtype):
    c = col.lower()
    if col == "send_date":
        return "Date"
    if c.endswith("_at"):
        return "DateTime"
    if col == "user_id":
        return "UInt64"
    if c in ("split", "era", "country", "last_sub_event"):
        return "LowCardinality(String)"
    # float-by-name BEFORE the label_/flag check, so continuous labels
    # (label_rev_365) aren't mistaken for 0/1 flags
    if any(k in c for k in ("ltv", "amount", "rate", "share", "weight", "gb_",
                            "paid_12mo", "_usd", "score", "rev", "monetary")):
        return "Float32"
    if (c.startswith("seg_") or c.startswith("label_") or c.endswith("_observable")
            or c in ("ever_paid", "premium_at_send", "treatment", "is_premium", "target")):
        return "UInt8"
    if pd.api.types.is_integer_dtype(dtype):
        return "Int64"
    if pd.api.types.is_float_dtype(dtype):
        return "Float32"
    return "String"


def opener(path):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


def load(table, path):
    with opener(path) as f:
        sample = pd.read_csv(f, nrows=50000)
    cols = list(sample.columns)
    coldefs = ", ".join(f"`{c}` {ch_type(c, sample[c].dtype)}" for c in cols)
    order = "(user_id, send_date)" if "send_date" in cols else (
        "user_id" if "user_id" in cols else "tuple()")
    ch(f"DROP TABLE IF EXISTS {DB}.{table}")
    ch(f"CREATE TABLE {DB}.{table} ({coldefs}) ENGINE = MergeTree ORDER BY {order}")
    data = open(path, "rb").read()
    ch(f"INSERT INTO {DB}.{table} FORMAT CSVWithNames", data=data,
       gz=path.endswith(".gz"), params={"input_format_csv_empty_as_default": "1"})
    n = ch(f"SELECT count() FROM {DB}.{table}").strip()
    print(f"  ok  {DB}.{table:<34} {int(n):>10,} rows  ({len(cols)} cols)")


def main():
    jobs = [
        ("train_email_conversion", "train_email_conversion.csv.gz"),
        ("train_uplift", "train_uplift.csv.gz"),
        ("test_predictions", "ml/test_predictions.csv.gz"),
    ]
    for p in sorted(glob.glob("segments_out/2026-06-11/*.csv")):
        name = "segment_" + os.path.basename(p)[:-4].replace("-", "_")
        jobs.append((name, p))
    for p in sorted(glob.glob("ml/segments/*.csv.gz")):
        name = "segtrain_" + os.path.basename(p)[:-7].replace("seg_", "")
        jobs.append((name, p))

    print(f"loading {len(jobs)} tables into `{DB}`...")
    for table, path in jobs:
        if not os.path.exists(path):
            print(f"  skip {table} (missing {path})")
            continue
        try:
            load(table, path)
        except urllib.error.HTTPError as e:
            print(f"  FAIL {table}: {e.read().decode()[:300]}")
            sys.exit(1)


if __name__ == "__main__":
    main()
