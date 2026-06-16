# Seedr ClickHouse — Complete Data Reference

A full inventory of everything stored on the ClickHouse server at `data.seedr.cc`: both databases, every table, schemas, lineage, join keys, and query guidance.

- **Audience:** anyone querying the warehouse beyond the 4 core analytics tables.
- **Companions:** `SEEDR_DATA_GUIDE.md` (how to connect + analytics query recipes), `SEEDR_PROJECT_OVERVIEW.md` (business context), `SEEDR_DATA_VERIFICATION_2026-06-10.md` (data-quality verdicts).
- **Last verified:** 2026-06-10 against ClickHouse `26.4.2.10` (user `shaya`, SSH tunnel → `127.0.0.1:8123`).
- **Access (verified 2026-06-16):** role `shaya_rw`, `readonly=0` — SELECT on all DBs, **read-write/DDL only in `ml.*` and `shaya.*`** (both currently empty); `seedr_telemetry`/`payments` have no write grant.

---

## 1. Databases at a glance

| Database | Base tables | MVs | What it holds |
|---|---|---|---|
| `seedr_telemetry` | 23 | 15 | User analytics (events/identity/subscriptions/revenue) + edge/QoS logs + minute rollups + node & ops telemetry |
| `payments` | 4 | 1 | Observability of the billing service ("Partytime"): app event log + HTTP access/error logs |

Engine split in `seedr_telemetry`: 4 × `ReplacingMergeTree` (the analytics core), 13 × `AggregatingMergeTree` (minute rollups), 6 × `MergeTree` (raw logs: `request_events`, `node_health`, `mysql_error_events`, `mysql_slow_events`, `playback_events`, `tt`).

The tables fall into four logical layers:

```
┌─ USER ANALYTICS ──────────────────────────────────────────────────┐
│ user_telemetry_events (146M)  ←─ matomo/web + mautic + internal   │
│   ├─ vid_user_map_mv            → vid_user_map (3.1M)             │
│   └─ user_subscription_state_mv → user_subscription_state (3.2K)  │
│ revenue_facts (280K, since 2016)                                  │
├─ EDGE / QoS ──────────────────────────────────────────────────────┤
│ request_events (187M, TTL 90d) ←─ nginx/Go edge on all nodes      │
│   └─ 13 × *_mv → minute/hour/day rollups (AggregatingMergeTree)   │
│ node_health (TTL 30d)          ←─ node OS/kernel telemetry        │
├─ OPS LOGS (new 2026-06) ──────────────────────────────────────────┤
│ mysql_error_events · mysql_slow_events · playback_events (empty)  │
├─ payments DB ─────────────────────────────────────────────────────┤
│ payment_app_events · payment_http_requests · payment_edge_requests│
│   └─ payment_errors_mv → payment_errors                           │
└───────────────────────────────────────────────────────────────────┘
```

Row counts / compressed sizes (2026-06-10, active parts):

| Table | Rows | Compressed | Notes |
|---|---|---|---|
| `user_telemetry_events` | 145.70M | 4.72 GiB | +~650K/day |
| `request_events` | 187.07M | 3.59 GiB | ~24–28M/day; created ~06-02, TTL 90d → steady state ≈ 2B |
| `vid_user_map` | 3.12M | 131 MiB | 838,284 distinct user_id |
| `revenue_facts` | 280,041 | 11.4 MiB | back to 2016-01-26 |
| `user_subscription_state` | 3,228 | 41 KiB | 1 row per active-ish subscriber |
| `mysql_error_events` | 440K+ | — | ingesting since 2026-06-09 |
| `mysql_slow_events` | 554+ | — | ingesting since 2026-06-09 |
| `playback_events` | 0 | — | created 2026-06-10, empty so far |
| `payment_app_events` | ~107K | — | window 2026-06-02 → now |
| rollup tables | grow with request_events | ~230 MiB total | |

---

## 2. User analytics layer (`seedr_telemetry`)

### 2.1 `user_telemetry_events` — the main event stream

One row per analytically-interesting event from any source. ~146M rows, ~650K/day.

