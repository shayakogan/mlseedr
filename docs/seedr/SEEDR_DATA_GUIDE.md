# Seedr Data Warehouse — Practical Guide

A starter handbook for new projects that need to read Seedr's telemetry data from ClickHouse (`data.seedr.cc`).

**Audience:** developers starting a new tool/service that consumes Seedr user/event/payment data.
**Scope:** what's in the warehouse, how to connect, what to query, what to watch out for.
**Last verified:** 2026-06-10 (multi-agent run — see `SEEDR_DATA_VERIFICATION_2026-06-10.md`).
**Full table inventory** (edge logs, rollups, payments DB): `SEEDR_CLICKHOUSE_REFERENCE.md`.

---

## 1. The 30-second version

* **ClickHouse is the primary data source for everything analytical.** All other systems (Matomo, Mautic, Partytime billing, the central catalog MySQL) are *upstream*: they emit events that fan into CH.
* **One ClickHouse server** at `data.seedr.cc`, **two databases**: `seedr_telemetry` (23 tables + 15 MVs — this guide covers the 4 core analytical ones) and `payments` (billing-service observability). Full inventory: `SEEDR_CLICKHOUSE_REFERENCE.md`.
* **Web analytics, email, subscriptions, payments** — all unified into `user_telemetry_events` (~146M rows, growing ~650K/day).
* **Identity:** join on `user_id` (UInt64, = Seedr `uc_users.id`) when the user is logged in. Anonymous users get a `vid` (UUID, mostly v5).
* **History window:** 2025-05-27 → present for web events. Older history exists for revenue (back to 2016) and mautic email (2025-01-10 → 2026-05-28, stream ended) — see §5.
* **Access (verified 2026-06-16):** `readonly=0`, role `shaya_rw`. **SELECT on everything**, but **write/DDL (INSERT, CREATE, ALTER, DROP, TRUNCATE…) only in the `ml.*` and `shaya.*` databases** — `seedr_telemetry` and `payments` have no write grant (effectively read-only). So model outputs / training tables CAN be persisted to CH directly (in `ml`/`shaya`), just not into production telemetry. (Earlier docs said "read-only, no writes" — that was wrong.)

---

## 2. Connecting

### 2.1 SSH tunnel (keep running in a terminal)

```bash
ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    shaya@data.seedr.cc \
    -L 8123:127.0.0.1:8123 \
    -L 9000:127.0.0.1:9000 \
    -L 13306:127.0.0.1:3306
```

- `8123` — ClickHouse HTTP (most flexible, use this by default)
- `9000` — ClickHouse native protocol (use with `clickhouse-client`)
- `13306` — MySQL on data.seedr.cc (red_data / agent / stat / pulse — separate from `seedr_telemetry`)

`13306` is remapped from the remote 3306 because a local MySQL might already occupy 3306. Adjust if you don't have a local MySQL.

The SSH login is **tunnel-only** — no shell. If the tunnel drops, restart it.

### 2.2 Credentials

Stored in a chmod-600 file:

```bash
# ~/.clickhouse.seedr
user=shaya
password=<sent separately>
host=127.0.0.1
http_port=8123
native_port=9000
database=seedr_telemetry
```

Never commit this file. The MySQL on data.seedr.cc uses a separate password — put in `~/.my.cnf.seedr-data`.

### 2.3 First query

```bash
# HTTP
CH_USER=$(grep ^user= ~/.clickhouse.seedr | cut -d= -f2)
CH_PW=$(grep ^password= ~/.clickhouse.seedr | cut -d= -f2)
curl -sS "http://127.0.0.1:8123/" --user "${CH_USER}:${CH_PW}" \
  --data-binary "SELECT version(), now() FORMAT TabSeparated"

# Native client
clickhouse-client --host 127.0.0.1 --port 9000 \
  --user shaya --password '<pw>' --database seedr_telemetry
```

Expect ~300ms round-trip for a trivial query (tunnel overhead, not CH).

---

## 3. Database structure

`seedr_telemetry` holds **23 base tables + 15 materialized views** (verified 2026-06-10). This guide details the 4 **user-analytics core** tables; the rest — the raw edge/QoS log `request_events` (~187M rows, TTL 90d), 13 minute/hour/day rollups, `node_health`, the new ops logs (`mysql_error_events`, `mysql_slow_events`, `playback_events`) and the separate `payments` database — are documented in `SEEDR_CLICKHOUSE_REFERENCE.md`.

