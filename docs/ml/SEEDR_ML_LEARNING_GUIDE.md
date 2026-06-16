# Seedr — ML Learning Methods Guide

A synthesized, **citable** map of machine-learning approaches relevant to Seedr's
marketing / conversion / retention problems. Built from a verified deep-research run
(106 agents, 24 sources, 117 claims extracted, 25 adversarially verified → 23 confirmed
/ 2 refuted, 2026-06-14) **plus our own measured results** on the email-conversion task.

Companions: `SEEDR_ML_RESEARCH.md` (final model report), `ml/README.md` (how to reproduce),
`SEEDR_ML_DATASET.md` (data dictionary), `SEEDR_MARKETING_SEGMENTS.md` (segments).

Legend: 🟢 verified by research · 📊 measured on our data · ⚠️ caveat/risk · ❌ refuted.

---

## 0. TL;DR — what matters for Seedr

1. **Our problem is supervised tabular classification on extreme imbalance** (~0.06% conversion).
   The model's job is *ranking* (whom to email), measured by **lift@k**, not accuracy.
2. **GBDT and modern tabular DL are at parity** on large tables 🟢 — neither categorically
   wins. We use a neural net only because we want the **multi-task backbone + swappable heads**,
   not for accuracy (LogReg already hits AUC 0.95 📊).
3. **Biggest free win: don't resample, do calibrate.** SMOTE/oversampling ruin probabilities 🟢;
   we used `pos_weight` + isotonic calibration → ECE 0.174 → 0.0004 📊.
4. **Multi-task co-training works**, naive hard-sharing risks negative transfer 🟢; for us 2 tasks
   (conversion + renewal) showed no negative transfer and fixed the renewal head (0.51 → 0.99 📊).
5. **The unanswered business question — "does the email *cause* conversion?" — needs uplift**,
   which needs a **randomized holdout**. Observational uplift failed for diagnosable reasons 📊.

---

## 1. Types of learning, mapped to Seedr

### 1.1 Supervised learning — propensity / classification  📊 (what we built)
Predict a labeled outcome from features. Our case: `P(pays within 14d | features at send)`.
- **Algorithms we use:** Logistic Regression (AUC 0.953), Gradient Boosting / HistGBM (0.756
  un-tuned → needs care), neural MLP backbone (0.962).
- **When to reach for it:** any "score each user/event for outcome X" task — conversion,
  churn, open/click propensity, fraud, quota-breach prediction.
- **Metric for us:** weighted ROC-AUC + **lift@k** (top-1% → ×65–67) + calibration. Accuracy is
  useless at 0.06% base rate.

### 1.2 Tabular deep learning vs gradient-boosted trees  🟢
- **Verdict (TabArena, NeurIPS 2025, 16 models / 51 datasets / 25M+ runs):** *"gradient-boosted
  trees are still strong contenders … deep learning methods have caught up under larger time
  budgets with ensembling."* CatBoost ranks #1 under conventional tuning; GBDT-vs-DL is a "false
  dichotomy" — **cross-model ensembles win.** → On our 3.5M-row table, DL ≈ GBDT; pick by
  engineering needs, not a mythical accuracy edge.
- **Highest-leverage DL lever = numerical feature embeddings** (Gorishniy et al., NeurIPS 2022):
  piecewise-linear (PLE) & periodic activations let *simple MLPs reach Transformer/GBDT parity*;
  *"the embedding choice matters more than the backbone."* 📊 We tried periodic embeddings (MLP-PLR):
  no AUC gain on our data (0.951 vs 0.962) — embeddings help *on average*, not guaranteed per-dataset.
- **Best DL baseline = TabM** (ICLR 2025): a parameter-efficient MLP ensemble (k=32 implicit MLPs
  sharing params), *"competes with GBDT and outperforms prior tabular DL, more efficient than
  attention/retrieval."* → if we want a stronger DL model, TabM > FT-Transformer/SAINT/TabR.
- **Attention & retrieval nets (FT-Transformer, SAINT, TabR) are NOT reliable MLP replacements** 🟢
  — often no better than a plain MLP. Don't reach for transformers on tabular by default.
- **Why NNs historically lagged** (Grinsztajn et al., NeurIPS 2022): NNs must (1) be robust to
  uninformative features, (2) preserve data orientation, (3) learn irregular functions — three
  inductive-bias gaps vs trees. Now narrowed; ❌ "trees remain SOTA over DL on medium data" was
  **refuted** in our verification.

### 1.3 Transfer learning & fine-tuning  🟢 📊 (the "swappable head" design)
Train a representation once, adapt it cheaply to new tasks/segments.
- **Recipe (Levin et al., ICLR 2023):** *"an MLP head with a trainable or frozen feature extractor
  is effective for all deep tabular models."* → our backbone + per-segment head is the right shape.
