# Seedr — Marketing Segments: Research & Recommendations

Deliverable of a two-part exercise (2026-06-11): (1) verified internet research on segmentation
for freemium cloud-download/streaming products; (2) live sizing of candidate segments against the
ClickHouse warehouse (`data.seedr.cc`). Companion docs: `SEEDR_PROJECT_OVERVIEW.md`,
`SEEDR_DATA_GUIDE.md`, `SEEDR_CLICKHOUSE_REFERENCE.md`.

Method: deep-research workflow (106 agents, 24 sources fetched, 119 claims extracted, 25
adversarially verified → 16 confirmed / 9 refuted) + ~12 live CH queries (windows noted per number).

---

## 1. What the verified research says

### 1.1 Benchmarks worth calibrating against (high confidence)

| Benchmark | Value | Source |
|---|---|---|
| Free→paid conversion, freemium self-serve ("good" / "great") | 3–5% / 6–8%, measured on 6-month signup cohorts | Lenny's Newsletter / OpenView (B2B-skewed; consumer typically 1–2.6%) |
| Day-0 conversion intent | 82% of trial starts happen the day the user arrives | RevenueCat 2025 (mobile data; extrapolation for web) |
| Year-1 retention: monthly vs annual plans | 17.0% vs 44.1% (~2.5× gap) | RevenueCat 2025 |
| Churn concentration | ~30% of annual subs cancel (auto-renew off) within the first month | RevenueCat 2025/2026 |

### 1.2 Segment taxonomy findings (medium-high confidence)

- **Usage-PQLs beat everything.** HubSpot's product-qualified leads converted 3–4× higher than
  marketing-qualified leads, and of their three PQL buckets (Hand-raise / **Usage-limit hitters** /
  Upgrade-gate hitters) the usage-limit bucket converted best. → quota/limit events are the
  top-priority campaign triggers.
- **Activation/habit milestones** (Reforge setup → aha → habit) are proven segment templates:
  e.g. setup = first fetch added, aha = first `task.completed` in N days, habit = activity on k of
  first 28 days. Lifecycle emails keyed to "milestone reached vs stalled".
- **Category-specific gates are proven upgrade triggers** in adjacent products: TeraBox throttles
  free downloads ~5.5× and compresses free streaming to 480p, and markets the lift; TorBox markets
  its 10GB→200GB quota lift at $3 as the headline conversion lever. Seedr's own pricing ladder
  (live-verified 2026-06-11: Lite $3.95/10GB → Basic $7.95/50GB/720p/2 slots → Pro $12.95/200GB/1080p/8
  slots → Master $19/1TB/4K/25 slots → Gold to 10TB/$119.95/100 slots) prices exactly three behavioral
  dimensions: **storage quota, streaming resolution, task concurrency** — each is a tele­metry-mappable
  segment, including paid→paid upsell.
- **India payments:** alternative payment methods (primarily UPI) ≈75% of India e-commerce volume;
  UPI AutoPay is the standard recurring rail (AFA-free ≤ ₹15K). → UPI-first checkout for the
  India segment is a conversion lever orthogonal to email.

### 1.3 Refuted / unverified — do NOT cite

- Trial-vs-freemium conversion bands (8–12%/15–25%) — killed in verification.
- Geo revenue-per-install deltas ($0.06–0.09 emerging markets) — killed.
- "UPI AutoPay drove 4,000 customers/day" — killed.
- Per-segment email open/click benchmarks, dunning-recovery benchmarks — nothing survived; open question.
- Direct-competitor (Real-Debrid/AllDebrid/Premiumize/put.io) segmentation practices — both claims killed;
  only TorBox's freemium funnel survived.

---

## 2. Live segment sizing (ClickHouse, 2026-06-11)

Base: ~104K logged-in web users / 30d; ~102.5K of them free; ~3.2K paying; 60% mobile-UA;
65% emerging markets (India alone 43%).