| Table | Rows (06-10) | Size | Purpose |
|---|---|---|---|
| **`user_telemetry_events`** | 145.7M | 4.7 GiB | The main event stream — pageviews, events, goals, emails, subscriptions |
| **`vid_user_map`** | 3.1M | 131 MiB | Mapping `vid` (UUID) → `user_id`; **838K distinct users** (a user averages ~3.7 vids) |
| **`user_subscription_state`** | ~3.2K | 41 KiB | Current subscription state per user (latest plan, last lifecycle event). ⚠️ churn-blind — see §8 |
| **`revenue_facts`** | 280K | 11 MiB | Per-transaction revenue history back to **2016** (provider, amount, currency, status) |
| `vid_user_map_mv`, `user_subscription_state_mv` | — | — | Materialized views feeding the two state tables from `user_telemetry_events` |

The 4 core tables use `ReplacingMergeTree(version)` — deduped by `ORDER BY` key, latest `version` wins. (The other 19 do NOT: rollups are `AggregatingMergeTree`, raw logs are plain `MergeTree`.)

---

## 4. `user_telemetry_events` — the main table

### 4.1 Schema (24 columns)

```sql
vid              FixedString(36)        -- visitor UUIDv4; primary identity
user_id          Nullable(UInt64)       -- Seedr user_id (= uc_users.id) when logged in
event_type       LowCardinality(String) -- pageview / event / goal / signup / heartbeat / ...
surface          LowCardinality(String) -- web (pre-2026-05-24) / landing / email / subscription / task / ...
                                        -- ⚠️ web events: 'web' before the 2026-05-24..27 migration, 'landing' after — see §10
slot_key         LowCardinality(String) -- A/B test slot
variant          LowCardinality(String) -- A/B test variant
url              String CODEC(ZSTD(3))  -- full URL or empty
referrer         String CODEC(ZSTD(3))
ip_v4            Nullable(IPv4)         -- only one of v4/v6 populated
ip_v6            Nullable(IPv6)
ua               String CODEC(ZSTD(3))
country          LowCardinality(String) -- ISO-3166 alpha-2 ('us','in','de',...)
matomo_idgoal    Nullable(UInt16)       -- 1..9, see §6
matomo_ec_id     String                 -- ecommerce order id (rare)
revenue_cents    Nullable(Int64)        -- transaction revenue in cents (no float rounding)
category         LowCardinality(String) -- event category (e.g. 'File', 'i18n')
action           LowCardinality(String) -- event action (e.g. 'View', 'Download')
name             String                 -- event name (free-text)
value            Nullable(Int64)        -- event value
metadata         String CODEC(ZSTD(3))  -- JSON blob; source-specific fields (see §5)
idempotency_key  String                 -- unique per logical event; dedup key
version          UInt32                 -- ReplacingMergeTree version
created_at       DateTime               -- event time (UTC)
received_at      DateTime               -- ingestion time (UTC)

-- Secondary indexes
INDEX idx_vid vid TYPE bloom_filter GRANULARITY 1
INDEX idx_uid user_id TYPE bloom_filter GRANULARITY 1

ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(created_at)
ORDER BY idempotency_key
TTL created_at + INTERVAL 24 MONTH       -- heartbeat: 30 days
```

### 4.2 What to read

| Query pattern | Notes |
|---|---|
| `WHERE user_id = X` | Hits `idx_uid` bloom filter → fast even on full window |
| `WHERE vid = '...'` | Hits `idx_vid` bloom filter |
| `WHERE created_at >= 'YYYY-MM-DD'` | Hits partition pruning — always include when possible |
| `WHERE event_type IN (...)` | LowCardinality, very cheap |
| `JSONExtractString(metadata, 'key') = ...` | **AVOID as a filter** on full window — JSON parse on every row, no index |
| `GROUP BY toYYYYMM(created_at)` | Hits partition pruning naturally |
| `count()` over wide range | Uses metadata only — sub-second |

### 4.3 Critical timezone note

- `created_at` and `received_at` in CH are **UTC**.
- Matomo's `actionDetails[].timestamp` field for the SAME event is **`Asia/Jerusalem` local time formatted as Unix epoch** (a known Matomo quirk for site_id=2).
- To cross-join Matomo Live API timestamps with CH: subtract **10800 sec in summer (IDT, UTC+3) / 7200 sec in winter (IST, UTC+2)**, OR use proper timezone conversion.

