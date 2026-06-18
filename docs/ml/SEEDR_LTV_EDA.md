# Seedr — Big LTV dataset: Data Analysis (EDA)

Source `train_ltv_big.csv.gz` (built by `ml/ltv2_dataset.py`). Monthly index dates over the active payer base (recency ≤ 2y). Target = net revenue in the next 365 days.

- **Rows:** 647,475  ·  **distinct users:** 23,204  ·  **index span:** 2017-01-01 → 2025-06-01
- **Features:** 30 (+ id/date/label)

## 1. Target: forward 12-month revenue

- Zero (no future revenue / will lapse): **58.0%**  ·  any future revenue: **42.0%**
- Mean $34.29  ·  median $0.00  ·  among payers-with-future-rev: mean $81.55, median $83.40
- Percentiles $50:0/75:70/90:119/95:121/99:239  ·  max $2354
- **Revenue concentration (Gini): 0.741** — heavy-tailed; top-10% of rows hold 46% of future revenue.

## 2. Forward revenue by current value tier (LTV-to-date)

            rows  fwd_mean  fwd_share_%  pay_next_%
tier                                               
Platinum  117325     85.30         45.1        75.0
Gold      120376     50.09         27.2        60.0
Silver    140375     27.02         17.1        42.0
Bronze    269399      8.81         10.7        20.0

## 3. Emerging markets vs rest

            rows  fwd_mean  ltv_to_date  pay_next
emerging                                         
False     616594     34.95       116.41      0.42
True       30881     21.11        75.70      0.36

Top countries by future revenue (rows≥2000):
           rows  fwd_mean         rev
country                              
us       170446     58.03  9890985.97
gb        29349     50.15  1471834.51
ca        21818     50.94  1111493.80
au        17609     46.99   827503.09
fr        15710     47.01   738580.63
de         8127     50.69   411998.01
za         7217     48.02   346560.81
nl         4464     47.77   213260.11
no         3515     52.53   184647.38
sg         3347     53.55   179233.51

## 4. Monthly vs annual

              rows  fwd_mean  pay_next  ltv_to_date
monthly(0)  610220     33.86      0.42       110.47
annual(1)    37255     41.34      0.49       179.95

## 5. Provider

            rows  fwd_mean          rev
provider                               
paypal    603112     34.64  20893328.28
paddle     44363     29.50   1308859.31

## 6. Drivers: recency & frequency

Retention curve — P(any future revenue) by recency:
              rows  pay_next_%  fwd_mean
rec_bucket                              
0-30        218628        90.7    83.699
31-60        37711        48.0    31.536
61-90        27540        31.1    15.399
91-180       70840        23.7    11.596
181-365     119545        16.3     9.089
366-730     173211         6.3     2.205

By prior frequency (lifetime txns):
               rows  fwd_mean  pay_next_%
freq_bucket                              
1            177896      7.45        16.0
2             78092     14.58        26.0
3-5          108946     25.02        38.0
6-10          84303     39.58        52.0
11-20         82568     54.19        61.0
20+          115670     79.55        76.0

## 7. Persistence: last-12m revenue vs next-12m

- Spearman(rev_365_prior, label) = **0.680** — subscription revenue is sticky; 'next year ≈ last year' is a strong, hard-to-beat baseline.
- Among rows with rev_365_prior>0: 55% still have revenue next year (retention of active payers).

## 8. Numeric feature ↔ target (Spearman)

                 |spearman| vs label
rev_90                         0.756
txns_90                        0.752
rev_180                        0.725
txns_180                       0.721
recency_days                   0.694
rev_365                        0.680
txns_365                       0.661
avg_monthly_rev                0.643
monetary_sum                   0.506
frequency                      0.495
gap_max                        0.298
gap_median                     0.287

## 9. Implications for the LTV model

- Heavy tail + ~58% zero → log-target regression or two-part (P(pay)×E(rev|pay)); rank metrics (decile revenue-capture, Gini) matter more than MAE.
- `rev_365`/`monetary_sum`/`recency`/`frequency` dominate (classic RFM) → persistence is the baseline to beat.
- Tier/geo/cycle differences are real and usable for value-based targeting & regional pricing.
