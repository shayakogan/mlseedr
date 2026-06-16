# Seedr — ML: "Conversion after a marketing email" — Final Report

Date: 2026-06-14. Companion: `SEEDR_ML_DATASET.md` (data dictionary), `ml/README.md`
(how to reproduce). Models & code in `ml/`.

## 1. Dataset

| Item | Value |
|---|---|
| Sample unit | (user_id, day a marketing email was received) |
| Candidate samples (full population) | **13,945,848** |
| Training file (after 25% negative downsampling, weighted) | **3,529,143 rows × 61 cols**, 55 MB gz |
| Features | 47 numeric + 2 categorical (→ 88 after one-hot) |
| Time split (chronological, no leakage) | train 2,574,440 · val 260,600 · test 694,103 |
| History window | sends 2025-06-26 → 2026-05-27 (effectively the mautic era) |
| Source extracts cached | 40 monthly ClickHouse aggregates, ~28M rows TSV (752 MB) |

### Segments
8 marketing segments defined; **6 have data** in the window, 2 are empty
(`storage_warning`, `streamer` — those event streams start after the window).
Per-segment datasets in `ml/segments/`:

| Segment | target | rows | positives |
|---|---|---|---|
| winback_active | conversion | 26,810 | 3,585 |
| monthly_loyal | renewal | 58,325 | 41,989 |
| heavy_downloader | conversion | 34,535 | 474 |
| dormant_payer | conversion | 9,248 | 1,079 |
| soft_cancel | conversion | 774 | 37 |
| cart_abandoner | conversion | 1,941 | 32 |

## 2. Conversion rate (test set, weighted = true population rate)

| Label | True rate | In top-1% by model |
|---|---|---|
| **label_conv_14d** (free→paid in 14d) | **0.056%** | **~3.7%** (lift ×67) |
| label_payment_14d (any payment incl. renewals) | 0.302% | — |

Base rate is tiny because mautic blasted broadly; the model's value is targeting.

## 3. Models, loss, training time, metrics

| Model | final train loss | train time | test ROC-AUC | lift@1% |
|---|---|---|---|---|
| LogisticRegression (baseline) | — | 19 s (1M subsample) | 0.953 | ×65 |
| HistGradientBoosting | — | ~4 min | 0.756 | ×49 |
| NN backbone + global head (v1) | 0.508 | ~8 min (8 ep) | 0.962 | ×65 |
| **NN multi-task backbone (v2)** | 0.703 | ~8 min (8 ep) | **conv 0.962 / renewal 0.991** | ×67 / ×88 |
| NN PLR multi-task (v3, periodic emb) | 0.625 | **6.4 min (7×55 s)** + eval | conv 0.951 / renewal 0.989 | ×67 / ×88 |

