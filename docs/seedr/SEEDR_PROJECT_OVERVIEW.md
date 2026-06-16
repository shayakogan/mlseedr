# Seedr — Project Overview

A bootstrap document for LLMs, new engineers, and data analysts who need to understand **what seedr.cc does, who uses it, and how it makes money**. Facts here are derived from two sources: the production telemetry warehouse (`seedr_telemetry` in ClickHouse on `data.seedr.cc`) and the public production codebase (see §7). See `SEEDR_DATA_GUIDE.md` for the raw warehouse schema.

**Last verified:** 2026-06-10 (see `SEEDR_DATA_VERIFICATION_2026-06-10.md` for verdicts; `SEEDR_CLICKHOUSE_REFERENCE.md` for the full warehouse inventory).

---

## 1. What is Seedr?

**Seedr is a cloud BitTorrent and remote-download service operated at https://www.seedr.cc.**

A user submits a magnet link, `.torrent` file, **or a URL from a supported cloud source** (Google Drive, Dropbox, Mega, TeraBox, generic HTTP, S3, Backblaze B2, …). Seedr's backend fetches the content onto Seedr-controlled storage; the user then **streams or downloads the resulting files** — over HTTPS, FTP, SFTP, WebDAV, or an S3-compatible API.

The model removes several frictions for end users:
- No local torrent software / ports / NAT issues.
- No exposure of the user's IP to torrent swarms (Seedr's IPs do the peer-to-peer work).
- Reliable HTTPS streaming with adaptive video quality (HLS, multi-bitrate transcoded by FFmpeg).
- Multi-protocol access (web, FTP/FTPS, SFTP, WebDAV, S3) makes Seedr usable from any client.
- Built-in archive extraction (ZIP, RAR, 7z, TAR, ISO, multi-part archives) and media transcoding (video → HLS, audio re-encode, image thumbnails, PDF/Office processing).

**Quality tiers are subscription-gated**: free users are capped at 480p video; premium tiers unlock 720p, 1080p, and 2160p.

Seedr has been operating since at least **2016** (the `revenue_facts` table holds transactions back to `2016-01`).

---

## 2. The product surface (what users see)

### Web routes (served by the PHP gateway — see §7)

| Path | What it is |
|---|---|
| `https://www.seedr.cc/` | Marketing homepage |
| `/files/<numeric-id>` | Public/shareable file page |
| `/torrent/<numeric-id>` | Torrent submission / details page |
| `/app/files/0`, `/app/files/<id>` | Logged-in file manager UI |
| `/app/tasks/0`, `/app/tasks/<id>` | Logged-in task (in-progress torrent/download) UI |
| `/app/settings#account` | Account settings / billing |
| `/signup` | Sign-up flow |
| `/pricing` | Marketing pricing page |
| `/payment` | Checkout flow |
| `/devices` | Multi-device management |
| `/dynamic/get_space` | Storage-quota API endpoint |

Logical split:
- **Marketing site** (`/`, `/pricing`, `/signup`, `/devices`) — anonymous traffic, conversion funnel.
- **Web app** (`/app/...`) — logged-in users managing files and downloads.

### Media / asset routes (served by the Go subnode — see §7)

| Path | What it is |
|---|---|
| `/presentations/p/file/<storageType>/<storageId>/<itemId>/assets/video/...` | HLS master playlist + segments |
| `/presentations/p/file/.../assets/audio/...` | On-demand audio re-encode |
| `/presentations/p/file/.../assets/image/...` | Thumbnails / previews |
| `/presentations/p/file/.../assets/archive/...` | Archive listing / extraction |
| `/presentations/p/file/.../status[/detailed]` | Asset generation progress |
| `/presentations/p/file/.../assets/info/{basic,extended,exif/*}` | Media metadata |

### Non-HTTP access (also served by the Go subnode)

Seedr exposes the user's library through additional file-protocol servers running on the same subnode binary:

| Protocol | Notes |
|---|---|
| **FTP / FTPS** | Explicit TLS + implicit FTPS |
| **SFTP** | SSH key + password auth |
| **WebDAV** | HTTP/2, range requests, bulk operations |
| **S3-compatible** | Custom implementation, usable from any S3 SDK |

