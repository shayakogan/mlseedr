# `segments/` — structure & file descriptions

Win-back segmentation of subscribers who churned in the last 30 days (built 2026-06-18).

> **Where the data lives:** the CSVs contain user-level PII (user_id, LTV, country) and
> are **git-ignored** — they are NOT in this repo. The data is on the server in ClickHouse
> table **`ml.churned_winback_30d`** (591 rows). This repo holds only the code + docs.

```
segments/
├── winback_churned.py          # generator: pulls from ClickHouse, classifies, writes CSVs (IN GIT)
├── README.md                   # group definitions + recommended email offers     (IN GIT)
├── STRUCTURE.md                # this file — tree + descriptions                   (IN GIT)
├── churned_winback_30d.csv     # MASTER: 591 churned users × 33 cols              (PII → ClickHouse only)
└── by_group/                   # per-group lists for the email campaign           (PII → ClickHouse only)
    ├── vip_high_value.csv              # 186 users, avg LTV $405 — concierge + perk
    ├── involuntary_payment_failed.csv  #  90 users — "update card", no discount
    ├── serial_reactivator.csv          # 131 users — light nudge + credit
    ├── engaged_recent.csv              # 106 users — "you were just here"
    ├── heavy_user_productfit.csv       #  39 users — lead with their feature
    ├── emerging_price_sensitive.csv    #  19 users — UPI / local price
    ├── loyal_annual.csv                #  15 users — annual welcome-back
    └── dormant_lowvalue.csv            #   5 users — cheap batch
```

## File descriptions

| File | In git? | What it is |
|---|---|---|
| `winback_churned.py` | ✅ | End-to-end builder: queries ClickHouse for the 30-day churned set + rich profile, assigns research-grounded win-back group + value tier + k-means cluster + recommended offer, writes the master and per-group CSVs. Re-runnable (`python segments/winback_churned.py`). |
| `README.md` | ✅ | Campaign guide: the 8 win-back groups, sizes, avg LTV, and the recommended email play per group; value tiers; k-means notes; column dictionary. |
| `STRUCTURE.md` | ✅ | This file. |
| `churned_winback_30d.csv` | ❌ PII → CH | Master table, one row per churned user with every feature + `winback_group`, `value_tier`, `recommended_offer`, `kmeans_cluster`. Mirror: `ml.churned_winback_30d`. |
| `by_group/<group>.csv` | ❌ PII → CH | Same rows split by `winback_group`, ready to hand straight to email. Reproduce by filtering `ml.churned_winback_30d` on `winback_group`. |

## On the server (ClickHouse)

```sql
-- whole set
SELECT * FROM ml.churned_winback_30d;
-- one campaign group (replace the by_group/*.csv files)
SELECT * FROM ml.churned_winback_30d WHERE winback_group = 'vip_high_value' ORDER BY ltv_usd DESC;
```

591 users · $110,833 lifetime revenue at risk. To get email addresses: join `user_id`
→ `uc_users` in the central catalog MySQL (`my.seedr.cc`).