- **When DL transfer decisively beats GBDT:** ONLY with related upstream data **and scarce
  downstream data** (4–200 samples). ⚠️ Does NOT generalize to our large table — so per-segment
  fine-tuning is justified by *modularity*, not accuracy.
- 📊 Our finding: frozen-backbone + per-segment fine-tuned head did **not** beat the global head
  (segments too small); the win came from **multi-task co-training** instead (§1.4).

### 1.4 Multi-task learning (shared backbone + per-task heads)  🟢 📊
One trunk, several heads — our "one main model, many heads" design.
- ⚠️ **Naive hard parameter sharing causes negative transfer & a "seesaw"** (Tencent PLE, RecSys
  2020 *Best Paper*): in a live A/B test hard-sharing gave **negative** online gains
  (−1.65% views / −1.79% watch time), while explicit shared/task-specific separation **CGC (+3.9%)
  and PLE (+4.2%)** won biggest. → if we add many segment-tasks, move from plain shared-bottom to
  **CGC/MMoE/PLE gating**.
- 📊 Our 2-task model (conversion + renewal) showed **no** negative transfer and made the renewal
  head excellent (AUC 0.515 → 0.991). Safe at 2 tasks; revisit gating beyond ~4.
- **MultiTab-Net** (AAAI 2026, ⚠️ self-reported only): multitask masked-attention to curb task
  competition — an option if hard-sharing degrades.

### 1.5 Imbalanced / long-tail learning  🟢 📊 (our 0.06% base rate)
- ❌ **Do NOT resample.** Random under/over-sampling and SMOTE *"yielded poorly calibrated models …
  did not improve AUROC"* (van den Goorbergh, JAMIA 2022; extended to ML by Carriero, Stat. in Med.
  2025: *"dramatically deteriorated calibration"*). Prefer threshold shifting.
- **Use prior correction / logit adjustment** (Menon et al., ICLR 2021): adjust logits by class
  priors, post-hoc or in-loss — principled alternative to resampling.
- **Or focal loss** (Lin et al., ICCV 2017): `FL = −(1−p)^γ·log(p)`, down-weights easy examples;
  best config α=0.25, γ=2 (from vision — a starting point, not tuned for CTR).
- ⚠️ **Any reweighting distorts probabilities → recalibrate** (Platt/isotonic/temperature).
  📊 We did exactly this: `pos_weight` BCE + isotonic → ECE 0.174 → **0.0004**, Brier 0.077 → 0.0006,
  AUC unchanged (rank-invariant). **This is our single most shippable upgrade.**
- **Metrics for <0.1% base rates:** PR-AUC, **lift@k**, calibration (ECE/Brier) — never accuracy.

### 1.6 Uplift / causal / incremental learning  📊 (the real marketing question)
"Does the email *cause* the conversion?" ≠ "who converts after an email?".
- **Estimators:** S-learner (one model, treatment as feature), T-learner (separate model per arm),
  X-/R-learner (uber `causalml`); uplift trees/forests; evaluate with **Qini / AUUC**.
- ⚠️ **Needs randomization.** 📊 Our observational quasi-uplift failed three ways: naive Δ is
  selection-biased (showed *negative* uplift for winback), S-learner collapsed to exactly 0
  (treatment 98% of rows → no split), T-learner gave small positives (+0.04–0.08 pp) but on a
  data-starved control arm. **Conclusion: a randomized holdout is required.**
- **The fix:** hold out 10% of each segment (persistent, accumulate over sends); start with large
  segments; then train an uplift model and target only positive-uplift users.
- (Research gap: the run found *no* surviving verified claim on bootstrapping uplift from purely
  observational data — consistent with our empirical failure.)

### 1.7 Foundation models for tabular data  🟢
- **TabPFN v2** (Nature, Jan 2025): ~100% win rate vs default XGBoost **on datasets ≤10K samples**;
  advantage shrinks as data grows. ⚠️ Transformer scales quadratically → effectively capped ~10K
  rows. → **Not for our 3.5M-row table**, but useful for *small* per-segment or cold-start problems
  (e.g., a brand-new segment with few labeled examples).

### 1.8 Other learning families (landscape, lighter evidence)
- **Unsupervised / clustering** — for *discovering* segments (k-means / HDBSCAN / embeddings on
  behavior) rather than the hand-defined 8. Useful to validate or refine our segment taxonomy.
- **Reinforcement learning / contextual bandits** — the *next* step after uplift: an agent that
  chooses send / no-send / which template per user, reward = conversion. Needs the randomized
  logging from §1.6 first; don't skip to RL without it.
