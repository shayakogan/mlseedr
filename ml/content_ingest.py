#!/usr/bin/env python3
"""Ingest content metadata from the Seedr admin FS API → content-affinity features.

Privacy: extracts ONLY file extension + size + timestamps and account-level
storage meta. NEVER stores file names, paths, email, password hashes or tokens.

Safety (the /tree endpoint has no pagination and times out on huge libraries):
per request we cap TIME and BYTES; users that exceed → content_status='too_large'
(flagged, skipped) so we never overload. Modest concurrency. `fs` may be a dict
or a list — both handled.

Output: content_features.csv.gz (one row/user) → load to ml.user_content.
"""
import argparse
import base64
import functools
import gzip
import io
import os
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

print = functools.partial(print, flush=True)
EPOCH = date(1970, 1, 1)
TODAY = date(2026, 6, 19)
MAX_BYTES = 12_000_000
TIMEOUT = 12

CFG = {}
for line in open(os.path.expanduser("~/.seedr_api")):
    k, _, v = line.strip().partition("=")
    CFG[k] = v
TOKEN, BASE = CFG["token"], CFG["base"]

EXT_CAT = {}
for cat, exts in {
    "video": "mkv mp4 avi vob m4v wmv mov flv mpg mpeg ts m2ts webm divx",
    "audio": "mp3 m4a m4b flac aac wav ogg opus wma",
    "ebook": "epub pdf mobi azw azw3 djvu fb2 cbz cbr",
    "software": "exe dmg iso pkg apk msi bin deb appimage",
    "archive": "zip rar 7z tar gz bz2",
    "image": "jpg jpeg png gif bmp webp heic",
    "submeta": "srt sub idx nfo ifo bup sfv vtt",
}.items():
    for e in exts.split():
        EXT_CAT[e] = cat


def ch_creds():
    u = p = None
    for line in open(os.path.expanduser("~/.clickhouse.seedr")):
        k, _, v = line.strip().partition("=")
        if k == "user": u = v
        elif k == "password": p = v
    return u, p


def get_user_ids(n, source):
    u, p = ch_creds()
    auth = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
    sql = f"SELECT user_id FROM ml.{source} ORDER BY cityHash64(user_id) LIMIT {n} FORMAT TSV"
    req = urllib.request.Request("http://127.0.0.1:8123/", data=sql.encode())
    req.add_header("Authorization", auth)
    return [int(x) for x in urllib.request.urlopen(req, timeout=60).read().decode().split()]


import threading
import time as _time
_RL_LOCK = threading.Lock()
_RL_NEXT = [0.0]
RATE = float(os.environ.get("SEEDR_RATE", "5"))  # steady requests/sec (avoid bursts → no 429)


def _throttle():
    """Global token-bucket: spread request starts evenly at <= RATE/s across all
    threads, so we never burst and trip the API rate limiter."""
    with _RL_LOCK:
        now = _time.monotonic()
        wait = _RL_NEXT[0] - now
        if wait > 0:
            _time.sleep(wait)
            now = _time.monotonic()
        _RL_NEXT[0] = now + 1.0 / RATE


def fetch(uid, retries=4):
    """Return raw JSON bytes or status str. Byte-capped + steady rate-limit +
    retry/backoff on 429 / 5xx / connection errors."""
    import random
    import time
    for attempt in range(retries):
        _throttle()
        req = urllib.request.Request(f"{BASE}/user/{uid}/tree")
        req.add_header("Authorization", f"Bearer {TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                buf = io.BytesIO()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if buf.tell() > MAX_BYTES:
                        return "too_large"
                return buf.getvalue()
        except (socket.timeout, TimeoutError):
            return "too_large"
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt + random.random())
                continue
            return "error"
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.random())
                continue
            return "error"
    return "error"


def to_day(s):
    try:
        return (datetime.strptime(str(s)[:10], "%Y-%m-%d").date() - EPOCH).days
    except Exception:
        return None


