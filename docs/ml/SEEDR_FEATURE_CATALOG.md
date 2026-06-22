# Seedr — Feature Catalog & roadmap (what we have, what to add)

2026-06-22. Inventory of features across all sources for the LTV / churn /
conversion / retention models, plus what can still be mined from ClickHouse.

## 1. Currently in the models
- **Telemetry (user_telemetry_events):** email engagement, web behaviour, funnel goal4,
  intent (`promo_sub`/visited_subscription), sessions, monetary/RFM, subscription lifecycle,
  profile (country/devices/mobile), 8 segment flags.
- **Revenue (revenue_facts, 10y):** LTV, frequency, recency, gaps, plan, provider, refunds.
- **Billing (Partytime metadata):** `expires_on` (exact renewal date), `has_active_payment_method`
  → involuntary-churn rule (validated 120/120).

## 2. NEW this round
### 2a. Content affinity — `ml.user_content` (13.6K users, 42 cols)
From the Seedr admin FS API (`/tree`). Persona (video_streamer 60% / empty 24% / …),
storage/library GB, by-category counts+GB, shares, `days_since_last_add`, `last_signin_day`,
`account_age_days`, `bandwidth_used_gb`. **Enriched (free, same call):** `n_folders`,
`n_lost_files` (library degradation), `files_added_30/90d` (freshness/activity),
`library_age_days`, `saw_walkthrough` (onboarding done), `is_business`/`is_referred`/`has_sso`
(acquisition), `lost_pw_requests`, `totp_enabled`, `invites_accepted`, `gb_archive/image`.

### 2b. Edge / QoS — `ml.user_edge` (92K users, built from request_events 7d)
The big untapped behavioural signal. Per user (last 7d):
`edge_requests_7d, edge_gb_7d, media_requests_7d` (streaming volume), `stream_gb_7d`,
**`n_rate_limited_7d`** (429 hits = quota/throttle pressure — 14,477 users affected!),
`n_stall_7d` (QoE problems — 57,917 users; churn risk), `n_errors_7d`, `distinct_files_7d`,
`active_days_7d`, `h3_share`, `app_requests_7d`, `n_countries`.

## 3. Still available in ClickHouse — to add next (with volumes)
| Source | Features | Volume (30d) | Value | Caveat |
|---|---|---|---|---|
| **task.\*** (subnode jobs) | downloads completed/failed per user, **task_failure_rate** (frustration), task volume/intensity | 1.49M completed / 170K users; 20.5K failed / 7.2K users | strong usage + frustration signal | since 2026-05-25 (recent only) |
| **email campaigns** (`email_id`,`open_count`,`kind`) | which campaign converts, per-email open_count, campaign affinity | 115 campaigns, 1.52M sends | campaign attribution + uplift | needs join by email_id |
| **payments DB** (`payment_app_events`) | `GRACE_WATCH` (dunning, 231 users), `RECONCILIATION_CRITICAL` (255), provider webhook failures, `MAUTIC_RETRY` | 37K USER_STATE / 9K users | **involuntary-churn / billing-health** — sharpens churn risk | window from 2026-06-02 |
| **request_events extras** | device/proto mix, ASN/ISP, time-of-day, upstream latency, RTT, stall_ms | 138M rows/7d | richer QoE/engagement | recent-only, heavy scans → use rollups |
| **funnel goals 1–3** (vid) | pricing-view / package-click intent (no user_id → via vid_user_map) | g1≈5K/90d | conversion intent | goals 1–3 carry no user_id |
| **bw_user_day trend** | bandwidth trend (this week vs last), per-user daily series | per-user rollup | consumption trajectory | request_events TTL 90d |
| **playback_events** (QoE) | time-to-first-frame, playback errors | empty when last checked | direct stream-QoE | re-check; was empty |

## 4. Priority to add (gain × effort)
1. **task.\* usage features** (downloads, failure_rate) → churn & LTV. Cheap, strong, in CH. **P1.**
2. **Edge `ml.user_edge`** (DONE) → join into churn/LTV/Customer-360; `n_rate_limited` = upsell trigger.
3. **payments billing-health** (GRACE_WATCH / reconciliation) → involuntary-churn precision. **P1.**
4. **email `email_id` campaign** features → campaign attribution + the uplift loop. **P2.**
5. **Storage quota** for `storage_used_pct` (the one content gap) — derive quota from
   plan/`billing_plan_id` or `/dynamic/get_space`. **P2.**

## 5. Caveat — temporal coverage
content / edge / task / payments streams are **recent-only** (May–June 2026 onward), so they
add little to *historical* model training (the content lift test showed +0.01 AUC, blunted by
the now-vs-history mismatch) but are excellent for **current scoring + campaigns**, and will
lift models properly once we train on **live snapshots** (start snapshotting now → label later).