Some of these are subscription-gated (e.g., WebDAV access typically requires premium).

---

## 3. Who uses Seedr

### Geographic profile (top countries by event volume)

Heavy emerging-markets bias visible across samples:
- **India (`in`)** — consistently the largest single country across recent samples.
- Indonesia (`id`), Philippines (`ph`), Bangladesh (`bd`), Nigeria (`ng`), Vietnam (`vn`), Sri Lanka (`lk`), Pakistan (`pk`), Kenya (`ke`), Tanzania (`tz`), Uganda (`ug`), Ethiopia (`et`), Ghana (`gh`) — these countries make up the canonical "emerging markets" marketing segment.
- Also present: United States, Germany, Netherlands, Norway, Poland, Italy, Saudi Arabia, UAE.

This shape is consistent with a service that solves "I can't run a torrent client at home" or "I need to bypass home-bandwidth/blocked-traffic limits" — both more acute in mobile-heavy / restrictive-ISP geographies.

### Device profile

- Mix of `Desktop` and `Smartphone` is visible in sample visits.
- Mobile traffic is strategically prioritised: `is_mobile = 1` is a named marketing cohort, suggesting mobile UX is a distinct surface to optimise for.

### Scale

| Metric | Value (as of 2026-06-10) |
|---|---|
| Total events in CH (all sources, ~13 months) | ~146M |
| Unique known users (distinct `user_id` in `vid_user_map`) | **~838K** (the map has 3.1M *rows* — one user ≈ 3.7 browser vids; earlier "~2.5M users" counted rows, not users) |
| Active web users in last 7 days | ~68K (logged-in, `surface='landing'`) |
| Unique visitors per day (Matomo `nb_uniq_visitors`) | ~40-60K (⚠️ in CH, filter `surface='landing'` — email-blast days inflate raw unique-vid up to 7×) |
| Daily visits | ~50-60K |
| Daily actions/pageviews | ~300-380K |
| Bounce rate (Matomo) | 33-43% (typical for a content site) |
| Active subscriptions (current state) | ~3.2K |
| Emails sent per month (all email, internal pipeline) | ~820K (incl. bulk campaigns; mautic stream ended 2026-05-28 — its real throughput was ~1.4M/mo, not the "15M/mo" previously quoted) |
| Transactional emails (internal) per month | ~300K |

The ~70K weekly-active vs ~3K paying-subscribers ratio implies a **freemium model with single-digit-percent paid conversion** — see §4.

---

## 4. Business model: freemium + recurring subscriptions

### Plan tiers
`revenue_facts.billing_plan_id` holds **33 distinct values** (1–23 plus a 1000-series: 1002…1090), with volume concentrated in plans **1, 3, 5**. Internal value-cohort labels (**Platinum / Gold / Silver / Bronze / Prospects**) are commonly used to talk about users by spend tier; the exact plan-ID mapping (esp. the 1000-series) should be confirmed with the billing team. Plan tier also controls maximum video quality (free → 480p, premium → 720p+) and unlocks access to protocols like WebDAV (verified in subnode auth code).

### Pricing signal
- Common completed amounts: **$6.95 (most frequent), $9.95, $19.95** monthly; **$69.50 / $99.50 / $199.50** annual.
- Currency for the business is **USD** (site_currency from `SitesManager.getSiteFromId`).
- **Current advertised tiers** (live-verified against `seedr.cc/pricing` + `/products/power`, 2026-06-11): Lite $3.95/10GB · Basic $7.95/50GB/720p/2 task slots · Pro $12.95/200GB/1080p/8 slots · Master $19/1TB/4K/25 slots · Gold One–Four $32.95–$119.95/2–10TB/35–100 slots. The ladder prices three behavioral dimensions — **storage quota, streaming resolution, task concurrency** — each of which is a telemetry-mappable upsell trigger (see `SEEDR_MARKETING_SEGMENTS.md`). Note the historical transaction amounts above differ from current list prices (price changes over time).