def features(uid, raw):
    import json
    status = raw if isinstance(raw, str) else "ok"
    if status != "ok":
        return {"user_id": uid, "content_status": status, "n_files": 0}
    try:
        d = json.loads(raw)
    except Exception:
        return {"user_id": uid, "content_status": "error", "n_files": 0}
    fs = d.get("fs", {})
    nodes = list(fs.values()) if isinstance(fs, dict) else (fs if isinstance(fs, list) else [])
    user = d.get("user", {}) or {}
    root = d.get("root", {}) or {}
    cat_n = {c: 0 for c in set(EXT_CAT.values()) | {"other"}}
    cat_gb = {c: 0.0 for c in cat_n}
    today_d = (TODAY - EPOCH).days
    n = 0; total = 0; largest = 0; last_day = None; first_day = None
    n_lost = 0; n_add_30 = 0; n_add_90 = 0
    folders = set()
    for nd in nodes:
        if not isinstance(nd, dict):
            continue
        sz = nd.get("size") or 0
        rp = nd.get("relative_path") or nd.get("title") or ""
        if not isinstance(rp, str):
            continue
        ext = os.path.splitext(rp)[1].lower().lstrip(".")[:8]
        cat = EXT_CAT.get(ext, "other")
        n += 1; total += sz; largest = max(largest, sz)
        cat_n[cat] += 1; cat_gb[cat] += sz / 1e9
        if nd.get("is_lost"):
            n_lost += 1
        if nd.get("folder_id") is not None:
            folders.add(nd.get("folder_id"))
        ld = to_day(nd.get("last_update"))
        if ld:
            if last_day is None or ld > last_day:
                last_day = ld
            if first_day is None or ld < first_day:
                first_day = ld
            if today_d - ld <= 30:
                n_add_30 += 1
            if today_d - ld <= 90:
                n_add_90 += 1
    storage_gb = (root.get("size") or 0) / 1e9
    days_since_add = today_d - last_day if last_day else -1
    library_age_days = today_d - first_day if first_day else -1
    # primary content category by GB (value-weighted), fallback by count
    if total > 0:
        primary = max(cat_gb, key=cat_gb.get)
    elif n > 0:
        primary = max(cat_n, key=cat_n.get)
    else:
        primary = "none"
    share = {c: round(cat_gb[c] / storage_gb, 3) if storage_gb > 0 else 0 for c in cat_n}
    persona = "empty" if n == 0 else {
        "video": "video_streamer", "audio": "music_audio", "ebook": "reader",
        "software": "software_downloader", "archive": "archive_hoarder",
        "image": "image_store", "submeta": "mixed", "other": "mixed",
    }.get(primary, "mixed")
    return {
        "user_id": uid, "content_status": "ok", "n_files": n,
        "storage_gb": round(storage_gb, 3), "library_gb": round(total / 1e9, 3),
        "largest_file_gb": round(largest / 1e9, 3),
        "avg_file_gb": round(total / 1e9 / n, 4) if n else 0,
        "n_video": cat_n["video"], "n_audio": cat_n["audio"], "n_ebook": cat_n["ebook"],
        "n_software": cat_n["software"], "n_archive": cat_n["archive"],
        "n_image": cat_n["image"], "n_other": cat_n["other"] + cat_n["submeta"],
        "gb_video": round(cat_gb["video"], 2), "gb_audio": round(cat_gb["audio"], 2),
        "gb_ebook": round(cat_gb["ebook"], 2), "gb_software": round(cat_gb["software"], 2),
        "share_video": share["video"], "share_audio": share["audio"],
        "share_ebook": share["ebook"], "share_software": share["software"],
        "primary_category": primary, "content_persona": persona,
        "days_since_last_add": days_since_add,
        "bandwidth_used_gb": round((user.get("bandwidth_used") or 0) / 1e9, 3),
        "last_signin_day": to_day(datetime.utcfromtimestamp(user["last_sign_in_stamp"]).isoformat()) if user.get("last_sign_in_stamp") else -1,
        "account_age_days": ((TODAY - EPOCH).days - (user["sign_up_stamp"] // 86400)) if user.get("sign_up_stamp") else -1,
        # --- new features (same /tree call, free) ---
        "gb_archive": round(cat_gb["archive"], 2), "gb_image": round(cat_gb["image"], 2),
        "n_folders": len(folders), "n_lost_files": n_lost,
        "files_added_30d": n_add_30, "files_added_90d": n_add_90,
        "library_age_days": library_age_days,
        "saw_walkthrough": int(user.get("saw_walkthrough") or 0),
        "is_business": int((user.get("org_id") or 0) > 0),
        "is_referred": int((user.get("invited_by") or 0) > 0),
        "has_sso": int(bool(user.get("sso_provider"))),
        "lost_pw_requests": int(user.get("lost_password_request") or 0),
        "totp_enabled": int(user.get("totp_enabled") or 0),
        "invites_accepted": int(user.get("invites_accepted_counter") or 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2000)
    ap.add_argument("-source", default="ltv_scores")
    ap.add_argument("-workers", type=int, default=4)
    ap.add_argument("-out", default="content_features.csv.gz")
    ap.add_argument("-resume", default="", help="existing csv.gz: keep its ok rows, only (re)fetch the rest")
    a = ap.parse_args()

    uids = get_user_ids(a.n, a.source)
    rows = []
    if a.resume and os.path.exists(a.resume):
        import pandas as pd
        prev = pd.read_csv(a.resume)
        ok_prev = prev[prev.content_status == "ok"]
        ok_ids = set(ok_prev.user_id)
        rows = ok_prev.to_dict("records")
        uids = [u for u in uids if u not in ok_ids]
        print(f"resume: kept {len(ok_ids):,} ok rows, re-fetching {len(uids):,} failed/missing")
    print(f"fetching content for {len(uids):,} users ({a.workers} workers, retry+backoff, {TIMEOUT}s/{MAX_BYTES//1_000_000}MB cap)...")
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for feat in ex.map(lambda u: features(u, fetch(u)), uids):
            rows.append(feat); done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(uids)}")

    import csv
    cols = ["user_id", "content_status", "n_files", "storage_gb", "library_gb",
            "largest_file_gb", "avg_file_gb", "n_video", "n_audio", "n_ebook",
            "n_software", "n_archive", "n_image", "n_other", "gb_video", "gb_audio",
            "gb_ebook", "gb_software", "share_video", "share_audio", "share_ebook",
            "share_software", "primary_category", "content_persona",
            "days_since_last_add", "bandwidth_used_gb", "last_signin_day", "account_age_days",
            "gb_archive", "gb_image", "n_folders", "n_lost_files", "files_added_30d",
            "files_added_90d", "library_age_days", "saw_walkthrough", "is_business",
            "is_referred", "has_sso", "lost_pw_requests", "totp_enabled", "invites_accepted"]
    with gzip.open(a.out, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    from collections import Counter
    st = Counter(r["content_status"] for r in rows)
    pers = Counter(r.get("content_persona", "n/a") for r in rows if r["content_status"] == "ok")
    print(f"\nstatus: {dict(st)}")
    print(f"personas: {dict(pers.most_common())}")
    print(f"saved {a.out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
