#!/usr/bin/env python3
"""Exploratory data analysis for the big LTV dataset (train_ltv_big.csv.gz).

Prints + writes docs/ml/SEEDR_LTV_EDA.md: target shape, revenue concentration,
forward-LTV by value tier / geo / plan-cycle / provider / recency / frequency,
persistence-baseline strength, retention curve, and feature↔target correlations.
"""
import functools

import numpy as np
import pandas as pd

print = functools.partial(print, flush=True)
DATA = "train_ltv_big.csv.gz"
TGT = "label_rev_365"
EMERGING = {"in", "id", "ph", "bd", "ng", "vn", "lk", "pk", "ke", "tz", "ug", "et", "gh", "mm"}
OUTMD = "docs/ml/SEEDR_LTV_EDA.md"
md = []


def emit(s=""):
    print(s); md.append(s)


def gini(x):
    x = np.sort(np.asarray(x, float)); n = len(x)
    if n == 0 or x.sum() == 0:
        return 0.0
    return float((2 * np.arange(1, n + 1) - n - 1).dot(x) / (n * x.sum()))


def tbl(df):
    return df.to_string()


def main():
    df = pd.read_csv(DATA)
    df["index_date"] = pd.to_datetime(df["index_date"])
    y = df[TGT].to_numpy()

    emit("# Seedr — Big LTV dataset: Data Analysis (EDA)\n")
    emit(f"Source `train_ltv_big.csv.gz` (built by `ml/ltv2_dataset.py`). "
         f"Monthly index dates over the active payer base (recency ≤ 2y). "
         f"Target = net revenue in the next 365 days.\n")
    emit(f"- **Rows:** {len(df):,}  ·  **distinct users:** {df.user_id.nunique():,}  "
         f"·  **index span:** {df.index_date.min().date()} → {df.index_date.max().date()}")
    emit(f"- **Features:** {df.shape[1]-3} (+ id/date/label)\n")

    # ---- target shape ----
    emit("## 1. Target: forward 12-month revenue\n")
    nz = y > 0
    emit(f"- Zero (no future revenue / will lapse): **{(~nz).mean()*100:.1f}%**  ·  "
         f"any future revenue: **{nz.mean()*100:.1f}%**")
    emit(f"- Mean ${y.mean():.2f}  ·  median ${np.median(y):.2f}  ·  "
         f"among payers-with-future-rev: mean ${y[nz].mean():.2f}, median ${np.median(y[nz]):.2f}")
    qs = [50, 75, 90, 95, 99]
    emit(f"- Percentiles ${'/'.join(f'{p}:{np.percentile(y,p):.0f}' for p in qs)}  ·  max ${y.max():.0f}")
    emit(f"- **Revenue concentration (Gini): {gini(y):.3f}** — heavy-tailed; "
         f"top-10% of rows hold {np.sort(y)[::-1][:len(y)//10].sum()/y.sum()*100:.0f}% of future revenue.\n")

    # ---- by value tier ----
    def tier(v):
        return ("Platinum" if v >= 200 else "Gold" if v >= 75 else "Silver" if v >= 25
                else "Bronze" if v > 0 else "Prospect")
    df["tier"] = df.monetary_sum.apply(tier)
    emit("## 2. Forward revenue by current value tier (LTV-to-date)\n")
    g = df.groupby("tier").agg(rows=("user_id", "size"), fwd_mean=(TGT, "mean"),
                               fwd_share=(TGT, "sum"), pay_next=(TGT, lambda s: (s > 0).mean())).round(2)
    g["fwd_share_%"] = (g.fwd_share / g.fwd_share.sum() * 100).round(1)
    g["pay_next_%"] = (g.pay_next * 100).round(1)
    emit(tbl(g[["rows", "fwd_mean", "fwd_share_%", "pay_next_%"]].sort_values("fwd_mean", ascending=False)) + "\n")

    # ---- geo ----
    df["emerging"] = df.country.isin(EMERGING)
    emit("## 3. Emerging markets vs rest\n")
    g = df.groupby("emerging").agg(rows=("user_id", "size"), fwd_mean=(TGT, "mean"),
                                   ltv_to_date=("monetary_sum", "mean"),
                                   pay_next=(TGT, lambda s: (s > 0).mean())).round(2)
    emit(tbl(g) + "\n")
    top = df.groupby("country").agg(rows=("user_id", "size"), fwd_mean=(TGT, "mean"),
                                    rev=(TGT, "sum")).query("rows>=2000").sort_values("rev", ascending=False).head(10)
    emit("Top countries by future revenue (rows≥2000):")
    emit(tbl(top.round(2)) + "\n")

    # ---- plan cycle ----
    emit("## 4. Monthly vs annual\n")
    g = df.groupby("is_annual").agg(rows=("user_id", "size"), fwd_mean=(TGT, "mean"),
                                    pay_next=(TGT, lambda s: (s > 0).mean()),
                                    ltv_to_date=("monetary_sum", "mean")).round(2)
    g.index = ["monthly(0)", "annual(1)"]
    emit(tbl(g) + "\n")

    # ---- provider ----
    emit("## 5. Provider\n")
    g = df.groupby("provider").agg(rows=("user_id", "size"), fwd_mean=(TGT, "mean"),
                                   rev=(TGT, "sum")).query("rows>=500").sort_values("rev", ascending=False).round(2)
    emit(tbl(g) + "\n")

    # ---- recency / frequency drivers ----
    emit("## 6. Drivers: recency & frequency\n")
    df["rec_bucket"] = pd.cut(df.recency_days, [0, 30, 60, 90, 180, 365, 730],
                              labels=["0-30", "31-60", "61-90", "91-180", "181-365", "366-730"])
    g = df.groupby("rec_bucket", observed=True).agg(rows=("user_id", "size"),
                                                     pay_next=(TGT, lambda s: (s > 0).mean()),
                                                     fwd_mean=(TGT, "mean")).round(3)
    g["pay_next_%"] = (g.pay_next * 100).round(1)
    emit("Retention curve — P(any future revenue) by recency:")
    emit(tbl(g[["rows", "pay_next_%", "fwd_mean"]]) + "\n")
    df["freq_bucket"] = pd.cut(df.frequency, [0, 1, 2, 5, 10, 20, 10000],
                               labels=["1", "2", "3-5", "6-10", "11-20", "20+"])
    g = df.groupby("freq_bucket", observed=True).agg(rows=("user_id", "size"),
                                                     fwd_mean=(TGT, "mean"),
                                                     pay_next=(TGT, lambda s: (s > 0).mean())).round(2)
    g["pay_next_%"] = (g.pay_next * 100).round(1)
    emit("By prior frequency (lifetime txns):")
    emit(tbl(g[["rows", "fwd_mean", "pay_next_%"]]) + "\n")

    # ---- persistence baseline strength ----
    emit("## 7. Persistence: last-12m revenue vs next-12m\n")
    sp = df[["rev_365", TGT]].corr(method="spearman").iloc[0, 1]
    emit(f"- Spearman(rev_365_prior, label) = **{sp:.3f}** — subscription revenue is sticky; "
         f"'next year ≈ last year' is a strong, hard-to-beat baseline.")
    emit(f"- Among rows with rev_365_prior>0: {((df.rev_365>0) & (df[TGT]>0)).sum()/(df.rev_365>0).sum()*100:.0f}% "
         f"still have revenue next year (retention of active payers).\n")

    # ---- feature correlations ----
    emit("## 8. Numeric feature ↔ target (Spearman)\n")
    num = df.select_dtypes("number").drop(columns=[TGT, "user_id"], errors="ignore")
    corr = num.corrwith(df[TGT], method="spearman").abs().sort_values(ascending=False).head(12)
    emit(tbl(corr.round(3).to_frame("|spearman| vs label")) + "\n")

    emit("## 9. Implications for the LTV model\n")
    emit("- Heavy tail + ~58% zero → log-target regression or two-part (P(pay)×E(rev|pay)); "
         "rank metrics (decile revenue-capture, Gini) matter more than MAE.")
    emit("- `rev_365`/`monetary_sum`/`recency`/`frequency` dominate (classic RFM) → persistence is the baseline to beat.")
    emit("- Tier/geo/cycle differences are real and usable for value-based targeting & regional pricing.")

    import os
    os.makedirs("docs/ml", exist_ok=True)
    open(OUTMD, "w").write("\n".join(md) + "\n")
    print(f"\nwrote {OUTMD}")


if __name__ == "__main__":
    main()