### Payment providers
- **PayPal ~86%** and **Paddle ~13%** of all transactions; long tail: dodo, razorpay, gocardless, native_btc, googleplay, nowpayments, manual.
- **No Stripe** (an earlier guess; disproved against `revenue_facts`).

### Subscription lifecycle (full state machine visible in CH)

Billing is owned by a dedicated internal service called **"Partytime"** (separate repo: `partytime/payments_ver2`). Its lifecycle events flow into CH with `src='internal_events'` and carry `metadata.partytime_event_type` + `metadata.subscription_id`.

`event_type` values under `src='internal_events'`:

```
subscription.created              ← new paid sub
subscription.reactivated          ← lapsed user comes back
subscription.cancellation_scheduled   ← user clicks "cancel" but keeps access until period end
subscription.canceled             ← end of paid access
subscription.expired              ← NEW event type since 2026-06-07
subscription.billing_plan_change  ← upgrade / downgrade   ⚠️ frozen since 2026-05-28 (single-day batch)
subscription.grace_period_entered ← payment failed but still active   ⚠️ frozen
subscription.payment_failed       ← billing decline
subscription.refund_processed     ← merchant refund   ⚠️ frozen since ~06-04
subscription.chargeback_opened    ← bank chargeback dispute   ⚠️ frozen since ~06-04
```

Volume signal (last 30 days, as of 2026-06-10):
- `created`: 3.6K · `reactivated`: 1.7K → **gross adds ~5.3K/month**
- `canceled`: 659 · `cancellation_scheduled`: 1.3K → **gross churn ~2K/month**
- `payment_failed`: 69 · `expired`: 38 → low absolute volume
- ⚠️ refund/chargeback/grace/plan-change streams appear **frozen** (no new events since late May / early June) — confirm emitter health with Partytime owners before computing rates from them.

These match a healthy SaaS-style churn-vs-growth profile. The `cancellation_scheduled` event is operationally important — it's a flag that the user has *intended* to cancel but is still a paying customer for a window — that's a retention-campaign trigger.

### Refund & chargeback signals
Both `refund_processed` (merchant-initiated) and `chargeback_opened` (bank-initiated) are first-class events. Low volumes (~17-24/month historically) suggest the service has tolerable consumer-friction. Worth alerting on spikes. ⚠️ As of 2026-06-10 both streams appear frozen (no new events since ~06-04) — verify the emitter before trusting current rates.

---

## 5. The conversion funnel

Seedr's conversion funnel is encoded in Matomo as **9 goals**, of which 1–4 form the canonical purchase funnel:

| Stage | Goal ID | Name | URL trigger | Meaning |
|---|---|---|---|---|
| 0 | — | Not Started | — | All anonymous browse / app use |
| 1 | 1 | Viewed Pricing | `/(payment\|pricing)/` | Considered upgrade |
| 2 | 2 | Package Clicked | `skip_select=true` in URL | Picked a plan |
| 3 | 3 | Chose Payment Method | `step=pay` in URL | Got to checkout step |
| 4 | 4 | Clicked Pay | manual JS `trackGoal()` | Submitted payment |
| — | 5 | Visited Signup | `/signup` | Sign-up flow entry (account creation) |
| — | 6 | Saw Pricing Landing | `/pricing` | Marketing-page view |
| — | 7 | Used File Feature | `event_category='File'` | Used core product feature ← largest by raw count |
| — | 8 | Engaged Visit 60s+ | `visit_duration ≥ 60` | Sustained engagement |
| — | 9 | Viewed Devices Page | `/devices` | Inspected multi-device limits |

This funnel is commonly referred to internally as `Stage 0..4`; Goal 4 ("Clicked Pay") is the bottom-of-funnel conversion target.

### Approximate funnel ratios (Matomo previous7, 2026-06)
```
~50K visits/day × 7d ≈ 350K visits
Goal 1 (Viewed Pricing):     ~4.5K conversions  → ~1.3% of visits view pricing
Goal 2 (Package Clicked):    ~200             → ~4% of pricing viewers pick a plan
Goal 3 (Chose Payment):      ~30              → small (URL pattern may miss real activity)
Goal 4 (Clicked Pay):        ~2.4K            → re-fired manually; harder to relate to step 3
Goal 5 (Visited Signup):     ~17K             → many sign-ups happen without pricing context
Goal 7 (Used File feature):  ~60K/week        → the daily-engagement signal
Goal 8 (Engaged 60s+):       ~2.9K/week       → meaningful-session indicator
```

