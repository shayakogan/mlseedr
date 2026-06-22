# Seedr — Customer-360 & retention engine (all phases)

2026-06-22. Unifies every model + signal into one per-user profile with a
`next_best_action`, plus daily snapshots, campaign audiences and an uplift-holdout
framework. Tables in ClickHouse `ml`. Code: `ml/snapshot_daily.sh`,
`ml/assign_holdout.py`; feature builders `content_ingest.py`, `*_score.py`.

## Phase 1 — new CH feature tables
| Table | Users | Key features |
|---|---|---|
| `ml.user_tasks` | 171,675 | downloads_30d, tasks_failed_30d, **task_failure_rate** (frustration), active_task_days, days_since_last_task |
| `ml.user_billing_health` | 18,323 | **in_grace_period** (dunning, 231), **reconciliation_critical** (257), billing_errors_30d |
| `ml.user_edge` (from request_events 7d) | 92,060 | media_requests/stream_gb (streaming), **n_rate_limited** (429 quota-pressure, 14.5K), **n_stall** (QoE, 58K), edge_gb, distinct_files |
| `ml.user_content` (FS API) | 13,576 | content_persona, storage_gb + (enriching to 42 cols: n_lost_files, files_added_30d, saw_walkthrough, …) |
| (deferred 1c) storage_used_pct | — | needs quota from plan / `/dynamic/get_space` |

## Phase 2 — `ml.customer_360` (13,576 payers)
One row/user joining LTV (value) × churn (risk) × billing × content × edge × tasks,
with `lifecycle_stage` and **`next_best_action`** (value×risk×content×billing logic).
> ⚠️ Build note: ClickHouse LEFT JOIN fills unmatched rows with the type DEFAULT (0),
> not NULL — must build with `SETTINGS join_use_nulls=1` or ifNull() defaults misfire
> (caught a false 10.8K `fix_payment`).

**next_best_action distribution:**
| Action | Users | Avg LTV | Play |
|---|---|---|---|
| monitor | 7,913 | $13 | low value, no action |
| reactivate_empty | 3,019 | $12 | paid but empty library → "your library is waiting" |
| **hd_upsell** | 1,145 | $48 | video_streamer hitting 429/stalls → "watch in HD/4K" |
| **vip_nurture** | 1,061 | $113 | high value, low risk → loyalty |
| fix_payment | 388 | $29 | grace / no-payment-method+expiring → "update card" |
| urgent_save | 71 | $79 | high value + high churn risk |

## Phase 3a — daily snapshots (`ml/snapshot_daily.sh` → `ml.customer_360_history`)
Rebuilds edge/tasks/billing + customer_360, appends a dated snapshot (idempotent per
day). First snapshot: 2026-06-22 (13,576). **Why:** accumulate a time series so models
can later train on LIVE snapshots (fixes the now-vs-history mismatch that limited the
content lift to +0.01). Schedule (crontab unavailable in this env — install on a host
with a persistent tunnel / autossh):
```
30 3 * * *  /…/ml/snapshot_daily.sh >> /tmp/seedr_snapshot.log 2>&1
```
Content (`ml.user_content`) refresh is separate/weekly (slow rate-limited FS API).

## Phase 3b — uplift holdout (`ml/assign_holdout.py` → `ml.campaign_holdout`)
Deterministically assigns a persistent ~10% control per campaign (hash on user_id+campaign).
Assigned: hd_upsell (1027 treat / 118 hold), reactivate_empty (2745 / 274). Send email to
`arm='treatment'` only; after the label window measure **uplift = conv(treatment) −
conv(holdout)** → the causal effect (not propensity). Bridge to bandit/RL targeting.

## Phase 4 — campaign audiences (`segments/campaigns/*.csv.gz`)
Per-action lists exported from customer_360: fix_payment (388), urgent_save (71),
hd_upsell (1,145), reactivate_empty (3,019), vip_nurture (1,061). Refresh = re-query
`ml.customer_360 WHERE next_best_action='…'`. PII → ClickHouse / internal only.

## Tables in `ml`
`customer_360` · `customer_360_history` · `campaign_holdout` · `user_tasks` ·
`user_billing_health` · `user_edge` · `user_content` · `ltv_scores` · `churn_scores` ·
`retention_priority` + training datasets.

## Remaining / next
- Finish enriched `ml.user_content` (42 cols) re-ingest → rebuild customer_360 with new content features.
- Storage quota → `storage_used_pct` (1c).
- Run a campaign on `arm='treatment'`, then measure the first real uplift.
- After ~30 days of snapshots: train churn/LTV on live snapshots for a real (not retro) content/edge lift.