```sql
vid              FixedString(36)        -- visitor UUID (93% v5, 7% v4); browser-bound
user_id          Nullable(UInt64)       -- Seedr account id (= uc_users.id) when logged in; ~81% non-null
event_type       LowCardinality(String) -- pageview/event/heartbeat/session_end/goal/signup/impression/click
                                        -- + email.* / task.* / subscription.* / conversion.* / account.*
surface          LowCardinality(String) -- web (pre-2026-05-24) / landing / email / task / account / subscription / conversion / app / ''
                                        -- ⚠️ web events: 'web' before the 2026-05-24..27 migration, 'landing' after (see §8 #13)
slot_key         LowCardinality(String) -- A/B test slot
variant          LowCardinality(String) -- A/B test variant
url              String CODEC(ZSTD(3))
referrer         String CODEC(ZSTD(3))
ip_v4            Nullable(IPv4)         -- only one of v4/v6 populated
ip_v6            Nullable(IPv6)
ua               String CODEC(ZSTD(3))
country          LowCardinality(String) -- ISO-3166 alpha-2
matomo_idgoal    Nullable(UInt16)       -- 1..9 (see DATA_GUIDE §6 for caveats)
matomo_ec_id     String CODEC(ZSTD(3))  -- ecommerce order id (rare)
revenue_cents    Nullable(Int64)        -- ⚠️ effectively dead: only on 'goal' events, 99.98% zero
category         LowCardinality(String) -- event category ('File', 'i18n', ...)
action           LowCardinality(String) -- event action ('View', 'Download', ...)
name             String                 -- event name (free-text)
value            Nullable(Int64)
metadata         String CODEC(ZSTD(3))  -- JSON; carries 'src' tag + source-specific fields
idempotency_key  String CODEC(ZSTD(3))  -- unique per logical event
version          UInt32                 -- ReplacingMergeTree version
created_at       DateTime               -- event time (UTC)
received_at      DateTime               -- ingestion time (UTC)

ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(created_at)
ORDER BY idempotency_key
TTL created_at + INTERVAL 30 DAY WHERE event_type='heartbeat',
    created_at + INTERVAL 24 MONTH
INDEX idx_vid vid TYPE bloom_filter, INDEX idx_uid user_id TYPE bloom_filter
```

**Source streams** (tag = `JSONExtractString(metadata,'src')`; for cheap filtering prefer the `surface` column — see §7):

| src | Window | Rows | Status |
|---|---|---|---|
| `matomo` | 2025-05-27 → 2026-05-27 21:35 | 116.46M | **stopped** (migration) |
| `''` | 2026-03-31 → now | 9.7M+ | live — current web telemetry |
| `mautic` | 2025-01-10 → 2026-05-28 14:13 | 17.66M | **stopped** — email migrated to internal_events |
| `internal_events` | 2026-01-12 → now | 1.84M+ | live — ALL email + task.* + subscription.* + conversion.* + account.* |

**metadata JSON fields by source:** subscription events carry `partytime_event_type`, `subscription_id`; email events carry `email_id`, `kind`, `open_count`; web events carry `accept_language`, truncated ip, ua details.

### 2.2 `vid_user_map` — browser identity → account

`vid FixedString(36)`, `user_id UInt64`, `version UInt32`. ReplacingMergeTree fed by `vid_user_map_mv` from `user_telemetry_events`. 3.12M rows / **838K distinct user_id** / 2.24M distinct vid — one user averages ~3.7 vids (devices/browsers), so **vid is not a user identifier**. ~8 malformed vids exist (non-v4/v5 version chars).

### 2.3 `user_subscription_state` — current subscriber state

`user_id UInt64`, `current_plan_id UInt32`, `last_event_type LowCardinality(String)`, `last_event_at DateTime`, `version UInt32`. Fed by `user_subscription_state_mv`. 3,228 rows.

⚠️ **Churn-blind:** `last_event_type` only ever holds `subscription.created` / `subscription.reactivated` / `subscription.billing_plan_change`. Cancelled users are *removed/absent*, not flagged. "Is premium" = present in this table; there is no cancel state to query.

### 2.4 `revenue_facts` — the financial ledger (since 2016)

```
txn_id, provider_transaction_id, user_id UInt64, subscription_id UInt64,
billing_plan_id Nullable, provider, txn_type, status, amount_usd, amount,
currency, country, transaction_date DateTime, version
```