(Numbers shift week-to-week; use Matomo HTTP API `Goals.get` for the authoritative current figure. CH currently undercounts some goals — goals 6–9 are frozen since 2026-05-27, and **goals 1–3 carry no `user_id`**, so per-user funnel math only works for stage 4 — see `SEEDR_DATA_GUIDE.md` §6 for details and workarounds.)

---

## 6. Email & retention infrastructure (mautic + transactional)

Seedr runs a high-volume email machine — much of the retention signal lives here.

> **Pipeline migration (2026-05-28):** the Mautic event stream ended on 2026-05-28; since then **all email** (marketing/bulk + transactional) flows into CH under `src='internal_events'`. Mautic data below is the historical record.

### Marketing email (Mautic — `src='mautic'`, 2025-01-10 → 2026-05-28, historical)
- ~1.4M `email.sent` per month (lifetime total 14.8M — an earlier "15M/month" figure conflated the cumulative with a monthly rate)
- Lifetime: **18.97% open rate**, **1.60% click rate of opens** (typical industry numbers)
- `metadata` JSON has `email_id` and `open_count` per event.

### All email since 2026-05-28 (server-side — `src='internal_events'`)
- ~820K `email.sent` per 30 days — transactional (~330K: receipts, password resets, billing notifications) plus migrated bulk/marketing campaigns (visible as batch spikes, e.g. 336K sends on 2026-06-07)
- Post-migration engagement: **~14.8% open / ~0.6% click-of-opens** — noticeably below the Mautic-era 19%/1.6%; whether this is real engagement loss or a tracking-parity gap is an open question for the email team
- Always tied to a `user_id` (no anonymous recipients).

Behavioral segments (e.g. `cart_abandonment`, `power_users_no_subscription`, `heavy_streamers` — see §8) define **audience targets for these mautic campaigns** — joining ClickHouse behavioral data with marketing infrastructure.

---

## 7. Technical infrastructure

A high-level map of how the production system is structured — services, hosts, languages — and where data flows.

### Service topology

Seedr is split into three large pieces plus a constellation of stateless workers:

| Service | Language | Role |
|---|---|---|
| **seedr_main** | PHP | The "gateway": serves the marketing site + web app under `/`, `/app/...`, handles OAuth/auth, owns user / file / billing URLs, talks to all DBs. Triggers Matomo + Mautic events. |
| **seedr_subnode** | Go 1.25 | The "muscle": runs on 25+ processing nodes. Polls MySQL for tasks (torrent downloads, archive extraction, media transcodes), serves files over HTTPS / FTP / SFTP / WebDAV / S3 from local storage, generates HLS streams. One binary (`server_clean`) hosts all protocol servers. Polls MySQL for assigned tasks (no message queue). |
| **Partytime / payments_ver2** | (separate repo) | The billing engine. Owns payment-provider integration (PayPal etc.), subscription state machine, refund/chargeback flows. Emits the `subscription.*` events seen in CH telemetry. |

A subnode does **not** do payments, Matomo tracking, or email sending — those belong to seedr_main and Partytime. A subnode **does** own torrent downloading, file storage, file serving, and all media conversion.

### Subnode tech stack (verified from `seedr_subnode/go.mod`)

| Component | Detail |
|---|---|
| Language | Go 1.25 |
| HTTP framework | Gin (`github.com/gin-gonic/gin`) |
| BitTorrent engine | A modified fork of `anacrolix/torrent` (custom mmap storage) |
| Cloud-source backends | rclone (`github.com/rclone/rclone`) — Google Drive, Dropbox, Mega, TeraBox, S3, Backblaze B2, Aria2, JDownloader |
| Media | FFmpeg subprocess for HLS/audio/image transcoding; `pdfcpu`, `libheif`, `go-mp4`, `go-mkvparse` for format-specific parsing |
| Archives | `mholt/archives` — ZIP, RAR, 7z, TAR, ISO, multi-part archives |
| Local KV stores | NutsDB (torrent state, file hashes), BoltDB (mmap piece data) |
| Caching / sessions | Redis via `redis/rueidis` |
| Distributed FS | `v1fs` (custom abstraction in `red_shared/v1fs`) — content-addressed dedup across users, files stored on disk as `/var/www/d{1..4}/files/{sha1-hash}` |