| Candidate segment | Definition used | Size | Note |
|---|---|---|---|
| **Win-back: active lapsed payers** | ever in `revenue_facts`, absent from `user_subscription_state`, active last 30d | **3,803** | bigger than the whole current paid base; LTV history available for personalization |
| Win-back: lapsed LTV ≥ $50 | same, lifetime LTV ≥ $50 | 8,705 | high-value reactivation pool |
| Win-back: recently lapsed | last txn within 180d, not subscribed | 3,982 | freshest intent |
| **Soft-cancel save** | `subscription.cancellation_scheduled` last 30d | 1,228/mo | still paying; prime save window |
| Churned (hard) last 30d | `subscription.canceled/expired` | 585/mo | post-churn win-back drip |
| **Storage-quota pressure** | `account.storage_warning` last 30d | 995/mo | the classic Usage-PQL trigger |
| **Heavy bandwidth, free** | `bw_user_day` > 10 GB / 7d, not subscribed | **7,920** | the real "power users" (818 users > 50 GB/7d incl. paid) |
| **Free streamers (quality-gated 480p)** | edge log `/media` hits last 7d, not subscribed | **18,317** | web events see only ~2.6K of them — use edge log |
| Cart abandonment | vids with goals 1–3, no goal 4, last 30d | 2,637 vids → **761 addressable** user_ids | real pool larger (goal 1 undercounted ~68%; goals 1–3 carry no user_id) |
| Archive-only users | Archive/Download only, last 30d | 24,382 | distinct use-case messaging |
| Heavy downloaders | >50 File-downloads / 30d | 669 | concurrency-upsell candidates |
| Dormant 30–60d | active in [60d,30d), silent last 30d | 36,000/mo | reactivation pool, lower propensity |
| New / resurrected last 30d | no activity in prior 30d window | 74,595/mo | onboarding/day-0 + habit-milestone series |
| Email-engaged | `email.opened` last 30d | 89,499 | engagement-based sub-targeting |
| Legacy "power_users_no_subscription" (>100 web actions/30d) | docs §8 definition | **178** | ⚠️ old web-event thresholds are broken post-migration — redefine on bandwidth/edge data |

### Data caveats discovered while sizing (not in the existing docs)

1. **`surface` era split:** before the 2026-05-24..27 migration web events carry `surface='web'`,
   after — `surface='landing'`. Any query spanning the migration must use `surface IN ('web','landing')`.
   (The data guide only mentions `'landing'`.)
2. **Streaming is invisible in web events:** `video/stream_start` covers ~2.6K users/30d while the
   edge log shows 18.8K streaming users/7d. Stream segments must be built from `request_events` /
   rollups, not web events.
3. Old §8 segment thresholds (>100 actions, >50 streams) produce near-empty segments on 30d windows;
   recalibrate on percentiles of `bw_user_day` / task counts.

---

## 3. Recommended segment portfolio (priority order)

Scoring = research-backed propensity × size × addressability (user_id for email) × data readiness.

### Tier 1 — launch first (trigger-based, highest propensity)

| # | Segment | Size | Campaign | Why first |
|---|---|---|---|---|
| 1 | **Usage-PQL bundle: quota & limit hitters** — storage warnings (995/mo) + free heavy-bandwidth (7.9K/7d) + concurrency-cap hitters | ~9K/mo | event-triggered upgrade email at the moment of the limit; tier matched to the gated dimension (storage→Lite/Basic, slots→Pro/Master) | best-converting trigger class in research (HubSpot Usage-PQL, TeraBox/TorBox playbook); fully addressable; real-time events |
| 2 | **Win-back: active lapsed payers** | 3.8K now (+~600 hard-churn/mo inflow) | "come back" offer personalized by past LTV & plan; recently-lapsed (≤180d) variant first | proven willingness to pay AND demonstrated current need (still using free); pool > entire paid base |
| 3 | **Soft-cancel save** | 1.2K/mo | immediate save-flow email/offer on `cancellation_scheduled`; pause/downgrade option | user still paying; docs already flag it as THE retention window; ~30%-in-month-1 benchmark says act fast |

### Tier 2 — high value, needs one fix or test each