280,041 rows, 2016-01-26 → now, `status` is 100% `'completed'`. Providers: paypal 242K · paddle 37K · dodo 425 · razorpay 76 · gocardless 22 · `''` 20 · native_btc 11 · googleplay 3 · nowpayments 2 · manual 1 (**no Stripe**). Top amounts: 6.95 / 9.95 / 19.95 monthly; 69.50 / 99.50 / 199.50 annual. `billing_plan_id`: 33 distinct values (1–23 plus a 1000-series: 1002…1090) + NULLs; volume concentrated in plans 1, 3, 5.

---

## 3. Edge / QoS layer (`seedr_telemetry`)

### 3.1 `request_events` — raw per-request edge log

One row per HTTP request served by the node fleet (`seedr.cc` main node + `rd*`/`nw*`/`sf*` subnodes). The single source feeding all minute rollups. ~24–28M rows/day, ~25–31K unique users/day, ~61 nodes.

```sql
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (node, base_path, status, ts)
TTL ts + INTERVAL 90 DAY
-- skip indexes: user_id (minmax), is_stall (set)
-- PROJECTION p_user (ORDER BY user_id), p_file (ORDER BY file_id)
```

| Group | Columns |
|---|---|
| Time/identity | `ts DateTime64(3)`, `node LC`, `conn_id UInt64`, `user_id UInt32` (0 = anonymous), `client_ip IPv6` |
| Object | `ffid UInt32`, `file_id UInt64`, `hash String` |
| Request | `base_path LC`, `path String`, `route LC` (nginx route label), `status UInt16`, `method Enum8`, `proto Enum8(h1/h2/h3)`, `is_range UInt8` |
| Volume/time | `bytes_sent UInt64`, `bytes_expect Int64`, `body_bytes UInt64`, `req_time_ms UInt32` |
| Transport QoS | `ttype Enum8(none/tcp/quic)`, `rtt_us`, `min_rtt_us`, `retrans`, `cwnd`, `delivery_kbps`, `bbr_state UInt8`, `bbr_bw_kbps`, `pacing_kbps`, `app_limited UInt8`, `in_flight` |
| Upstream | `up_addr LC`, `up_status UInt16`, `up_connect_ms`, `up_header_ms`, `up_response_ms`, `up_tries UInt8` |
| Geo/network | `country LC`, `region LC`, `asn UInt32`, `asn_org LC` |
| Derived (MATERIALIZED) | `is_error` (status≥500), `is_stall`, `is_stalled` (large-file stall heuristic) |

Typical distributions (7d sample, 2026-06): `base_path` — `/phpapi` 64.6M, `/media` 18.3M, `/other` 17.0M, `/tasks` 11.4M, `/dev` 11.1M, `/app` 9.6M, `/ff_get` 6.0M…; `status` — 200 137.9M, **429 8.5M (heavy rate-limiting)**, 404 6.3M, 401 6.2M, 304 5.3M, 206 2.9M; transport — tcp:quic ≈ 4.4:1, proto h2 > h3 > h1.

**Joins:** `user_id` → `user_telemetry_events.user_id` (clean, ~100% of non-zero ids match). There is **no `vid` column** — anonymous edge traffic cannot be tied to web visitors. `file_id`/`hash`/`ffid` → file objects; `asn`/`country` → geo dims.

### 3.2 Minute-rollup layer (AggregatingMergeTree + paired `_mv`)

Each rollup holds `*State()` aggregate columns populated at insert time by a MaterializedView reading `request_events`. **Read them with the matching `*Merge()` combinators** (`countMerge`, `sumMerge`, `uniqMerge`, `avgMerge`, `quantilesTDigestMerge(...)`).

