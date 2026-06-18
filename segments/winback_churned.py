#!/usr/bin/env python3
"""Win-back segmentation of users who churned in the last 30 days.

Pulls the ~591 churned subscribers (subscription.canceled/expired in 30d) from
ClickHouse with a rich profile, then assigns each a research-grounded win-back
GROUP (from the 106-agent segmentation research: involuntary vs voluntary churn,
value tiers, monthly-vs-annual, emerging-market/UPI price sensitivity, usage-PQL
product-fit, engagement recency) + a value tier + a data-driven k-means cluster.

Output: segments/churned_winback_30d.csv (master) + per-group CSVs + summary.
Each row = one user_id + everything marketing needs for a personalized win-back email.
"""
import base64
import io
import os
import urllib.request

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

OUT = os.path.dirname(__file__)
EMERGING = {"in", "id", "ph", "bd", "ng", "vn", "lk", "pk", "ke", "tz", "ug", "et", "gh", "mm"}


def creds():
    u = p = None
    for line in open(os.path.expanduser("~/.clickhouse.seedr")):
        k, _, v = line.strip().partition("=")
        if k == "user":
            u = v
        elif k == "password":
            p = v
    return u, p


USER, PW = creds()
AUTH = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()


def q(sql):
    req = urllib.request.Request("http://127.0.0.1:8123/", data=sql.encode(), method="POST")
    req.add_header("Authorization", AUTH)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode()


def df_q(sql):
    return pd.read_csv(io.StringIO(q(sql + " FORMAT TSVWithNames")), sep="\t")


# 1) churned base: latest canceled/expired per user, in last 30d
base = df_q("""
SELECT user_id,
       argMax(event_type, created_at) AS churn_type,
       toDate(max(created_at))         AS churn_date,
       dateDiff('day', max(created_at), now()) AS days_since_churn
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= now() - INTERVAL 30 DAY
  AND event_type IN ('subscription.canceled','subscription.expired') AND user_id IS NOT NULL
GROUP BY user_id
""")
ids = ",".join(map(str, base.user_id.tolist()))
print(f"churned users (30d): {len(base)}")

# 2) subscription history (all-time): reactivations + recent payment failure (involuntary signal)
sub = df_q(f"""
SELECT user_id,
       countIf(event_type='subscription.reactivated')   AS prior_reactivations,
       countIf(event_type='subscription.created')        AS prior_creates,
       countIf(event_type='subscription.payment_failed' AND created_at >= now() - INTERVAL 45 DAY) AS recent_payment_failed,
       countIf(event_type='subscription.cancellation_scheduled' AND created_at >= now() - INTERVAL 45 DAY) AS recent_cancel_sched
FROM seedr_telemetry.user_telemetry_events
WHERE event_type LIKE 'subscription.%' AND user_id IN ({ids})
GROUP BY user_id
""")

# 3) revenue / value (since 2016)
rev = df_q(f"""
SELECT user_id,
       round(sum(amount_usd),2)                  AS ltv_usd,
       count()                                   AS txns,
       round(argMax(amount_usd, transaction_date),2) AS last_amount,
       argMax(billing_plan_id, transaction_date) AS last_plan_id,
       toDate(min(transaction_date))             AS first_purchase,
       toDate(max(transaction_date))             AS last_purchase,
       dateDiff('day', min(transaction_date), max(transaction_date)) AS customer_lifespan_days,
       anyHeavy(currency)                        AS currency
FROM seedr_telemetry.revenue_facts
WHERE status='completed' AND user_id IN ({ids})
GROUP BY user_id
""")

# 4) engagement (last 120d) + profile
eng = df_q(f"""
SELECT user_id,
       countIf(event_type='pageview')                              AS pageviews_120,
       uniqExactIf(toDate(created_at), surface IN ('web','landing')) AS web_active_days_120,
       countIf(category='File'    AND action='Download')           AS file_dl_120,
       countIf(category='Archive' AND action='Download')           AS archive_dl_120,
       countIf(category='video'   AND action IN ('stream_start','stream_session')) AS streams_120,
       countIf(category='Promotional' AND action='visited_subscription') AS promo_sub_120,
       dateDiff('day', max(created_at), now())                     AS days_since_last_web,
       countIf(event_type='email.sent')                           AS em_sent,
       countIf(event_type='email.opened')                         AS em_opened,
       countIf(event_type='email.clicked')                        AS em_clicked,
       anyHeavy(country)                                          AS country,
       round(countIf(match(ua,'Mobile|Android'))/greatest(countIf(ua!=''),1),2) AS mobile_share,
       uniqExact(vid)                                            AS devices
FROM seedr_telemetry.user_telemetry_events
WHERE user_id IN ({ids}) AND created_at >= now() - INTERVAL 120 DAY
GROUP BY user_id
""")

df = base.merge(sub, on="user_id", how="left").merge(rev, on="user_id", how="left").merge(eng, on="user_id", how="left")
df = df.fillna({"ltv_usd": 0, "txns": 0, "last_amount": 0, "prior_reactivations": 0,
                "prior_creates": 0, "recent_payment_failed": 0, "recent_cancel_sched": 0,
                "file_dl_120": 0, "archive_dl_120": 0, "streams_120": 0, "promo_sub_120": 0,
                "pageviews_120": 0, "web_active_days_120": 0, "em_sent": 0, "em_opened": 0,
                "em_clicked": 0, "mobile_share": 0, "devices": 0, "customer_lifespan_days": 0})