### Storage & dedup
Files are addressed by SHA1 hash, not user. If two users seed the same torrent, the underlying file is stored once and referenced twice — that's the **shared data store** layer in subnode (`src/task/usertorrenttask/shareddatastore.go`).

### Production data systems

| System | Host | What lives there | Access |
|---|---|---|---|
| **ClickHouse warehouse** | `data.seedr.cc:8123,9000` | `seedr_telemetry` (analytics core: `user_telemetry_events, vid_user_map, user_subscription_state, revenue_facts` + edge/QoS log `request_events`, minute rollups, node/ops telemetry) and `payments` (billing-service observability) — full inventory in `SEEDR_CLICKHOUSE_REFERENCE.md` | SELECT on all; **read-write in `ml.*` + `shaya.*`** (role `shaya_rw`, verified 2026-06-16); prod telemetry effectively read-only |
| **Local MySQL on data box** | `data.seedr.cc:3306` | `red_data, agent, stat, pulse` (auxiliary services) | read-only via tunnel |
| **Central task / catalog MySQL** | `my.seedr.cc` (a.k.a. `seedr.cc`) | `seedr.uc_users` (emails, account meta), `seedr.user_subscriptions` (live billing state), the `tasks`/`task_status`/`user_torrents`/`files` tables that the 25+ subnodes poll | separate box, separate tunnel — request from infra |
| **Billing MySQL** | `payment.seedr.cc` | `payments_live_1` — Partytime payment pipeline state | separate box, separate tunnel |
| **Matomo Reporting** | `stat.repora.com` | site_id=2; web/event/goal analytics back to 2019-07-01 | HTTPS + bearer token |

