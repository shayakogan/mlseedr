package main

// Segment catalogue for Seedr marketing campaigns.
//
// Every query targets the ClickHouse warehouse at data.seedr.cc (read-only,
// via the SSH tunnel) and respects the data-quality rules documented in
// SEEDR_DATA_GUIDE.md / SEEDR_CLICKHOUSE_REFERENCE.md:
//   - web events spanning the 2026-05-24..27 migration filter
//     surface IN ('web','landing'), never 'landing' alone;
//   - goals 1-3 carry no user_id, so the cart-abandonment funnel is built on
//     vid and mapped to accounts through vid_user_map;
//   - "is premium" == present in user_subscription_state (the table is
//     churn-blind: churned users are absent, not flagged);
//   - streaming segments are built on the edge log (request_events), not on
//     web events, which see only ~15% of streamers;
//   - bw_user_day is AggregatingMergeTree — read with sumMerge/countMerge.
//
// Output of every query is one row per user_id so marketing can join emails
// from the central catalog MySQL (uc_users) downstream.

type Segment struct {
	Name  string // file/flag name, kebab-case
	Title string
	Why   string // one-line business rationale
	SQL   string // must NOT contain a FORMAT clause; the runner appends it
}

const premiumUsers = `SELECT user_id FROM seedr_telemetry.user_subscription_state`