- **Semi-supervised / self-supervised** — pre-train on the huge unlabeled event stream
  (`request_events`, 25M/day) to build user embeddings, then fine-tune on scarce conversion labels.
  Plausible future lever given our label scarcity.

---

## 2. Verified findings table (research, with citations)

| # | Finding | Conf. | Source |
|---|---|---|---|
| 1 | GBDT ≈ DL parity on practical tables; ensembles win; foundation models win small data | high | TabArena, arXiv:2506.16791 |
| 2 | DL beats GBDT only in transfer + scarce-downstream regime (4–200 samples) | high | Levin, arXiv:2206.15306 |
| 3 | Numerical embeddings (PLE/periodic) are the top DL lever; MLP→Transformer parity | high | Gorishniy, arXiv:2203.05556 |
| 4 | TabM (param-efficient MLP ensemble) = best practical DL baseline | high | TabM, arXiv:2410.24210 |
| 5 | Attention/retrieval nets not reliable MLP replacements | high | TabM; TabArena |
| 6 | MLP head on frozen/trainable backbone = effective transfer recipe | high | Levin, arXiv:2206.15306 |
| 7 | Hard parameter sharing → negative transfer/seesaw; CGC/PLE fix it (live A/B) | high | PLE, RecSys 2020 |
| 8 | Don't resample (SMOTE/over/under) — ruins calibration, no AUROC gain | high | JAMIA 2022; Stat.Med 2025 |
| 9 | Use logit adjustment / prior correction for imbalance | high | Menon, ICLR 2021 |
| 10 | Focal loss α=0.25 γ=2 (vision-derived starting point) | high | Lin, ICCV 2017 (arXiv:1708.02002) |
| 11 | After reweighting, recalibrate (probs are distorted) | high | JAMIA 2022 |
| 12 | TabPFN wins ≤10K samples, capped ~10K (quadratic) | high | TabPFN, Nature 2025 |

❌ **Refuted (transparency):** "trees remain SOTA over DL on medium data" (0-3); "FT-Transformer
end-to-end beats all GBDT at every level" (0-3).

---

## 3. Prioritized for Seedr — gain × effort

| Priority | Action | Type | Effort | Expected gain |
|---|---|---|---|---|
| **P0** | Calibrate scores (isotonic/temperature) before any €-decision | §1.5 | done 📊 | high (usable probabilities) |
| **P0** | Ship one global model (LogReg or multi-task NN) for top-k targeting | §1.1 | done 📊 | high (lift ×67) |
| **P1** | **Randomized 10% holdout** per campaign → real uplift | §1.6 | low (process) | **highest** (answers the actual question) |
| **P1** | Add internal-era features (task/storage/stream) after ~mid-July 2026 | §1.1 | low | high (best Usage-PQL signals) |
| **P2** | If pushing accuracy: try TabM and/or focal+logit-adjustment | §1.2/1.5 | medium | low–medium |
| **P2** | CGC/MMoE gating if expanding to many per-segment heads | §1.4 | medium | medium (avoids negative transfer) |
| **P3** | Clustering to discover/refine segments; TabPFN for new small segments | §1.7/1.8 | medium | exploratory |
| **P3** | Self-supervised user embeddings from `request_events` | §1.8 | high | speculative |

---

## 4. Caveats from the research (don't over-read)

- DL-beats-GBDT results are tightly scoped (small data / transfer); on our 3.5M rows it's parity.
- Imbalance-harm evidence is strongest for logistic regression (extended to ML in 2025); treat
  "avoid SMOTE + recalibrate" as well-supported by analogy, not a direct deep-net replication.
- Focal α/γ come from object detection, not CTR — tune for us.
- MultiTab-Net superiority is self-reported (single source) — medium confidence.
- Field moves fast (sources span Nov 2025–2026); re-verify before big bets.

## 5. Sources (primary)
TabArena arXiv:2506.16791 · TabM arXiv:2410.24210 · Gorishniy embeddings arXiv:2203.05556 ·
Grinsztajn arXiv:2207.08815 · Levin transfer arXiv:2206.15306 · PLE RecSys 2020
(10.1145/3383313.3412236) · MultiTab-Net arXiv:2511.09970 · van den Goorbergh JAMIA 2022 ·
Menon logit-adjustment (ICLR 2021) · Lin focal loss arXiv:1708.02002 · TabPFN v2 Nature 2025 ·
uber/causalml · uplift-modeling.com.

*Generated from a 106-agent verified deep-research run + our own experiments, 2026-06-14.
Point-in-time; figures drift.*