| # | Segment | Size | Campaign | Dependency |
|---|---|---|---|---|
| 4 | **Free streamers (480p-gated)** | 18.3K/7d | "watch in HD" upsell; mobile-first creative (60% mobile) | build from edge log; quality-gating proven by TeraBox analog |
| 5 | **Cart abandoners** | 761 addressable/mo (real pool ~3-8K) | classic abandonment drip within 24h | fix goal 1–3 `user_id` attach in tracker to unlock full pool |
| 6 | **Monthly→annual upgrade** | subset of 3.2K payers | discount-for-annual switch | needs `billing_plan_id`→tier/cycle mapping from billing team; 2.5× retention gap as upside |
| 7 | **Onboarding: day-0 + first-28d habit milestones** | 74.6K new/resurrected per mo; ~5.3K new subs/mo | milestone-keyed series (setup/aha/habit); for new SUBSCRIBERS: first-30-days onboarding (anti month-1 churn) | define milestones on `task.completed`; verify day-0 pattern on own funnel data |

### Tier 3 — large but lower propensity / blocked

- **Dormant reactivation** (36K/mo) — cheap batch sends, expect low rates; combine with email-engagement filter (89.5K openers) to protect sender reputation.
- **India/UPI checkout** (44.9K active users, 43%) — a payments/checkout project (UPI AutoPay; razorpay already in the provider tail), not an email segment per se.
- **Archive-only** (24.4K) — distinct value-prop messaging; test as content angle, not a dedicated funnel.
- **Dunning / payment-failed** — research playbook strong, but the `refund/chargeback/grace` event streams are frozen since ~05-28..06-04; confirm Partytime emitter health first.

### Suggested replacement for legacy segments A–H

Keep F (geo) and H (mobile) as overlay dimensions, not standalone segments. Replace A/B/C thresholds
with percentile-based definitions on `bw_user_day` + `task.completed` counts (e.g. top-decile free
users). E (cart abandonment) survives but must be vid-based until goals 1–3 carry user_id.
G (upsell) becomes #6 (cycle switch) + the paid→paid quality/slots upsell from the pricing ladder.

---

## 4. Extraction tool (`mlshaya_segments`)

The segments above are implemented as a Go CLI in this directory (`main.go` + `segments.go`,
stdlib only). It reads `~/.clickhouse.seedr`, runs the segment queries strictly one at a time over
the SSH tunnel, and writes one CSV per segment (one row per `user_id` + scoring columns, ready to
join emails from `uc_users` downstream).

```bash
go run . -list                  # catalogue with business rationale
go run .                        # extract all → ./segments_out/<date>/
go run . -only winback-active   # selected segments, comma-separated
go run . -dry                   # print the SQL only
```

First live extraction (2026-06-11):

| Segment | Users | Output columns |
|---|---|---|
| `quota-storage-warning` | 995 | warnings_30d, last_warning_at, is_premium |
| `quota-bandwidth-free` | 8,899 | gb_7d, requests_7d |
| `winback-active` | 4,571 | ltv_usd, lifetime_txns, last_txn_at, last_seen_at, events_30d |
| `soft-cancel-save` | 847 | scheduled_at (current state, cancels/reactivations excluded) |
| `streamers-free-hd-upsell` | 15,393 | media_requests_7d, gb_streamed_7d, country |
| `cart-abandoners` | 682 | furthest_goal, last_touch_at, funnel_vids |
| `dormant-recent-payers` | 1,674 | ltv_usd, last_txn_at, lifetime_txns |
| `monthly-to-annual` | 1,413 | current_plan_id, last_amount_usd, txns_12mo, paid_12mo_usd |

Notes: `winback-active` (4,571) is larger than the §2 estimate (3,803) because the query correctly
spans both `surface` eras (`'web'`+`'landing'`); `cart-abandoners` (682 vs 761) now excludes users
who subscribed after abandoning; `soft-cancel-save` (847 vs 1,228 raw events) keeps only users whose
*latest* subscription event is still `cancellation_scheduled`.

## 5. Open questions

1. Per-segment email engagement benchmarks — unverified externally; derive from our own
   `email.opened/clicked` history per campaign instead.
2. Geo conversion/ARPU deltas — compute from `revenue_facts` × `country` (we have 10 years of txns).
3. Does the day-0 / month-1 mobile benchmark hold for Seedr's web funnel? Answerable from our own
   2016+ revenue + funnel data.
4. `billing_plan_id` ↔ tier/cycle mapping (esp. 1000-series) — required for #6; ask billing.
