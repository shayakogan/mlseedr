# Seedr — Content Affinity (storage/file metadata)

2026-06-19. New data source: the Seedr **admin FS API** gives each user's storage
+ file metadata — the missing "what do they actually store/stream" signal that
telemetry lacked (and that the 106-agent research flagged as a real conversion axis).
Code: `ml/content_ingest.py` → `ml.user_content`.

## API
`GET https://v2.seedr.cc/api/v0.1/admin/fs/v1/user/{user_id}/tree`
Bearer service token (works for all users) — stored in `~/.seedr_api` (NOT in git/code).

Returns `{user{...}, root{...}, fs{...}}`:
- `user`: account meta — incl. **sensitive** fields (password hash, email, tokens) → we
  read ONLY `sign_up_stamp`, `last_sign_in_stamp`, `bandwidth_used`.
- `root.size`: total storage used (bytes).
- `fs`: the files — **may be a dict (keyed by id) OR a list** (varies by user; both handled).
  Each node: `size`, `last_update`, `relative_path`/`title` (→ extension only), hash, server.

### Recon findings
- No pagination/limit params and no lighter endpoint (404). `/tree` is all-or-nothing.
- **Heavy libraries time out** (0 bytes even at 25s; e.g. user 39) → cannot be fetched.
- Sample of 25 random payers: 25/25 ok, rich variety (epub/pdf readers, mkv streamers,
  mp3 collectors, dmg/iso software, DVD rips).

## Safety (no overload)
Per request: **12 s timeout + 12 MB byte cap**; users that exceed → `content_status='too_large'`
(flagged, skipped). Modest concurrency (6 workers). On 2,000 users: 96.6% ok, 1.75% too_large,
1.6% error — no overload.

## Privacy
Extract ONLY: file **extension** (→ category), size, timestamps, storage totals, account
meta (signup/last-signin/bandwidth). NEVER store file names, paths, email, password, tokens.
Data lives in `ml.user_content` (internal) — not in git.

## Features (`ml.user_content`, 28 cols)
`n_files, storage_gb, library_gb, largest_file_gb, avg_file_gb`,
counts + GB by category (`video/audio/ebook/software/archive/image/other`),
`share_video/audio/ebook/software`, `primary_category`, **`content_persona`**,
`days_since_last_add`, `bandwidth_used_gb`, `last_signin_day`, `account_age_days`, `content_status`.

Extension→category map covers video (mkv/mp4/avi/vob…), audio (mp3/m4b/flac…),
ebook (epub/pdf/mobi…), software (exe/dmg/iso…), archive (zip/rar/7z…), image, submeta (srt/nfo…).

## Personas (POC, 2,000 active payers) × value
| Persona | Users | Avg storage | Avg pred LTV |
|---|---|---|---|
| video_streamer | 1,191 (62%) | 82.7 GB | **$29.7** (highest) |
| empty (paid, no content) | 436 (22%) | 0.4 GB | $12.4 (at-risk signal) |
| music_audio | 140 | 11.1 GB | $25.1 |
| software_downloader | 59 | 20.8 GB | $19.4 |
| archive_hoarder | 40 | 38.8 GB | $21.4 |
| reader | 24 | 3.3 GB | $17.5 |
| image_store | 8 | 3.4 GB | $4.4 |

→ Content persona correlates with value; an **uncorrelated new signal** vs monetary history.

## Why this matters / next steps
1. **Break the conversion ceiling (0.95):** add content features (esp. `has video they can't
   stream in HD on free`, `storage_used_pct`) to the conversion model — pure Usage-PQL signals.
2. **Content-aware campaigns:** HD-upsell to video_streamers; storage-pressure when near quota;
   win-back "your library is still here"; persona-specific messaging.
3. **Enrich churn/LTV:** library freshness (`days_since_last_add`), `last_signin_day` (login
   recency — not in telemetry!), bandwidth.
4. **Scale:** run on the full active base (caps make it safe); heavy users flagged `too_large`.

## Reproduce
```bash
# token in ~/.seedr_api (token=..., base=https://v2.seedr.cc/api/v0.1/admin/fs/v1)
.venv/bin/python ml/content_ingest.py -n 2000 -workers 6   # → content_features.csv.gz → ml.user_content
```

## Lift test (2026-06-19): content features in the models
Joined content onto churn/LTV datasets, compared GBM with vs without content
(user-disjoint / time split, identical rows). Code: `ml/churn_lift.py`, `ml/ltv_lift.py`.

| Model | Metric | no content | + content | lift |
|---|---|---|---|---|
| Churn | ROC-AUC | 0.787 | 0.797 | **+0.010** |
| LTV | P(pay) AUC | 0.799 | 0.799 | ~0 |
| LTV | Spearman (revenue rank) | 0.684 | 0.699 | **+0.016** |

**Honest verdict: small positive lift, not a breakthrough.** Reasons: (1) content is a
NOW snapshot vs historical labels (mild leak blunts it); (2) content correlates with
signals the models already use (heavy storage ≈ heavy bandwidth/engagement); (3) small
overlap samples add noise. The real value of content is NOT marginal retro-AUC but:
current scoring/personas, content-aware campaigns (HD-upsell, storage-pressure, win-back),
and new signals absent from telemetry (`last_signin_day`, `days_since_last_add`) — which
will lift models properly once trained on LIVE content snapshots (no temporal mismatch).
