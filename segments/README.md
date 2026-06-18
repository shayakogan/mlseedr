# Win-back: users who churned in the last 30 days

Built 2026-06-18 by `winback_churned.py` from ClickHouse. **591 churned subscribers**
(`subscription.canceled`/`expired` in the last 30 days), with **$110,833 of lifetime
revenue at risk**. Each user is classified into one research-grounded win-back **group**
(for the email message) + a **value tier** + a data-driven **k-means cluster**.

Files:
- `churned_winback_30d.csv` — master, 591 users, all features + group/tier/cluster/offer.
- `by_group/<group>.csv` — one file per win-back group, ready to hand to email.

## Win-back groups (priority order — send in this order)

| Group | Users | Avg LTV | Why / how to email |
|---|---|---|---|
| **involuntary_payment_failed** | 90 | $230 | Billing failed (payment_failed before cancel) — NOT a choice. "Update your card, your files are safe." **No discount needed; highest recovery rate.** Send first. |
| **vip_high_value** | 186 | **$405** | Top value (LTV ≥ $150 / Platinum). $75K of the $110K at risk. Personal/concierge tone + loyalty perk. Worth a human touch. |
| **loyal_annual** | 15 | $50 | Were on annual (committed). Annual welcome-back discount. |
| **serial_reactivator** | 131 | $50 | Have come back before (≥1 prior reactivation) — responsive. Light nudge + small returning-user credit. |
| **heavy_user_productfit** | 39 | $63 | Heavy downloads/streams — loved the product. Lead with the feature they used most (HD streaming / storage). |
| **emerging_price_sensitive** | 19 | $9 | Emerging-market + low LTV. Local price / UPI checkout / steepest discount (research: India ≈75% UPI). |
| **engaged_recent** | 106 | $46 | Active on site within 14d of churn. "You were just here — finish what you started." |
| **dormant_lowvalue** | 5 | $26 | Cold + low value. Cheap batch / biggest discount, or deprioritize. |

Groups are mutually exclusive (assigned by the priority above); the raw attributes are
in the master CSV so you can re-cut freely. `recommended_offer` column states the play.

## Value tiers (by lifetime revenue)
Platinum (≥$200) 171 · Gold (≥$75) 146 · Silver (≥$25) 113 · Bronze (>$0) 127 · Prospect 34.
317 of 591 are Platinum+Gold → most of the recoverable revenue sits in a targetable minority.

## k-means clusters (complementary, data-driven)
5 clusters on log-scaled LTV, tenure, churn/web recency, downloads, streams, reactivations,
email open-rate, payment-failure, annual flag. Cluster 0 (n=53, LTV $328) & 4 (n=191, LTV $268)
= high-value cohorts; cluster 1 (n=120, LTV $8) = low-value. Use as a cross-check on the
rule-based groups (`kmeans_cluster` column).

## Key columns (master CSV)
`winback_group, value_tier, recommended_offer, kmeans_cluster, churn_type, churn_date,
days_since_churn, ltv_usd, txns, last_amount, last_plan_id, is_annual, prior_reactivations,
recent_payment_failed, country, is_emerging, mobile_share, devices, web_active_days_120,
total_downloads, streams_120, days_since_last_web, em_sent/opened/clicked, email_open_rate`.

To get email addresses: join `user_id` → `uc_users` in the central catalog MySQL (`my.seedr.cc`).

## Reproduce
```bash
.venv/bin/python segments/winback_churned.py   # tunnel to data.seedr.cc must be open
```