```python
from datetime import datetime
from zoneinfo import ZoneInfo
def matomo_ts_to_utc(mts):
    naive = datetime.utcfromtimestamp(mts)
    jer = naive.replace(tzinfo=ZoneInfo('Asia/Jerusalem'))
    return int(jer.astimezone(ZoneInfo('UTC')).timestamp())
```

Forgetting this gives **0% match rate** in cross-source joins. Don't skip.

---

## 5. The four data sources (`metadata.src`)

`user_telemetry_events` is a union of streams tagged via `metadata.src`. Each source has different lifecycle, fields, and history depth.

```sql
SELECT JSONExtractString(metadata,'src') AS src, count()
FROM seedr_telemetry.user_telemetry_events
GROUP BY src;
```

### 5.1 `src='matomo'` (legacy tag, web/analytics) — STOPPED
- **History:** 2025-05-27 → **2026-05-27 21:35 UTC** (stopped on migration; verified dead 2026-06-10, counts frozen)
- **Volume:** 116.5M events, ~80% of total
- **Contents:** pageviews, session_end, event, heartbeat, goal, click, impression, signup (= goal 4)
- **Status:** historical only. New web events arrive with `src=''` instead.

### 5.2 `src=''` (current matomo, web/analytics)
- **History:** 2026-03-31 → now (real-time)
- **Volume:** 9.7M and growing
- **Contents:** same event taxonomy as legacy `matomo` source (pageview, event, heartbeat, session_end, goal, signup, impression, click). Carries NO email/task/subscription rows — stream isolation verified.
- **Status:** **this is where current web events live.** Queries against "current web" should use `WHERE src IN ('matomo','')` to span both eras.

### 5.3 `src='mautic'` (marketing email) — STOPPED 2026-05-28
- **History:** **2025-01-10** → **2026-05-28 14:13 UTC**. The email stream did not die — it **migrated to `src='internal_events'`** (verified: zero mautic rows after the cutoff, email.* flows real-time under internal_events).
- **Volume:** 17.66M (final): `email.sent` 14.8M / `email.opened` 2.8M / `email.clicked` 45K
- **`user_id`** is populated when known. Anonymous email recipients are rare.
- Lifetime open/click rates: 18.97% / 1.60% of opens. Real throughput was **~1.4M sends/month** (not "15M/month" — that figure conflated the lifetime cumulative with a monthly rate).

### 5.4 `src='internal_events'` (server-side telemetry — now incl. ALL email)
- **History:** 2026-01-12 → now. ⚠️ That start date holds only for `subscription.*` — per-event-type starts verified live 2026-06-11: `task.*` since **2026-05-25**, `account.storage_warning` since **2026-06-01** (earlier rows, if they ever existed, are gone). Web `category='video'` events (different stream) exist since **2026-05-16**.
- **Volume:** 1.84M and growing
- **Contents:** five categories —
  - **Background tasks** (`task.completed` ~862K, `task.failed` ~7K) — bulk of volume. Emitted by the Go subnode workers (25+ nodes) processing torrent downloads, generic URL / cloud-source downloads (Google Drive, Dropbox, Mega, TeraBox), archive extraction, and video/audio/image/document transcoding.
  - **Email — ALL of it since 2026-05-28** (`email.sent/opened/clicked`): transactional AND migrated marketing/bulk. ~824K sent per 30 days, with visible bulk-campaign batches (e.g. 278K on 05-31, 336K on 06-07). Post-migration engagement: open ~14.8%, click-of-opens ~0.6% — lower than mautic's lifetime 19%/1.6%; under investigation.
  - **Subscription lifecycle** (`subscription.created/canceled/reactivated/billing_plan_change/payment_failed/refund_processed/chargeback_opened/cancellation_scheduled/grace_period_entered` + **`subscription.expired`, new since 2026-06-07**) — the billing state machine from **Partytime**. ~25K events cumulative. ⚠️ billing_plan_change/grace_period/refund/chargeback streams appear frozen since ~05-28..06-04 — see §10.
  - **Conversion events** (`conversion.failed` since 2026-06-04, `conversion.completed` since 2026-06-09) — new, low volume.
  - **Account alerts** (`account.storage_warning`) — system notifications. ~2.2K events.
- **`metadata` JSON carries IDs:**
  - `subscription_id` and `partytime_event_type` on subscription events (from Partytime).
  - `email_id`, `kind`, `open_count` on email events.