| Rollup | Grain | Aggregates |
|---|---|---|
| `stats_minute` | minute, node | count, bytes, errors (status≥400), req_time pctiles/avg |
| `country_minute` | minute, node, country | count, bytes, uniq client_ips, req_time pctiles/avg |
| `isp_minute` | minute, node, asn, asn_org | count, bytes, uniq ips, req_time pctiles/avg |
| `path_minute` | minute, node, base_path | count, bytes, req_time pctiles/avg |
| `status_minute` | minute, node, status | count |
| `ip_minute` | minute, node, client_ip | count, bytes |
| `file_minute` | minute, node, hash | count, bytes |
| `user_minute` | minute, node, user_id (≠0) | count, bytes |
| `stall_minute` | minute, node, base_path | stall count, bytes_lost, avg stall/upstream ms (uses `is_stalled`) |
| `stream_status_minute` | minute, node, pattern, status | count by stream pattern (presentation_master/_audio/_hls, legacy_lq/_hd, vod_map, subnode_action_*, thumbs, subtitles, other) |
| `stream_empty_stub_minute` | minute, node, pattern | count of `status=200 AND body_bytes=101` (empty-stub responses) |
| `path_quality` | **hour**, node, asn, base_path | count, rtt p50/p95, retrans, stalls, errors, h3_share, bytes |
| `bw_user_day` | **day**, user_id | sum(bytes), request count — per-user daily bandwidth |

Example read:

```sql
SELECT minute, node, countMerge(requests) AS reqs, sumMerge(bytes) AS bytes,
       sumMerge(errors) AS errors, avgMerge(rt_avg) AS rt_avg_ms
FROM seedr_telemetry.stats_minute
WHERE minute >= now() - INTERVAL 30 MINUTE
GROUP BY minute, node ORDER BY minute DESC, reqs DESC LIMIT 20;
```

### 3.3 `node_health` — node OS/kernel telemetry

**Not** a request_events rollup — directly inserted by node agents. MergeTree, `ORDER BY (node, ts)`, TTL 30d. Columns: `ts`, `node`; TCP/UDP kernel counters (`syn_drops`, `listen_overflows`, `backlog_drops`, `retrans_segs`, `lost_retransmit`, `tcp_timeouts`, `estab_conns`, `orphan_conns`, `udp_in_datagrams/_in_errors/_rcvbuf_errors/_sndbuf_errors`); load (`load1/5/15`, `proc_running`, `proc_blocked`); memory (`mem_total_mb`, `mem_avail_mb`, `swap_used_mb`); app processes (`ffmpeg_count`, `ffmpeg_cpu_pct`, `nginx_workers`); disk (`disk_await_max_ms`, `disk_util_max_pct`); `uptime_sec`.

Use for: correlating QoS regressions (`stats_minute` errors / `stall_minute`) with node overload (load, ffmpeg storms, disk saturation).

---

## 4. Ops logs (new, 2026-06)

| Table | Since | Schema (key cols) | Purpose |
|---|---|---|---|
| `mysql_error_events` | 2026-06-09 | `ts DateTime64(6)`, `host LC`, `thread_id`, `severity LC`, `code LC`, `subsystem LC`, `message` · MergeTree, `ORDER BY (severity, code, ts)`, partition by month | MySQL error-log ingest from the fleet (440K rows in first day — noisy, needs triage) |
| `mysql_slow_events` | 2026-06-09 | `ts DateTime64(6)`, `host LC`, `user LC`, `client_host LC`, `db LC`, `query_time Float32`, `lock_time`, `rows_sent/examined/affected`, `bytes_sent`, `last_errno`, `killed`, `sql_text` · `ORDER BY ts` | MySQL slow-query log |
| `playback_events` | 2026-06-10 | `ts DateTime64(3)`, `node LC`, `hash`, `kind LC`, `detail`, `level Int8`, `ttff_ms UInt32`, `browser LC`, `client_ip IPv6`, `country LC`, `asn` · `ORDER BY (kind, ts)`, partition by day | Player/QoE telemetry (time-to-first-frame, playback errors). **Empty as of 2026-06-10** — re-check |
| `tt` | — | `a UInt8` + EPHEMERAL/MATERIALIZED IPv6-CIDR-masking experiment, 3 rows | Scratch table; disposable |

---

## 5. `payments` database — billing-service observability

Operational/debug telemetry of the payments service (Partytime). **Not the ledger** — the curated financial record is `seedr_telemetry.revenue_facts`; this DB is the live wire- and app-level view that produces it. All times UTC, partitioned by month. Observed window starts 2026-06-02.