var segments = []Segment{
	{
		Name:  "quota-storage-warning",
		Title: "Storage-quota pressure (Usage-PQL)",
		Why:   "Users hitting storage limits are the best-converting upgrade trigger (HubSpot Usage-PQL 3-4x); premium hitters are tier-upsell targets.",
		SQL: `
SELECT
    user_id,
    count()                 AS warnings_30d,
    max(created_at)         AS last_warning_at,
    if(user_id IN (` + premiumUsers + `), 1, 0) AS is_premium
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= now() - INTERVAL 30 DAY
  AND event_type = 'account.storage_warning'
  AND user_id IS NOT NULL
GROUP BY user_id
ORDER BY warnings_30d DESC, last_warning_at DESC`,
	},
	{
		Name:  "quota-bandwidth-free",
		Title: "Heavy-bandwidth free users (>10GB/7d)",
		Why:   "The real power users (7.9K vs 178 by legacy web-event thresholds); their usage already exceeds free-tier value — prime free-to-paid conversion.",
		SQL: `
SELECT
    user_id,
    round(sumMerge(bytes) / 1e9, 2) AS gb_7d,
    countMerge(reqs)                AS requests_7d
FROM seedr_telemetry.bw_user_day
WHERE day >= today() - 7
  AND user_id != 0
GROUP BY user_id
HAVING gb_7d >= 10
   AND user_id NOT IN (` + premiumUsers + `)
ORDER BY gb_7d DESC`,
	},
	{
		Name:  "winback-active",
		Title: "Lapsed payers still active in the product",
		Why:   "Proven willingness to pay AND demonstrated current need; pool (~3.8K) is larger than the whole current paid base. Personalize by LTV.",
		SQL: `
WITH active AS (
    SELECT user_id, max(created_at) AS last_seen_at, count() AS events_30d
    FROM seedr_telemetry.user_telemetry_events
    WHERE created_at >= now() - INTERVAL 30 DAY
      AND surface IN ('web', 'landing', 'task')
      AND user_id IS NOT NULL
    GROUP BY user_id
)
SELECT
    r.user_id                   AS user_id,
    round(sum(r.amount_usd), 2) AS ltv_usd,
    count()                     AS lifetime_txns,
    max(r.transaction_date)     AS last_txn_at,
    any(a.last_seen_at)         AS last_seen_at,
    any(a.events_30d)           AS events_30d
FROM seedr_telemetry.revenue_facts r
INNER JOIN active a ON r.user_id = a.user_id
WHERE r.status = 'completed'
  AND r.user_id NOT IN (` + premiumUsers + `)
GROUP BY r.user_id
ORDER BY ltv_usd DESC`,
	},
	{
		Name:  "soft-cancel-save",
		Title: "Cancellation scheduled, not yet churned",
		Why:   "Still paying but flagged intent to leave — the prime save window; ~30% of churn happens within a month, so speed matters.",
		SQL: `
SELECT user_id, last_event_at AS scheduled_at
FROM (
    SELECT
        user_id,
        argMax(event_type, created_at) AS last_sub_event,
        max(created_at)                AS last_event_at
    FROM seedr_telemetry.user_telemetry_events
    WHERE created_at >= now() - INTERVAL 60 DAY
      AND event_type LIKE 'subscription.%'
      AND user_id IS NOT NULL
    GROUP BY user_id
)
WHERE last_sub_event = 'subscription.cancellation_scheduled'
ORDER BY scheduled_at DESC`,
	},
	{
		Name:  "streamers-free-hd-upsell",
		Title: "Free streamers gated at 480p (edge log)",
		Why:   "Quality gating is a proven upgrade trigger (TeraBox playbook); 18K+ free streamers/week are invisible in web events — only the edge log sees them.",
		SQL: `
SELECT
    user_id,
    count()                         AS media_requests_7d,
    round(sum(bytes_sent) / 1e9, 2) AS gb_streamed_7d,
    anyLast(country)                AS country
FROM seedr_telemetry.request_events
WHERE ts >= today() - 7
  AND base_path = '/media'
  AND user_id != 0
GROUP BY user_id
HAVING media_requests_7d >= 20
   AND user_id NOT IN (` + premiumUsers + `)
ORDER BY gb_streamed_7d DESC`,
	},
	{
		Name:  "cart-abandoners",
		Title: "Entered purchase funnel, never clicked pay",
		Why:   "Highest purchase intent of any free cohort; classic abandonment drip. Built on vid (goals 1-3 carry no user_id), mapped via vid_user_map.",
		SQL: `
WITH funnel AS (
    SELECT
        vid,
        max(matomo_idgoal)                 AS furthest_goal,
        maxIf(1, matomo_idgoal IN (1, 2, 3)) AS touched_funnel,
        maxIf(1, matomo_idgoal = 4)          AS clicked_pay,
        max(created_at)                    AS last_touch_at
    FROM seedr_telemetry.user_telemetry_events
    WHERE created_at >= now() - INTERVAL 30 DAY
      AND matomo_idgoal IS NOT NULL
    GROUP BY vid
)
SELECT
    m.user_id            AS user_id,
    max(f.furthest_goal) AS furthest_goal,
    max(f.last_touch_at) AS last_touch_at,
    count()              AS funnel_vids
FROM funnel f
INNER JOIN seedr_telemetry.vid_user_map m ON f.vid = m.vid
WHERE f.touched_funnel = 1
  AND f.clicked_pay = 0
  AND m.user_id NOT IN (` + premiumUsers + `)
GROUP BY m.user_id
ORDER BY last_touch_at DESC`,
	},
	{
		Name:  "dormant-recent-payers",
		Title: "Paid within 180d, unsubscribed, silent 30d",
		Why:   "Freshest win-back inventory that left the product entirely — needs an incentive to return, unlike winback-active who still use it.",
		SQL: `
SELECT
    user_id,
    round(sum(amount_usd), 2) AS ltv_usd,
    max(transaction_date)     AS last_txn_at,
    count()                   AS lifetime_txns
FROM seedr_telemetry.revenue_facts
WHERE status = 'completed'
GROUP BY user_id
HAVING last_txn_at >= now() - INTERVAL 180 DAY
   AND user_id NOT IN (` + premiumUsers + `)
   AND user_id NOT IN (
       SELECT user_id
       FROM seedr_telemetry.user_telemetry_events
       WHERE created_at >= now() - INTERVAL 30 DAY
         AND user_id IS NOT NULL
         AND surface IN ('web', 'landing', 'task'))
ORDER BY ltv_usd DESC`,
	},
	{
		Name:  "monthly-to-annual",
		Title: "Loyal monthly payers — annual-plan switch",
		Why:   "Annual plans retain ~2.5x better than monthly (RevenueCat); 3+ renewals prove commitment, so the annual offer is credible and protects LTV.",
		SQL: `
SELECT
    s.user_id              AS user_id,
    s.current_plan_id      AS current_plan_id,
    r.last_amount_usd      AS last_amount_usd,
    r.txns_12mo            AS txns_12mo,
    round(r.paid_12mo, 2)  AS paid_12mo_usd
FROM seedr_telemetry.user_subscription_state s
INNER JOIN (
    SELECT
        user_id,
        argMax(amount_usd, transaction_date)                    AS last_amount_usd,
        countIf(transaction_date >= now() - INTERVAL 365 DAY)   AS txns_12mo,
        sumIf(amount_usd, transaction_date >= now() - INTERVAL 365 DAY) AS paid_12mo
    FROM seedr_telemetry.revenue_facts
    WHERE status = 'completed'
    GROUP BY user_id
) r ON s.user_id = r.user_id
WHERE r.last_amount_usd < 30
  AND r.txns_12mo >= 3
ORDER BY paid_12mo_usd DESC`,
	},
}