---

## 6. Goal taxonomy

Matomo on site_id=2 defines **9 goals**. They show up in CH as rows with `matomo_idgoal IS NOT NULL`. **Always query goals by `matomo_idgoal` across all `event_type` values — Goal 4 is split between `event_type='signup'` (~82%) and `'goal'` (~18%), and the ratio drifts over time.**

> ⚠️ **Goals 1–3 carry NO `user_id`** (verified 100% NULL, 2026-06-10). Per-user funnel math (`uniqExact(user_id)`) silently returns **0** for stages 1–3. Use raw `count()` or `uniqExact(vid)` for those stages; only goals 4–5 are user-attributable.

| Goal ID | Name | Matomo match attribute | Status in CH (re-verified 2026-06-10) |
|---|---|---|---|
| 1 | Viewed Pricing | url regex `/(payment\|pricing)/?` | ⚠️ undercounted ~68%; **no user_id** |
| 2 | Package Clicked | url contains `skip_select=true` | ✅ ~88% match; **no user_id** |
| 3 | Chose Payment Method | url contains `step=pay` | ✅ matches; **no user_id** |
| 4 | Clicked Pay | manually (JS `trackGoal`) | ✅ split `signup`/`goal` ~82/18; ~+52% (likely double-counted) |
| 5 | Visited Signup | url contains `/signup` (~84%; rest fire on `/app/` urls) | ⚠️ undercounted ~76% |
| 6 | Saw Pricing Landing | url contains `/pricing` | ❌ frozen since 2026-05-27 — **still frozen 06-10** |
| 7 | Used File Feature | event_category=`File` | ❌ frozen since 2026-05-27 — **still frozen 06-10** (biggest loss) |
| 8 | Engaged Visit 60s+ | visit_duration ≥ 60 | ❌ frozen since 2026-05-27 — **still frozen 06-10** |
| 9 | Viewed Devices Page | url contains `/devices` | ❌ frozen since 2026-05-27 — **still frozen 06-10** |

