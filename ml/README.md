# Seedr — ML: conversion-after-email models

Supervised propensity modelling on `train_email_conversion.csv.gz` (see
`../SEEDR_ML_DATASET.md` for the data dictionary). Reproduce with:

```bash
python3 -m venv .venv && .venv/bin/pip install pandas scikit-learn joblib
.venv/bin/python ml/train.py            # global model (both algos)
.venv/bin/python ml/train_segments.py   # per-segment models + comparison
```

## 1. Global model (target = `label_conv_14d`, free→paid within 14d)

- Clean: `-1` sentinels → NaN + missing flags; empty categoricals → named level;
  drop leak (`mobile_share_now`) and zero-variance columns.
- Normalize: signed `log1p` + `StandardScaler` (linear model only; trees are scale-free).
- Split: **chronological 80/20** by `send_date` (train ≤ 2026-03-24, test after).
  Random splits leak user-level future info.

| Model | test ROC-AUC | lift@1% | lift@5% | lift@10% |
|---|---|---|---|---|
| **LogisticRegression** | **0.953** | ×65 | ×17 | ×8.9 |
| HistGradientBoosting | 0.756 | ×49 | ×13 | ×6.9 |

**The linear baseline wins decisively.** After log-transform the signal is near-linear
and concentrated in monetary/segment features (`seg_winback_active`, `seg_dormant_payer`,
`txns_before`, `ltv_before_usd`, `tenure_days`, `days_since_web_activity`, `country`).
Training: LogReg 19s (1M subsample), GBM ~4min. Artifacts: `model_conv14.joblib`,
`test_predictions.csv.gz`.

True (population) conversion on test: **0.056%** for `label_conv_14d`, 0.30% for any
payment. Targeting the model's top-1% lifts realized conversion to ~3.7%.

## 2. Per-segment models vs the global model (same segment test rows)

Each segment: fresh `LogisticRegression` trained on that segment only, compared to the
global model scored on identical test rows. `label_conv_14d` for free segments;
`label_payment_14d` (renewal) for `monthly_loyal`. lift@top-20% within segment.

| Segment | target | test+ | seg AUC / lift | global AUC / lift | winner |
|---|---|---|---|---|---|
| heavy_downloader | conv | 115 | 0.912 / ×4.6 | **0.927 / ×4.5** | global |
| winback_active | conv | 625 | 0.757 / ×2.6 | **0.759 / ×2.5** | tie→global |
| dormant_payer | conv | 192 | **0.699 / ×2.2** | 0.629 / ×1.9 | **segment** |
| soft_cancel | conv | 15 ⚠ | 0.758 / ×2.0 | **0.783 / ×2.4** | global (noisy) |
| cart_abandoner | conv | 0 ⚠ | — | — | n/a (no test positives) |
| monthly_loyal | renewal | 7161 | 0.804 / ×1.5 | n/a (diff. target) | — |

### Conclusion: specialization barely helps — keep ONE global model

The global model ties or beats per-segment models on 3 of 4 conversion segments.
Per-segment training only helped `dormant_payer` (+0.07 AUC), and the small segments
(soft_cancel 15, cart_abandoner 0 test positives) don't have enough positives to train
a trustworthy standalone model. Reason: the global model already includes the segment
flags as features, so it learns segment-specific behaviour without fragmenting the data.

**Recommendation:** ship the single global LogisticRegression for free→paid scoring; use
the `monthly_loyal` renewal model (AUC 0.804) separately as a churn/retention scorer
(its ×1.5 lift is modest only because base renewal rate is already ~39%). Re-evaluate
per-segment models after mid-July 2026 when internal-era task/storage/stream features
(the strongest Usage-PQL signals) enter the training window.

## 2b. Neural backbone + swappable per-segment heads (the requested design)

