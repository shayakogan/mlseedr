#!/usr/bin/env bash
# Daily refresh of CH-derived features + customer_360 + append a dated history
# snapshot (for live-snapshot training later — fixes the now-vs-history mismatch).
#
# Rebuilds the CH-only tables (edge/tasks/billing — no slow external API) and
# customer_360, then appends today's snapshot to ml.customer_360_history.
# Content (ml.user_content) is refreshed separately/weekly (slow rate-limited API).
#
# Requires the SSH tunnel to data.seedr.cc:8123 up (use autossh for cron).
# Cron example:  0 3 * * *  /home/shaya/go/src/helloworld/mlshaya_segments/ml/snapshot_daily.sh >> /tmp/seedr_snapshot.log 2>&1
set -euo pipefail
CRED="$HOME/.clickhouse.seedr"
U=$(grep ^user= "$CRED" | cut -d= -f2); P=$(grep ^password= "$CRED" | cut -d= -f2)
CH() { curl -sS -m 600 "http://127.0.0.1:8123/?$1" --user "$U:$P" --data-binary "$2"; }

curl -sS -m 8 http://127.0.0.1:8123/ping >/dev/null || { echo "$(date -u) ABORT: CH tunnel down"; exit 1; }
echo "$(date -u) refresh start"

CH "" "CREATE OR REPLACE TABLE ml.user_edge ENGINE=MergeTree ORDER BY user_id AS
SELECT user_id, count() AS edge_requests_7d, round(sum(bytes_sent)/1e9,3) AS edge_gb_7d,
  countIf(base_path='/media') AS media_requests_7d, round(sumIf(bytes_sent,base_path='/media')/1e9,3) AS stream_gb_7d,
  countIf(status=429) AS n_rate_limited_7d, countIf(is_stall) AS n_stall_7d, countIf(status>=500) AS n_errors_7d,
  uniqExact(file_id) AS distinct_files_7d, uniqExact(toDate(ts)) AS active_days_7d,
  round(countIf(proto='h3')/count(),3) AS h3_share, countIf(base_path='/app') AS app_requests_7d, uniqExact(country) AS n_countries
FROM seedr_telemetry.request_events WHERE ts>=today()-7 AND user_id!=0 GROUP BY user_id"

CH "" "CREATE OR REPLACE TABLE ml.user_tasks ENGINE=MergeTree ORDER BY user_id AS
SELECT assumeNotNull(user_id) AS user_id, countIf(event_type='task.completed') AS downloads_30d,
  countIf(event_type='task.failed') AS tasks_failed_30d,
  round(countIf(event_type='task.failed')/greatest(count(),1),3) AS task_failure_rate,
  uniqExact(toDate(created_at)) AS active_task_days_30d, dateDiff('day',max(created_at),now()) AS days_since_last_task
FROM seedr_telemetry.user_telemetry_events WHERE event_type LIKE 'task.%' AND created_at>=now()-INTERVAL 30 DAY AND user_id IS NOT NULL GROUP BY user_id"

CH "" "CREATE OR REPLACE TABLE ml.user_billing_health ENGINE=MergeTree ORDER BY user_id AS
SELECT toUInt64(uid) AS user_id, toUInt8(countIf(action='GRACE_WATCH')>0) AS in_grace_period,
  toUInt8(countIf(action='RECONCILIATION_CRITICAL')>0) AS reconciliation_critical,
  countIf(level='error') AS billing_errors_30d, dateDiff('day',max(ts),now()) AS days_since_billing_event
FROM payments.payment_app_events WHERE ts>=now()-INTERVAL 45 DAY AND uid!=0 GROUP BY uid"

CH "join_use_nulls=1" "CREATE OR REPLACE TABLE ml.customer_360 ENGINE=MergeTree ORDER BY user_id AS
SELECT l.user_id AS user_id, l.value_tier AS value_tier, l.pred_ltv_12m AS pred_ltv_12m, l.ltv_decile AS ltv_decile,
  l.country AS country, l.is_annual AS is_annual, l.recency_days AS recency_days,
  ifNull(ch.churn_risk_30d,-1) AS churn_risk_30d, ifNull(ch.has_payment_method,-1) AS has_payment_method,
  ifNull(ch.days_to_expires,-999) AS days_to_expires, ifNull(ch.had_cancel_sched,0) AS had_cancel_sched,
  ifNull(bh.in_grace_period,0) AS in_grace_period, ifNull(bh.reconciliation_critical,0) AS reconciliation_critical,
  ifNull(co.content_persona,'unknown') AS content_persona, ifNull(co.storage_gb,-1) AS storage_gb,
  ifNull(co.files_added_30d,0) AS files_added_30d, ifNull(co.n_lost_files,0) AS n_lost_files,
  ifNull(co.saw_walkthrough,-1) AS saw_walkthrough,
  ifNull(e.n_rate_limited_7d,0) AS n_rate_limited_7d, ifNull(e.n_stall_7d,0) AS n_stall_7d,
  ifNull(e.media_requests_7d,0) AS media_requests_7d, ifNull(e.stream_gb_7d,0) AS stream_gb_7d,
  ifNull(t.downloads_30d,0) AS downloads_30d, ifNull(t.task_failure_rate,0) AS task_failure_rate,
  if(isNotNull(ch.churn_risk_30d),'subscriber','lapsed_or_free') AS lifecycle_stage,
  multiIf(ifNull(bh.in_grace_period,0)=1 OR (ifNull(ch.has_payment_method,-1)=0 AND ifNull(ch.days_to_expires,-999) BETWEEN -7 AND 30),'fix_payment',
    ifNull(ch.churn_risk_30d,0)>=0.5 AND l.pred_ltv_12m>=50,'urgent_save',
    ifNull(co.content_persona,'')='video_streamer' AND (ifNull(e.n_rate_limited_7d,0)>0 OR ifNull(e.n_stall_7d,0)>5),'hd_upsell',
    ifNull(co.content_persona,'')='empty','reactivate_empty', l.pred_ltv_12m>=75,'vip_nurture','monitor') AS next_best_action
FROM ml.ltv_scores l
LEFT JOIN ml.churn_scores ch ON l.user_id=ch.user_id
LEFT JOIN ml.user_billing_health bh ON l.user_id=bh.user_id
LEFT JOIN ml.user_content co ON l.user_id=co.user_id
LEFT JOIN ml.user_edge e ON l.user_id=e.user_id
LEFT JOIN ml.user_tasks t ON l.user_id=t.user_id"

CH "" "CREATE TABLE IF NOT EXISTS ml.customer_360_history ENGINE=MergeTree PARTITION BY snapshot_date ORDER BY (user_id, snapshot_date) AS SELECT today() AS snapshot_date, * FROM ml.customer_360 LIMIT 0"
TODAY=$(date -u +%F)
CH "" "ALTER TABLE ml.customer_360_history DROP PARTITION '$TODAY'"  # idempotent for re-runs
CH "" "INSERT INTO ml.customer_360_history SELECT today() AS snapshot_date, * FROM ml.customer_360"
echo "$(date -u) refresh done: $(CH '' 'SELECT count() FROM ml.customer_360' )"