| Table | Rows (06-10) | Schema | Purpose |
|---|---|---|---|
| `payment_app_events` | ~107K | `ts DateTime64(3)`, `rid` (request id), `action LC`, `uid UInt32`, `sub_id String`, `txn_id String`, `error_code LC`, `level LC` (info/warn/error), `msg` · `ORDER BY (action, ts)` | Structured business-event log. Top actions: `GOCARDLESS_RENEWAL`, `ORG_INFO`, `USER_STATE`, `GRACE_WATCH`, `DODO_CHECKOUT`, `PAYPAL_IPN`, `RAZORPAY_RENEWAL`, `BINANCE_WEBHOOK`, `RECONCILIATION_CRITICAL`, `MAUTIC_RETRY` |
| `payment_http_requests` | ~51K | `ts`, `rid`, `method`, `route`, `uri`, `status`, `req_us`, `ip`, `provider LC`, `is_error` · `ORDER BY (route, ts)` | Inbound HTTP access log of the payments service |
| `payment_edge_requests` | ~51K | + `upstream_ms`, `upstream_status`, `bytes`, `ua`, `host`, `aborted` | Edge/proxy access log with upstream timing. Provider label includes paypal, dodo, gocardless, binance, razorpay, googleplay, nowpayments, roku, stripe, apple (route vocabulary — broader than actual transaction providers) |
| `payment_errors` | ~2.4K | subset of http_requests cols | Error-only funnel, auto-populated by `payment_errors_mv` (`WHERE is_error=1 OR status>=400`). Mostly scanner noise (404s) |