**Until the CH pipeline is fixed**, treat goal aggregates as authoritative from Matomo HTTP API, not from CH. Per-user goal *presence* in CH is still useful (when the goal fires, it's recorded correctly).

```sql
-- Correct: count all goal firings
SELECT toDate(created_at) AS day,
       countIf(matomo_idgoal=1) AS g1,
       countIf(matomo_idgoal=2) AS g2,
       countIf(matomo_idgoal=4) AS g4,   -- note: event_type='signup', not 'goal'
       countIf(matomo_idgoal=5) AS g5
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= today() - INTERVAL 7 DAY
  AND matomo_idgoal IS NOT NULL
GROUP BY day ORDER BY day;
```

---

## 7. Identity model

Three identifiers; pick correctly when joining.

| ID | Where | Format | Use to join |
|---|---|---|---|
| `vid` | CH (`user_telemetry_events.vid`) | UUID, 36 chars (**93% v5, 7% v4** — not v4-only) | events from same browser/device |
| `user_id` | CH (`user_telemetry_events.user_id`) | UInt64 | **same Seedr account across all sources, browsers, sessions** ← prefer this |
| `idvisitor` | Matomo Live API | 16-hex MD5-style hash | ❌ no direct mapping to CH `vid` |

**Rule:** if comparing CH with anything else (Matomo, Seedr MySQL, payments box), join on `user_id`. For anonymous-user analysis (no login), CH and Matomo are separate ID spaces.

### `vid_user_map`

```sql
SELECT vid, user_id, version FROM seedr_telemetry.vid_user_map
WHERE vid = '00000361-f934-5998-a877-5e2edfefdcbd';
-- → user_id 9182163
```

3.1M rows mapping to **838K distinct `user_id`** — one user averages ~3.7 vids (devices/browsers), so never treat vid-counts as user-counts. Useful when you have a known `vid` and want the Seedr account it eventually logged into.

---

## 8. Subscription & revenue state

Two derived tables answer most billing questions without scanning the event log.

### `user_subscription_state` — current state per user

```sql
DESC seedr_telemetry.user_subscription_state;
-- user_id UInt64, current_plan_id UInt32, last_event_type LowCard, last_event_at DateTime, version UInt32
```

⚠️ **The table is churn-blind** (verified 2026-06-10): `last_event_type` only ever holds `created` / `reactivated` / `billing_plan_change`. Cancel states never appear — a churned user is simply *absent* from the table, not flagged. So:

```sql
-- Premium users (active subscription) — presence in the table IS the filter;
-- the IN(...) clause is redundant but harmless:
SELECT user_id, current_plan_id, last_event_at
FROM seedr_telemetry.user_subscription_state;

-- Free users = NOT EXISTS in this table.
-- (Do NOT look for "last event is a cancel" — that state never occurs here.
--  For churn analysis, query subscription.canceled/expired events in
--  user_telemetry_events instead.)
```

### `revenue_facts` — transaction-level revenue, since **2016**

```sql
DESC seedr_telemetry.revenue_facts;
-- txn_id, provider_transaction_id, user_id, subscription_id, billing_plan_id,
-- provider, txn_type, status, amount_usd, amount, currency,
-- country, transaction_date, version
```

Facts (verified 2026-06-10): `status` is 100% `'completed'`; providers are **paypal (86%) and paddle (13%)** plus a tail (dodo, razorpay, gocardless, native_btc, googleplay, nowpayments, manual) — **no Stripe**; common amounts 6.95 / 9.95 / 19.95 monthly and 69.50 / 99.50 / 199.50 annual; `billing_plan_id` has **33 distinct values** (1–23 plus a 1000-series) concentrated in plans 1, 3, 5.

**Real LTV** (no formula needed):

```sql
SELECT user_id, sum(amount_usd) AS ltv_usd,
       count() AS transactions,
       min(transaction_date) AS first_purchase,
       max(transaction_date) AS last_purchase
FROM seedr_telemetry.revenue_facts
WHERE status = 'completed'
GROUP BY user_id
ORDER BY ltv_usd DESC LIMIT 100;
```

This is the only CH table with **pre-2025-05-27 history** (back to 2016) — great for cohort lifetime analysis.

---

## 9. Common query recipes

### 9.1 Reconstruct a user's journey (replaces Matomo `Live.getVisitorProfile`)

```sql
SELECT created_at, event_type, surface, category, action, name, matomo_idgoal, url, country
FROM seedr_telemetry.user_telemetry_events
WHERE user_id = ?
  AND JSONExtractString(metadata,'src') IN ('matomo','')   -- web events only
ORDER BY created_at;
```

~800ms for a user with 44K events over 14 months. Compare with Matomo's 5-60s for the same data.

### 9.2 Bulk journey aggregates (one query, N users)

```sql
SELECT
  user_id,
  uniqExact(vid)                                 AS device_count,
  min(created_at)                                AS first_visit_at,
  max(created_at)                                AS last_visit_at,
  count()                                        AS total_actions,
  countIf(event_type='pageview')                 AS pageviews,
  countIf(matomo_idgoal IS NOT NULL)             AS goal_firings,
  max(matomo_idgoal)                             AS max_goal_reached,
  any(country)                                   AS primary_country
FROM seedr_telemetry.user_telemetry_events
WHERE user_id IN (?,?,?,...)                     -- batch of up to ~10K
  AND created_at >= today() - INTERVAL 30 DAY
GROUP BY user_id;
```

1K users → ~400ms · 10K users → ~1 sec · all active users (~70K in 7d) → ~30 sec.

### 9.3 Daily event volume (dashboard)

```sql
SELECT toDate(created_at) AS day,
       countIf(event_type='pageview') AS pageviews,
       countIf(event_type='event')    AS events,
       countIf(event_type='goal' OR (event_type='signup' AND matomo_idgoal IS NOT NULL)) AS goals,
       uniqExact(user_id)             AS unique_users,
       uniqExact(vid)                 AS unique_visitors
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= today() - INTERVAL 30 DAY
  AND JSONExtractString(metadata,'src') IN ('matomo','')
GROUP BY day ORDER BY day;
```

### 9.4 Email engagement per user

```sql
SELECT user_id,
       countIf(event_type='email.sent')                  AS sent,
       countIf(event_type='email.opened')                AS opened,
       countIf(event_type='email.clicked')               AS clicked,
       round(countIf(event_type='email.opened')   / countIf(event_type='email.sent') * 100, 1) AS open_rate,
       round(countIf(event_type='email.clicked')  / countIf(event_type='email.opened') * 100, 1) AS click_rate
FROM seedr_telemetry.user_telemetry_events
WHERE user_id = ?
  AND JSONExtractString(metadata,'src') IN ('mautic','internal_events')
GROUP BY user_id;
```

Note: since **2026-05-28** ALL email (marketing + transactional) lives under `src='internal_events'`; `mautic` is history-only. The two-source filter above remains correct for full-history queries. Cheaper alternative: `WHERE surface='email'` (no JSON parsing).

### 9.5 Funnel via goals

> ⚠️ **Do NOT use `uniqExact(user_id)` for goals 1–3** — their `user_id` is 100% NULL (verified 2026-06-10), so a per-user funnel silently returns zeros. Count raw firings (or `uniqExact(vid)`) for stages 1–3; only stage 4 is user-attributable.

```sql
SELECT
    countIf(matomo_idgoal=1)              AS s1_pricing_views,    -- raw: no user_id on goals 1-3
    countIf(matomo_idgoal=2)              AS s2_package_clicks,
    countIf(matomo_idgoal=3)              AS s3_payment_chosen,
    countIf(matomo_idgoal=4)              AS s4_clicked_pay,
    uniqExactIf(user_id, matomo_idgoal=4) AS s4_unique_users      -- only stage 4 supports this
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= today() - INTERVAL 7 DAY
  AND matomo_idgoal IS NOT NULL;
```

⚠️ **`s4_clicked_pay` will be ~+52% inflated vs Matomo** (Goal 4 duplication). Subtract `total / 1.52` for adjusted estimate, or use Matomo HTTP API for the authoritative number.

### 9.6 Real LTV joined with engagement

```sql
WITH revenue AS (
  SELECT user_id, sum(amount_usd) AS ltv_usd
  FROM seedr_telemetry.revenue_facts
  WHERE status='completed'
  GROUP BY user_id
),
engagement AS (
  SELECT user_id,
         max(created_at) AS last_seen,
         countIf(event_type='pageview') AS pageviews_30d
  FROM seedr_telemetry.user_telemetry_events
  WHERE created_at >= today() - INTERVAL 30 DAY
    AND event_type='pageview'
  GROUP BY user_id
)
SELECT r.user_id, r.ltv_usd, e.last_seen, e.pageviews_30d
FROM revenue r
LEFT JOIN engagement e ON r.user_id = e.user_id
WHERE r.ltv_usd > 100
ORDER BY r.ltv_usd DESC LIMIT 50;
```

---

## 10. Known data quality issues

A quick punch-list of issues observed in the warehouse. The fix for most of these is **upstream of CH** (in the matomo→CH ingest pipeline); these are temporary workarounds until that pipeline is corrected.

| Issue | Impact | Temporary workaround |
|---|---|---|
| **Goals 1–3 carry no `user_id`** (100% NULL) | per-user funnel returns 0 for stages 1–3 | use raw `count()` / `uniqExact(vid)` for stages 1–3; fix belongs in the tracker (fires before identity attach) |
| Goals 6, 7, 8, 9 frozen since 2026-05-27 (**still frozen as of 2026-06-10**) | major undercount for File-feature usage, devices-page, visit-duration goals | until ingest is fixed: query Matomo HTTP API for these specific goals. Note (2026-06-11): only the goal *firings* are frozen — raw `event_type='event', category='File'/'Archive'` rows still flow (~36K users/30d), so File-feature usage can be measured from raw events instead of goal 7 |
| Streaming under-tracked in web events: `video/stream_start` ≈ 2.6K users/30d vs edge log `/media` ≈ 18.8K users/7d (verified 2026-06-11) | stream-based segments built on web events miss ~85% of streamers | build streaming segments from `request_events` (base_path='/media') or stream rollups — see `SEEDR_CLICKHOUSE_REFERENCE.md` §3 |
| Goals 1, 5 undercounted ~70-76% | partial undercount even for url-pattern goals | until ingest is fixed: calibrate against Matomo aggregates |
| Goal 4 ~+52% inflated; split `signup`(~82%)/`goal`(~18%), ratio drifts | over-count of paid clicks; event_type filters miss rows | query by `matomo_idgoal`; dedupe by `(user_id, day)` or scale by 0.66 |
| `2026-05` partition inflated (dual-write window 2026-05-24..27, peak 1.63M/day on 05-26) | trend distortion | **dedup by `idempotency_key` does NOT help** — the dual-write events are distinct (ratio 1.0000). Exclude/normalize the window instead |
| `user_subscription_state` has no cancel states | churned users invisible in the state table | premium = present in table; churn analysis via `subscription.canceled/expired` events |
| `subscription.billing_plan_change` / `grace_period_entered` / `refund_processed` / `chargeback_opened` frozen since ~05-28..06-04 | rates from these events are stale/batch artifacts | confirm emitter status with Partytime owners before using |
| Email-blast days inflate daily unique `vid` up to 7× (e.g. 368K on 2026-06-07 via `surface='email'`) | visitor KPIs spike falsely | filter `surface='landing'` for unique-visitor metrics |
| **`surface` value for web events changed at the migration**: `'web'` before 2026-05-24, `'landing'` after (dual-write window 05-24..27 has both; verified live 2026-06-11) | `WHERE surface='landing'` silently drops ALL pre-migration web rows — e.g. a 60-day dormant-user query returns 0 | any query spanning the migration must use `surface IN ('web','landing')`; `'landing'` alone is only safe for windows entirely after 2026-05-27 |
| `src=''` event timestamps occasionally in the future | client-side clock skew (~7% of rows; max +300s since June producer fix, was +60min) | tolerate ±5min when matching |
| Pre-2025-05-27 web data not yet in CH | no long-history web behavioral data | Matomo HTTP API at `stat.repora.com` still holds it; backfill into CH is a separate workstream |
| `event_type='signup'` is ~99.98% "Goal 4 fired" (rare true account-creation rows exist with NULL idgoal) | misleading naming | trust `matomo_idgoal` not `event_type` |

---

## 11. Performance & operations

### Use partition pruning every time

```sql
WHERE created_at >= '2026-05-01' AND created_at < '2026-06-01'
-- touches one partition (202605), ~10-15M rows, fast
```

vs

```sql
WHERE toYYYYMM(created_at) = 202605
-- equivalent, also pruned
```

vs

```sql
-- DON'T do this on the full table:
WHERE event_type='pageview'
-- scans all ~70M pageview rows across all partitions
```

### Avoid `JSONExtractString(metadata, ...)` as a filter

```sql
-- BAD: scans+parses every row
WHERE JSONExtractString(metadata,'src')='matomo' AND ...

-- BETTER (often you don't need src filter at all):
WHERE event_type IN ('pageview','event','session_end','goal','signup')
  AND created_at >= today() - INTERVAL 30 DAY

-- BEST for stream separation: the LowCardinality `surface` column
-- ('landing'=web, 'email', 'task', 'subscription', 'conversion', 'account')
WHERE surface = 'landing'
  AND created_at >= today() - INTERVAL 30 DAY

-- ⚠️ but for windows reaching back before the 2026-05-24..27 migration,
-- web events carry surface='web', not 'landing' — span both eras:
WHERE surface IN ('web','landing')
  AND created_at >= today() - INTERVAL 90 DAY
```

If you must filter by `src` (e.g. to split the matomo/'' web eras), narrow the partition first.

### Throughput expectations

| Workload | Server-side latency | Output | Effective rate via tunnel |
|---|---|---|---|
| Aggregate count (any window) | ~300ms | tiny | n/a |
| 1-user journey (full history) | ~800ms | ~7 MB | ~10 MB/s |
| 1K users batch | ~400ms | ~250 KB | n/a |
| 10K users batch | ~1 sec | ~2.5 MB | n/a |
| All active users / 7d aggregates | ~30-37 sec | ~90 MB | ~2.5 MB/s |
| Raw 1-day event dump | ~70 sec | ~100 MB | ~1.3 MB/s |
| Raw 1-week event dump | >3 min | ~500 MB | ~2.7 MB/s |

The SSH tunnel + JSON serialization cap your real-world throughput at ~1-3 MB/s for large dumps. For mass extraction, prefer aggregates or run on the CH host directly (if you have access).

### Don't overload the server

Heavy GROUP BY over the full table with no partition filter can wedge the server. If a colleague is mid-query, your aggressive scan can make their query hang too. Best practices:

- Always include `WHERE created_at >= ...` even for "all-time" questions.
- Use `LIMIT 1000` for exploratory queries.
- Avoid `SELECT DISTINCT vid FROM <large window>` — it's expensive.
- One query at a time per session.

---

## 12. What is NOT (yet) in CH

CH is **the primary analytical store** and should be your default for any new query. The exceptions below are either upstream operational systems (rows you'd query *if* you need live operational state, not analytics) or known temporary gaps the ingest pipeline hasn't filled yet.

| Need | Where it currently lives |
|---|---|
| **Pre-2025-05-27 web analytics** (gap) | Still in Matomo at `https://stat.repora.com/index.php?idSite=2&token_auth=...`. Backfilling it into CH is a separate workstream. |
| **Live torrent / task / file catalog** (operational state, not analytics) | Central catalog MySQL at `my.seedr.cc` — tables `tasks`, `task_status`, `user_torrents`, `files`, `folder_files`. Read here only if you need real-time job state; for analytics use CH. |
| **Live billing details** (operational state) | **Partytime** payment pipeline at `payment.seedr.cc` (`payments_live_1`). The lifecycle *events* are already in CH; the billing service's own logs/observability are ALSO in CH (`payments` database — see `SEEDR_CLICKHOUSE_REFERENCE.md` §5). Only use the MySQL directly if you need active dunning state, in-flight authorisations, etc. |
| **User-facing account metadata** (emails, signup date, profile fields) | Central catalog MySQL `uc_users` — read here if you need PII for outbound systems (e.g. email sending). For analytics, the `user_id` in CH is enough. |

Everything else — web events, email engagement, subscription state, revenue, identity mapping — **lives in CH and CH alone**. Build new analytical pipelines against CH; reach for the other systems only when CH genuinely doesn't have what you need.

---

## 13. Cheat-sheet: Matomo HTTP API (legacy fallback only)

You should not need these for day-to-day work. Use them only for the two scenarios where CH is currently incomplete: pre-2025-05-27 web data, or the temporarily under-counted goals listed in §10.


```bash
BASE="https://stat.repora.com"
TOKEN="<your matomo api token>"
SITE=2

# Per-day visit totals
curl -X POST "$BASE/index.php" \
  -d "module=API&format=JSON&idSite=$SITE&token_auth=$TOKEN" \
  -d "method=VisitsSummary.get&period=day&date=previous30"

# Per-goal conversions
curl -X POST "$BASE/index.php" \
  -d "module=API&format=JSON&idSite=$SITE&token_auth=$TOKEN" \
  -d "method=Goals.get&idGoal=7&period=day&date=previous30"

# Single user's visits (use segment, NOT visitorId, for userId-based lookup)
curl -X POST "$BASE/index.php" \
  -d "module=API&format=JSON&idSite=$SITE&token_auth=$TOKEN" \
  -d "method=Live.getLastVisitsDetails&segment=userId==9612960&period=range&date=previous30&filter_limit=100"
```

Reminder: Matomo `actionDetails[].timestamp` is **Jerusalem-local**, not UTC. Convert before joining with CH.

---

## 14. Where to learn more

- Companion document: `SEEDR_PROJECT_OVERVIEW.md` — explains the business/product so you know *why* a given table or event exists.
- `SEEDR_CLICKHOUSE_REFERENCE.md` — **full inventory of both databases**: `request_events` edge/QoS log, the minute-rollup layer, `node_health`, ops logs, and the `payments` DB.
- `SEEDR_MARKETING_SEGMENTS.md` — verified segmentation research + live segment sizing (2026-06-11) and the recommended campaign segment portfolio.
- `SEEDR_DATA_VERIFICATION_2026-06-10.md` — latest verification verdicts (what's trustworthy, what's broken).
- ClickHouse docs: <https://clickhouse.com/docs/en/sql-reference> — especially `MergeTree`, `LowCardinality`, `JSONExtract*`.
- Matomo Reporting API: <https://developer.matomo.org/api-reference/reporting-api>.

---

## 15. Quick-start checklist for a new project

- [ ] Open the SSH tunnel to `data.seedr.cc`.
- [ ] Put credentials in `~/.clickhouse.seedr` (chmod 600).
- [ ] Test: `curl http://127.0.0.1:8123/ping` → `200`.
- [ ] First real query: `SELECT count() FROM seedr_telemetry.user_telemetry_events WHERE created_at >= today() - INTERVAL 1 HOUR`.
- [ ] Decide: do you need long-history (>1 year)? If yes, plan for Matomo HTTP API fallback.
- [ ] Decide: do you need emails? Then plan for `seedr.cc` MySQL access (separate provisioning).
- [ ] If your app writes timestamps anywhere, decide UTC end-to-end. **Do not** use Matomo's local-time timestamps as joins keys.

Good luck.