Losses are weighted BCE and NOT comparable across rows (different #tasks / pos_weight).
All training on CPU (8 threads), no GPU. Total project compute is minutes, not hours.

### Calibration (v3, research-driven)
pos_weight reweighting wrecks probabilities; isotonic recalibration on a val slice fixed it:

| conv head probabilities (test, weighted) | Brier | ECE |
|---|---|---|
| raw (pos_weight model) | 7.67e-2 | 0.174 |
| **isotonic-calibrated** | **6.20e-4** | **0.00042** |

AUC is unchanged by calibration (rank-invariant). This is the difference between
"top-k targeting works" (always did) and "predicted probabilities are trustworthy
for expected-value / budget decisions" (now they are).

## 4. Architecture delivered (what the user asked for)

**One shared backbone + swappable per-segment / per-task heads.** `backbone_mt.pt`
(95 KB) is the main model; each head is ~2 KB. Serve = backbone once + chosen head.

- **v1 (frozen backbone + per-segment fine-tuned head):** mechanism works, but
  per-segment heads do NOT beat the global head (segments too small; renewal head
  near-random 0.515 because the conversion-only backbone never saw that task).
- **v2 (multi-task co-training): the recommended structure.** Co-training conversion +
  renewal on one trunk made the renewal head excellent (0.515 → 0.991). Add/fine-tune
  heads per campaign on top of this trunk.
- **v3 (PLR periodic embeddings + calibration):** embeddings gave no AUC gain here
  (0.951 vs 0.962), but calibration is a clear, shippable win.

## 5. Research synthesis (verified, deep-research 106 agents)

Highest-leverage, evidence-backed findings applied/validated:
1. **Numerical embeddings (PLE/periodic, Gorishniy NeurIPS'22)** — top DL lever in
   general; on OUR data no AUC gain (tried, measured — honest negative result).
2. **Don't resample (SMOTE/over/under) — it ruins calibration** (van den Goorbergh
   JAMIA'22, Carriero StatMed'25). We use pos_weight + **post-hoc calibration** → ECE 0.0004.
3. **Multi-task: avoid naive hard-sharing risk of negative transfer** (Tencent PLE/CGC,
   RecSys'20). Our 2-task hard-share showed no negative transfer; if adding many
   segment-tasks, move to CGC/MMoE gating.
4. **MLP head on frozen/trainable backbone is the right transfer recipe** (Levin ICLR'23).
5. GBDT vs DL is **parity** on large tables (TabArena NeurIPS'25); NN justified here by
   the modular-head requirement, not by accuracy.

## 5b. Quasi-uplift per segment (observational — `cmd/dataset -uplift`, `ml/uplift.py`)

Built a control arm WITHOUT a live experiment: treatment = email-day (3.60M rows),
control = web-active days for the same population with NO email within ±14d (76K rows).
Three estimators per segment (2026-06-14):

| Segment | naive Δ (T−C) | S-learner | T-learner |
|---|---|---|---|
| heavy_downloader | +0.04 pp | +0.000 | +0.076 pp |
| winback_active | −0.61 pp | +0.000 | +0.063 pp |
| dormant_payer | −1.85 pp (n_C=75) | +0.000 | +0.035 pp |
| soft_cancel | — (n_C<50) | +0.000 | −0.234 pp (starved) |
| cart_abandoner | — | +0.000 | +0.055 pp |
| monthly_loyal | 0 | +0.000 | +0.004 pp |

**Conclusion: observational data cannot identify the email's causal effect here.** Each
estimator fails for a diagnosable reason: (1) **naive** is confounded — negative uplifts
are selection bias (control = self-returning, less-emailed, more-motivated users), not
real harm; (2) **S-learner collapses to exactly 0** — treatment is 98% of rows (47:1
imbalance) so the GBM never splits on it; (3) **T-learner** gives small positive uplifts
(+0.04…+0.08 pp) that are the least-biased read but rest on a control arm with only ~343
positives — high variance, still confounded by no-overlap. The T-learner's small positives
are the best available guess (email helps a little, most in heavy_downloader/winback), but
not trustworthy. **Only a randomized holdout resolves this.**

### Recommended randomized holdout (the real fix)
- Per campaign, randomly assign **10% of each segment to a no-send control**; keep it
  **persistent** across sends and accumulate over cycles (one send is underpowered:
  detecting ~1 pp uplift in winback at ~3% base needs ~4–5K per arm).
- Start with large segments (free streamers 15K, heavy bandwidth 9K) — they reach
  significance fastest.
- uplift = conv(sent) − conv(holdout); then train a T-/X-learner on the randomized data
  and target only positive-uplift users. Evaluate with Qini/AUUC.

## 6. Recommendation & next steps

- **Ship:** v2 multi-task backbone + isotonic calibrator for scoring. LogReg 0.953 is a
  fine, even-cheaper fallback if a NN runtime isn't wanted.
- **Biggest real upgrades (ranked):** (1) internal-era task/storage/stream features after
  ~mid-July 2026 — strongest Usage-PQL signals, currently empty; (2) **uplift modelling**
  with a randomized 5–10% email holdout — optimizes the *incremental* effect of sending,
  which is the correct objective and the bridge to bandit/RL targeting; (3) per-segment
  heads will pay off once segments are larger and the backbone is multi-task.