**Joins to `seedr_telemetry`:** `payment_app_events.uid` → `user_id` (sampled ~100% match into `user_telemetry_events`, ~24% into `revenue_facts` — payment-event users aren't all transacting users); `sub_id` (String) ↔ `revenue_facts.subscription_id` (UInt64) — **cast required**, e.g. `toUInt64OrZero(sub_id)`. Provider vocabulary shared with `revenue_facts.provider`.

Useful ops queries: `RECONCILIATION_CRITICAL` rows (billing inconsistencies), `level='error'` rate by `action`, provider webhook failures by `status`.

---

## 6. Materialized-view lineage (all 16 MVs)

| MV | Source | Target |
|---|---|---|
| `vid_user_map_mv` | user_telemetry_events | vid_user_map |
| `user_subscription_state_mv` | user_telemetry_events | user_subscription_state |
| `stats_minute_mv` | request_events | stats_minute |
| `country_minute_mv` | request_events | country_minute |
| `isp_minute_mv` | request_events | isp_minute |
| `path_minute_mv` | request_events | path_minute |
| `status_minute_mv` | request_events | status_minute |
| `ip_minute_mv` | request_events | ip_minute |
| `file_minute_mv` | request_events | file_minute |
| `user_minute_mv` | request_events | user_minute |
| `stall_minute_mv` | request_events | stall_minute |
| `stream_status_minute_mv` | request_events | stream_status_minute |
| `stream_empty_stub_minute_mv` | request_events | stream_empty_stub_minute |
| `path_quality_mv` | request_events | path_quality |
| `bw_user_day_mv` | request_events | bw_user_day |
| `payments.payment_errors_mv` | payments.payment_http_requests | payments.payment_errors |

MVs fire at insert time only — they do not backfill. (`mysql_*`, `playback_events`, `node_health`, `tt` have no MVs.)

---

## 7. Identity & join keys

| Key | Where | Notes |
|---|---|---|
| `user_id` | user_telemetry_events (Nullable UInt64), vid_user_map, user_subscription_state, revenue_facts, request_events (UInt32, 0=anon), user_minute, bw_user_day, payments.payment_app_events (`uid`) | **The universal join key.** Always prefer it for cross-table/cross-system joins |
| `vid` | user_telemetry_events, vid_user_map | Browser-bound UUID (93% v5 / 7% v4). One user ≈ 3.7 vids. **Absent from request_events** — edge traffic doesn't know vid |
| `subscription_id` | revenue_facts (UInt64), subscription events' `metadata`, payments `sub_id` (String) | Cast String↔UInt64 when joining payments ↔ revenue_facts |
| `file_id` / `hash` / `ffid` | request_events, file_minute (`hash`) | File objects; hash = content address (v1fs SHA1) |
| `node` | request_events, all rollups, node_health, playback_events | Fleet topology: `seedr.cc` main + `rd*`/`nw*`/`sf*` subnodes |
| `asn` / `country` | request_events, isp/country rollups, playback_events, user_telemetry_events (`country`) | Geo/network dims |

**Performance tip:** to separate streams in `user_telemetry_events`, prefer the LowCardinality `surface` column (`landing`=web, `email`, `task`, `subscription`, `conversion`, `account`) over `JSONExtractString(metadata,'src')` — same partitioning of the data without JSON parsing on every row. ⚠️ For web events the surface value changed at the migration: `'web'` before 2026-05-24, `'landing'` after — queries spanning the migration must use `surface IN ('web','landing')`. The `src` tag remains useful for the matomo-vs-'' era split within web events.

---

## 8. Known data-quality caveats (compact)

Authoritative detail in `SEEDR_DATA_VERIFICATION_2026-06-10.md`.

| # | Caveat | Consequence |
|---|---|---|
| 1 | Goals 1–3 have `user_id` 100% NULL | Per-user funnel impossible for stages 1–3; use `count()` / `uniqExact(vid)` |
| 2 | Goals 6–9 frozen since 2026-05-27 (ingest bug, unfixed) | Use Matomo HTTP API for those goals |
| 3 | Goal 4 stored under both `event_type` 'signup' (~82%) and 'goal' (~18%); ratio drifts | Query by `matomo_idgoal`, never by event_type |
| 4 | `user_subscription_state` has no cancel states | Churned users vanish; "premium" = present in table |
| 5 | billing_plan_change / grace_period / refund / chargeback events frozen since ~05-28..06-04 | Don't compute rates from them until emitter is confirmed |
| 6 | 2026-05 partition inflated 05-24..27 (dual-write); events are DISTINCT — dedup by idempotency_key removes nothing | Exclude/normalize that window in trend analysis |
| 7 | ~7% of web rows future-dated (client clock skew), max +300s since June fix | Tolerate ±5 min when matching |
| 8 | Email-blast days (e.g. 06-07/06-08) inflate daily unique vid up to 7× via `surface='email'` | Filter `surface='landing'` for visitor KPIs |
| 9 | `revenue_cents` ≈ always 0 | Use `revenue_facts` for revenue |
| 10 | matomo (≤05-27) / mautic (≤05-28) are dead streams | Current web = src `''`; current email = `internal_events` |
| 11 | Pre-2025-05-27 web history absent from CH | Matomo HTTP API at stat.repora.com |
| 12 | request_events holds <90d (TTL) and started 2026-06-02 | No long history on the edge log yet |
| 13 | Web-event `surface` changed `'web'` → `'landing'` at the 2026-05-24..27 migration (verified 2026-06-11) | `surface='landing'` alone silently drops all pre-migration web rows; span both: `surface IN ('web','landing')` |
| 14 | Streaming is heavily under-tracked in web events: `video/stream_start` ≈ 2.6K users/30d vs edge log `/media` ≈ 18.8K users/7d | Build streaming/QoE segments from `request_events` (base_path='/media') or stream rollups, not from web events |
| 15 | internal_events per-type history is uneven (verified 2026-06-11): `subscription.*` since 2026-01-12, but `task.*` only since **2026-05-25**, `account.storage_warning` since **2026-06-01**; web `category='video'` since **2026-05-16** | Historical features/segments built on tasks, storage warnings or stream events are empty before those dates |

---

## 9. Query guidance for the non-analytics layers

- **Always partition-prune:** `request_events` partitions by **day** (`WHERE ts >= today() - 1`), `user_telemetry_events` by month, payments tables by month.
- **Per-user edge scans are cheap** thanks to the `p_user` projection: `WHERE user_id = N` over request_events is fast even without a time filter (still add one).
- **Rollups before raw:** for dashboards/time-series use `*_minute` tables with `*Merge()` — they're 100–1000× smaller than request_events.
- **Heavy GROUP BY on request_events** (e.g. by `path` or `client_ip` over a week) can be hundreds of millions of rows — narrow to a day or use ip_minute/path_minute.
- **Reading AggregatingMergeTree without `*Merge()`** returns opaque state blobs — always aggregate.
- One query at a time; the server is shared.
