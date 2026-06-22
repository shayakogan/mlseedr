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
| `ml.user_storage_quota` (1c ✅) | 13,576 | **storage_used_pct** (12,643 / 93%), plan_gb, plan_name, used_gb |

### Phase 1c — storage quota (resolved)
The admin FS API exposes only **used** bytes (`root.size`), **no per-user cap** — every
endpoint other than `/user/{id}/tree` 404s, and `user`/`root` objects carry no
space_max/package field (verified on free + premium users). So the cap comes from the
**plan**: `ml.plan_storage` maps `billing_plan_id`→GB (`ml/plan_storage_seed.sql`),
derived from the revenue_facts price catalog × Seedr public pricing —
Basic $6.95→30 GB, Pro $9.95→100 GB, Master $19.95→1 TB (free base 2 GB). Plan ids
1/2,3/4,5/6 cover ~93% of the base; the rest (regional/legacy ids) await a billing map.
`storage_used_pct = used_gb / plan_gb` → **1,604 users ≥80% of cap** (900 at 80–100%,
704 over) = the storage-pressure upsell audience.

## Phase 2 — `ml.customer_360` (13,576 payers)
One row/user joining LTV (value) × churn (risk) × billing × content × edge × tasks,
with `lifecycle_stage` and **`next_best_action`** (value×risk×content×billing logic).
> ⚠️ Build note: ClickHouse LEFT JOIN fills unmatched rows with the type DEFAULT (0),
> not NULL — must build with `SETTINGS join_use_nulls=1` or ifNull() defaults misfire
> (caught a false 10.8K `fix_payment`).

**next_best_action distribution:**
| Action | Users | Avg LTV | Play |
|---|---|---|---|
| monitor | 7,000 | $12 | low value, no action |
| reactivate_empty | 2,980 | $13 | paid but empty library → "your library is waiting" |
| **hd_upsell** | 1,128 | $47 | video_streamer hitting 429/stalls → "watch in HD/4K" |
| **storage_upsell** | 1,115 | $32 | ≥80% of storage cap (avg 107%), not yet Master → "upgrade for more space" |
| **vip_nurture** | 889 | $117 | high value, low risk → loyalty |
| fix_payment | 393 | $30 | grace / no-payment-method+expiring → "update card" |
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
`user_billing_health` · `user_edge` · `user_content` · `user_storage_quota` ·
`plan_storage` · `ltv_scores` · `churn_scores` · `retention_priority` + training datasets.

## Uplift measurement (`ml/measure_uplift.sql`)
Single CH query: conv(treatment) − conv(holdout) + ARPU + two-proportion z-test verdict
for a campaign, from `ml.campaign_holdout` × `seedr_telemetry.revenue_facts`. Run before a
send → baseline arm balance (should be ~0 / not_significant); after send + window → causal uplift.

## Remaining / next
- Map the remaining ~7% `billing_plan_id`→GB (regional/legacy/Gold-Power) in `ml.plan_storage` (billing team).
- Run a campaign on `arm='treatment'`, then measure the first real uplift (`ml/measure_uplift.sql`).
- After ~30 days of snapshots: train churn/LTV on live snapshots for a real (not retro) content/edge lift.