df["days_since_last_web"] = df["days_since_last_web"].fillna(999).astype(int)
df["country"] = df["country"].fillna("unknown")
df["email_open_rate"] = (df.em_opened / df.em_sent.replace(0, np.nan)).fillna(0).round(3)
df["total_downloads"] = df.file_dl_120 + df.archive_dl_120
df["is_annual"] = ((df.last_amount >= 60) | (df.customer_lifespan_days / df.txns.clip(lower=1) > 180)).astype(int)
df["is_emerging"] = df.country.isin(EMERGING).astype(int)

# ---- value tier (research "Platinum/Gold/Silver/Bronze/Prospect" by LTV) ----
def tier(ltv):
    if ltv >= 200: return "Platinum"
    if ltv >= 75:  return "Gold"
    if ltv >= 25:  return "Silver"
    if ltv > 0:    return "Bronze"
    return "Prospect"
df["value_tier"] = df.ltv_usd.apply(tier)

# ---- research-grounded win-back GROUP (mutually exclusive, priority order) ----
def group(r):
    if r.recent_payment_failed > 0:
        return "involuntary_payment_failed"          # billing failure, not a choice → "fix payment"
    if r.ltv_usd >= 150 or r.value_tier == "Platinum":
        return "vip_high_value"                       # concierge, generous offer
    if r.is_annual:
        return "loyal_annual"                         # annual renewal offer
    if r.prior_reactivations >= 1:
        return "serial_reactivator"                   # came back before → light nudge
    if r.total_downloads + r.streams_120 >= 50:
        return "heavy_user_productfit"                # lead with the feature they used
    if r.is_emerging and r.ltv_usd < 50:
        return "emerging_price_sensitive"             # UPI / local price / discount
    if r.days_since_last_web <= 14:
        return "engaged_recent"                       # just here → "come back"
    return "dormant_lowvalue"                          # cheap batch / biggest discount
df["winback_group"] = df.apply(group, axis=1)

OFFER = {
    "involuntary_payment_failed": "Update payment method — your files are safe (no discount needed)",
    "vip_high_value": "Personal concierge win-back + premium discount / loyalty perk",
    "loyal_annual": "Annual plan welcome-back discount (they were committed)",
    "serial_reactivator": "Light reactivation nudge + small returning-user credit",
    "heavy_user_productfit": "Lead with the feature they used most (HD streaming / storage)",
    "emerging_price_sensitive": "Local price / UPI checkout / steepest discount",
    "engaged_recent": "'You were just here' — finish what you started, short offer",
    "dormant_lowvalue": "Low-cost batch: biggest discount or win-back drip",
}
df["recommended_offer"] = df.winback_group.map(OFFER)

# ---- data-driven k-means (complementary view) ----
feat = pd.DataFrame({
    "ltv": np.log1p(df.ltv_usd), "tenure": np.log1p(df.customer_lifespan_days),
    "recency_churn": df.days_since_churn, "recency_web": np.log1p(df.days_since_last_web),
    "downloads": np.log1p(df.total_downloads), "streams": np.log1p(df.streams_120),
    "reacts": df.prior_reactivations, "open_rate": df.email_open_rate,
    "pay_fail": df.recent_payment_failed, "annual": df.is_annual,
})
Xs = StandardScaler().fit_transform(feat.fillna(0))
df["kmeans_cluster"] = KMeans(n_clusters=5, random_state=42, n_init=10).fit_predict(Xs)

# ---- save ----
cols = ["user_id", "winback_group", "value_tier", "recommended_offer", "kmeans_cluster",
        "churn_type", "churn_date", "days_since_churn",
        "ltv_usd", "txns", "last_amount", "last_plan_id", "is_annual", "value_tier",
        "first_purchase", "last_purchase", "customer_lifespan_days",
        "prior_reactivations", "recent_payment_failed", "recent_cancel_sched",
        "country", "is_emerging", "mobile_share", "devices",
        "web_active_days_120", "pageviews_120", "total_downloads", "streams_120",
        "promo_sub_120", "days_since_last_web",
        "em_sent", "em_opened", "em_clicked", "email_open_rate"]
cols = list(dict.fromkeys(cols))  # dedupe value_tier
master = df[cols].sort_values(["winback_group", "ltv_usd"], ascending=[True, False])
master_path = os.path.join(OUT, "churned_winback_30d.csv")
master.to_csv(master_path, index=False)

print(f"\nmaster: {master_path}  ({len(master)} users)")
print("\n=== win-back groups ===")
g = df.groupby("winback_group").agg(users=("user_id", "size"), avg_ltv=("ltv_usd", "mean"),
                                    total_ltv=("ltv_usd", "sum")).round(0).sort_values("users", ascending=False)
print(g.to_string())
print("\n=== value tiers ===")
print(df.value_tier.value_counts().to_string())
print("\n=== k-means clusters (size, avg LTV, avg days_since_churn) ===")
print(df.groupby("kmeans_cluster").agg(n=("user_id", "size"), ltv=("ltv_usd", "mean"),
                                       dchurn=("days_since_churn", "mean")).round(1).to_string())

# per-group CSVs for the campaign
gdir = os.path.join(OUT, "by_group")
os.makedirs(gdir, exist_ok=True)
for grp, sub_df in master.groupby("winback_group"):
    sub_df.to_csv(os.path.join(gdir, f"{grp}.csv"), index=False)
print(f"\nper-group CSVs: {gdir}/<group>.csv")