One shared **backbone** (MLP 88→128→64→32, BatchNorm+Dropout) learns a
representation from all data; a tiny **head** (32→1, 33 params) maps it to a
prediction and can be swapped/fine-tuned per segment or per task. Files:
`nn_model.py` (arch), `nn_backbone.py` (train), `nn_finetune.py` (per-segment
heads), `nn_multitask.py` (co-trained heads). PyTorch CPU, ~1 min/epoch.

**Global backbone + head (conversion), test:** ROC-AUC **0.962**, lift@1% ×65 —
matches/edges the LogReg baseline while giving the modular head capability.

**v1 — frozen backbone + per-segment fine-tuned head** (transfer learning):

| Segment | seg-head AUC/lift | global-head AUC/lift | ΔAUC |
|---|---|---|---|
| heavy_downloader | 0.942 / ×4.7 | **0.956 / ×4.9** | −0.015 |
| winback_active | 0.746 / ×2.6 | **0.754 / ×2.6** | −0.008 |
| dormant_payer | 0.585 / ×1.3 | **0.620 / ×1.5** | −0.035 |
| soft_cancel (15+) | 0.652 / ×1.7 | 0.652 / ×1.7 | +0.001 |
| monthly_loyal (renewal) | **0.515** (near-random) | n/a | — |

Finding: head-only fine-tuning **does not beat** the global head — segments are
too small (head overfits the few positives) and the renewal head is near-random
because the conversion-only backbone never saw that task.

**v2 — multi-task backbone** (co-train conversion + renewal heads on ONE trunk):

| Head | test ROC-AUC | lift@1% | vs v1 |
|---|---|---|---|
| conversion | 0.962 | ×67 | = |
| **renewal** | **0.991** | ×88 | **0.515 → 0.991** |

Finding: co-training fixes it. The shared trunk now carries both signals, so the
renewal head is excellent (renewal timing is highly predictable from billing
cadence — good for churn/retention monitoring, **not** proof email caused it;
see uplift below). **This is the recommended structure: one multi-task backbone,
one head per objective, add/fine-tune heads per campaign as needed.**

**v3 — periodic numerical embeddings (MLP-PLR) + calibration** (`nn_plr.py`, 7 ep, 6.4 min):

| Head | test ROC-AUC | lift@1% | vs v2 |
|---|---|---|---|
| conversion | 0.951 | ×67 | −0.011 (no gain; PLR didn't help on this data) |
| renewal | 0.989 | ×88 | ≈ |

Calibration of the conv head (isotonic on a val slice): **ECE 0.174 → 0.0004**,
Brier 7.7e-2 → 6.2e-4 (AUC rank-invariant). Per research, pos_weight reweighting
distorts probabilities; this makes them trustworthy for expected-value decisions.
Verified research synthesis: `../SEEDR_ML_RESEARCH.md`.

Serving: load `backbone_mt.pt` once + the chosen `head_*.pt`; embed → head → sigmoid;
apply `calibrator_conv.joblib` if you need calibrated probabilities (not just ranking).

## 3. How to raise realized conversion further (ranked)

1. **New features** — internal-era `task.*`/`storage_warning`/`stream` aggregates once
   they age past the label window (~mid-July). Highest expected gain.
2. **Uplift modelling** — needs a randomized email holdout (5–10% of each segment gets no
   send). Optimizes the *incremental* effect of the email, not "who pays anyway". This is
   the correct objective for budget decisions and the bridge toward bandit/RL targeting.
3. Calibrated probabilities + GBM/catboost tuning — marginal; linear already near-ceiling.

## Files

```
ml/train.py                  global pipeline (clean→normalize→split→train→eval)
ml/train_segments.py         per-segment models + head-to-head vs global
ml/model_conv14.joblib       global model (logreg + gb)
ml/test_predictions.csv.gz   global test-set scores
ml/segments/<seg>.csv.gz     per-segment training datasets (+ _summary.json)
ml/segments/models/<seg>.joblib  per-segment trained models
```