**The CH warehouse is the primary data source for everything analytical.** Marketing campaigns, ML predictions, dashboards, audit/QA, and any new project consuming Seedr data should start from CH. The systems below are *upstream emitters* that feed CH; you read them directly only when you need live operational state (e.g., a dunning flag that hasn't yet been emitted as an event).

### Tracking stack (upstream feeders of CH)
- **Matomo** (`stat.repora.com`, site_id=2, timezone `Asia/Jerusalem`) — the analytics tracker fired from seedr_main + browser. Its events flow through a server-side ingest pipeline into CH (`user_telemetry_events` with `src='matomo'` historically; `src=''` since the 2026-05-27 migration). You query Matomo's HTTP API directly only for the two legacy gaps documented in `SEEDR_DATA_GUIDE.md` §10.
- **Mautic** — the marketing-automation platform. Its CH event stream (`src='mautic'`) ended 2026-05-28; email events (incl. campaigns) now arrive via `src='internal_events'`.
- **Subnode workers (Go)** — emit `task.*` events with `src='internal_events'`.
- **Partytime billing service** — emits `subscription.*` lifecycle events with `src='internal_events'` and `metadata.partytime_event_type`.
- **Account-management code in seedr_main** — emits `account.storage_warning` and similar.

Net effect: **every analytically interesting event ends up in `user_telemetry_events`**, tagged by `metadata.src`, joinable by `user_id`.

### Identity surface
- Browser-side: `vid` (UUIDv4) stored in localStorage/cookie. Travels in tracking events.
- Account-side: `user_id` (UInt64, = `uc_users.id`) attached to events once the user is logged in.
- Email-side: same `user_id` used for mautic/internal emails.

The Mautic-style email is the only system where `user_id` exists without any `vid` (no browser context).

---

## 8. Key business segments

Eight named marketing/behavioral segments are commonly used for audience targeting and internal discussion at Seedr. They join ClickHouse behavioral data with subscription state (`user_subscription_state`) to produce mailable / addressable cohorts.

| ID | Name | Audience | Filter |
|---|---|---|---|
| A | `power_users_no_subscription` | free | Highly engaged users (downloads + streams > 100) who never visited pricing |
| B | `heavy_streamers` | free | Streams a lot, downloads few files (>50 streams, <10 dl) |
| C | `heavy_downloaders` | free | Files-focused users (>50 downloads) |
| D | `archive_only` | free | Only ever touches archives (zip/rar/etc.) |
| E | `cart_abandonment` | free | Made it into funnel stages 1, 2, 3 but never reached Stage 4 |
| F | `geo_emerging_markets` | free | India, Indonesia, Philippines, etc. (the 14-country list above) |
| G | `upsell_existing_subscribers` | premium | All current paying customers, for upsell campaigns |
| H | `mobile_first` | free | `is_mobile=1` |

Orthogonal to the marketing segments, **value cohorts** (Platinum / Gold / Silver / Bronze / Prospects) are used to talk about users by lifecycle stage, typically derived from funnel stage + visit count + payment attempts.

> ⚠️ **Threshold calibration (2026-06-11):** measured live, the A–C definitions above are nearly empty on a 30-day window — segment A (>100 downloads+streams) yields only ~178 users, and segment B is invisible because streaming is under-tracked in web events (~2.6K users/30d via `video/stream_start` vs ~18.8K streamers/7d in the edge log). Power/streamer segments should be rebuilt on `bw_user_day` bandwidth percentiles and `request_events` `/media` traffic. A recalibrated, research-backed segment portfolio (incl. win-back: 3.8K lapsed payers still active; soft-cancel: ~1.2K/mo; quota-pressure: ~1K/mo storage warnings + 7.9K free users >10GB/wk) is in `SEEDR_MARKETING_SEGMENTS.md`.

---

## 9. Vocabulary cheat-sheet

Terms used internally with specific meanings:

| Term | Meaning |
|---|---|
| **Visit / Session** | Matomo concept — a series of pageviews within a 30-min idle window. CH approximates this via `session_end` events. |
| **Action** | Anything a user does in a visit — pageview, event, download, outlink. Matomo `nb_actions`. |
| **Goal** | A defined conversion event (1–9). See §5. |
| **Funnel stage** | A user's furthest-reached goal in the canonical 4-step purchase funnel. Stored in `visitor_journeys.funnel_stage` (0–4). |
| **vid** | UUID visitor identifier in CH (93% v5 / 7% v4). Browser-bound; one user averages ~3.7 vids. |
| **idvisitor** | Matomo's 16-hex visitor identifier. ≠ vid. |
| **user_id** | Seedr account ID (= `uc_users.id`). The only safe cross-system identity key. |
| **Task** | A background job processed by a subnode worker. Types include torrent download, generic URL/cloud download, archive extraction, video/audio/image conversion, document processing. Lives in `/app/tasks/<id>`. Emits `task.completed/failed` events. |
| **Surface** | LowCardinality CH column describing the UI surface where the event originated (`landing`, `email`, `subscription`, `task`, ...). |
| **Cancellation_scheduled** | "Soft cancel" — user clicked cancel but still has access until period end. **Prime retention window.** |
| **Grace period** | Payment failed but service continues for N days while retrying. |
| **Mautic** | The marketing-email platform. Sender of campaign emails. CH stream ended 2026-05-28; email events now flow as internal events. |
| **Internal events** | Server-side telemetry, NOT user clickstream. Subscription state, transactional emails, background tasks. |
| **Power user** | Highly engaged free user, conventionally defined as `(streams_watched + files_downloaded) > 100`. Segment A in §8. |
| **seedr_main** | The PHP gateway service — marketing site, web app, auth, central catalog. Owns `/`, `/app/...`, `/signup`, `/pricing`, etc. |
| **seedr_subnode** | One of 25+ Go workers. Polls MySQL for tasks (downloads, transcodes), serves files over HTTP/FTP/SFTP/WebDAV/S3, generates HLS streams. Repo: `seedr_subnode`. |
| **Partytime** | The internal billing engine (separate repo, `partytime/payments_ver2`). Emits the `subscription.*` events; metadata fields `partytime_event_type` and `subscription_id` come from here. |
| **Presentation** | A media-asset URL namespace served by the subnode (`/presentations/...`) — HLS video, audio re-encodes, thumbnails, archive listings. |
| **v1fs** | Custom distributed content-addressed filesystem used by subnodes. Files are keyed by SHA1 hash → enables cross-user dedup. |
| **rclone source** | Any remote storage provider that subnode can pull a file from (Google Drive, Dropbox, Mega, TeraBox, S3, B2, …). |
| **HLS** | HTTP Live Streaming — Apple's adaptive-bitrate format. Subnode transcodes uploaded videos into HLS for in-browser playback. Quality ladder is plan-tier-gated. |

---

## 10. What an LLM should know in one paragraph

> Seedr.cc is a cloud BitTorrent and remote-download service running since 2016. Users submit magnets, .torrent files, or URLs from cloud sources (Google Drive, Dropbox, Mega, TeraBox, S3, B2, …); Seedr's servers fetch the content and let users stream or download it via HTTPS, FTP, SFTP, WebDAV, or S3-compatible APIs. Built-in archive extraction and adaptive HLS video transcoding (FFmpeg) make Seedr a full content-handling layer, not just a downloader. The architecture splits into a PHP gateway (`seedr_main`, the website + APIs), 25+ Go processing nodes (`seedr_subnode`, the torrent/download/transcode workers + multi-protocol file servers), and a separate billing engine ("Partytime"). The business is freemium — ~70K weekly-active users, ~3.2K paying subscribers, $6.95–$19.95 monthly tiers (PayPal + Paddle), plan tiers gating video quality (480p free vs 720p+ premium) and protocols like WebDAV. The user base skews heavily toward emerging markets (India, Indonesia, Philippines, etc.). Marketing is email-driven (~820K sends/30d via the internal pipeline; the Mautic stream ended 2026-05-28). Conversion is tracked via a 4-stage Matomo funnel (Viewed Pricing → Package Clicked → Chose Payment → Clicked Pay). **All analytically interesting telemetry — web events, email engagement, subscription lifecycle, payments, plus the raw edge/QoS request log — fans into a single ClickHouse warehouse at `data.seedr.cc` (`seedr_telemetry.user_telemetry_events`, ~146M rows; `request_events`, ~25M rows/day), which is the primary data source for any new project.** Tools and services join on `user_id` for cross-source consistency.

---

## 11. References for deeper context

- `SEEDR_DATA_GUIDE.md` — the companion technical data-warehouse handbook (schema, connection, query recipes).
- `SEEDR_CLICKHOUSE_REFERENCE.md` — full inventory of both CH databases (edge/QoS log, rollups, ops logs, payments DB).
- `SEEDR_DATA_VERIFICATION_2026-06-10.md` — latest live-data verification (what to trust, what's broken).
- `SEEDR_MARKETING_SEGMENTS.md` — verified segmentation research (2026-06-11) + live segment sizing and the recommended campaign portfolio.
- `https://www.seedr.cc` — the production website.
- `https://stat.repora.com` — Matomo dashboard (site_id=2). Login required.

---

## 12. What this doc deliberately does NOT cover

- **Internals of the BitTorrent client** — the `seedr_subnode` repo has the production answers (modified `anacrolix/torrent` fork, custom mmap storage, shared-data dedup). Read its source if you need that detail.
- **Quota enforcement specifics** — broadly: per-user storage limits stored in MySQL, gated by the subnode's `GetQuota()` / auth service. Exact thresholds and abuse-mitigation policy: ask the platform team.
- **Competitor positioning, marketing strategy specifics, brand voice** — out of scope; ask the growth/marketing team.
- **Exact pricing tiers and plan IDs** — only partial signal observed. Authoritative source is the `seedr.cc/pricing` page and the central MySQL.
- **Compliance / legal / DMCA process** — outside the scope of this overview.

If your project needs any of these, start by asking a Seedr team member directly. Don't infer from telemetry alone.
