# Laya vs rule engine — checkpoint `typed-decisions`, day 2026-09-26

150 product-days, stratified sampling, from a datasheet of 7,056,000 facts.

Checkpoint that actually answered: `typed-decisions`

> **The labels come from the rule engine.** They are deterministic threshold rules, not human judgements. Every number below measures *agreement with a threshold expert*, never correctness. Read the baselines.

## Labels in this sample

```
pattern  : {'DEMAND_SURGE': 34, 'MARGIN_SQUEEZE': 34, 'PRICE_SPIKE': 34, 'none': 34, 'DEMAND_COLLAPSE': 9, 'PRICE_CUT_UNANSWERED': 3, 'STOCKOUT_RISK': 2}
severity : {'critical': 64, 'warn': 52, 'none': 34}
reorder  : 2 of 150 true
```

## `choice` — which pattern applies

| metric | Laya | baseline |
|---|---|---|
| accuracy | 0.0267 | majority class `DEMAND_SURGE` = 0.2267 |
| random guessing | | 0.1250 |
| macro F1 | 0.0222 | |
| soft accuracy (prob on the right answer) | 0.1225 | |
| multiclass Brier (lower better) | 0.8967 | |

## `score` — how severe

| metric | Laya | baseline |
|---|---|---|
| accuracy | 0.0400 | majority class `critical` = 0.4267 |
| macro F1 | 0.0469 | |
| mean absolute error, rubric levels | 1.407 | |
| soft accuracy | 0.0000 | |

## `noul` — reorder now?

| metric | Laya | baseline |
|---|---|---|
| Brier (lower better) | 0.1390 | all-zero 0.0133, base rate 0.0132 |
| log loss | 0.4657 | |
| AUC | 0.669 | 0.5 = no discrimination |
| ECE (lower better) | 0.3544 | mean probability 0.368 vs base rate 0.013 |

Reliability, by predicted probability bucket:

| bucket | n | mean probability | observed rate | gap |
|---|---|---|---|---|
| 0.2-0.3 | 2 | 0.299 | 0.000 | -0.299 |
| 0.3-0.4 | 138 | 0.365 | 0.015 | -0.351 |
| 0.4-0.5 | 10 | 0.413 | 0.000 | -0.413 |

## Agreement with the rules

- pattern: 0.0267
- severity: 0.0400
- reorder: 0.9867

## Latency (one call, three questions)

n=150 min=4463ms p50=5676ms p95=7641ms max=8129ms

## Confusion, `choice` (rows = rules, columns = Laya)

| truth \ pred | none | DEMAND_SURGE | DEMAND_COLLAPS | PRICE_SPIKE | PRICE_CUT_UNAN | MARGIN_SQUEEZE | STOCKOUT_RISK | PROMO_INEFFECT |
|---|---|---|---|---|---|---|---|---|
| none | 0 | 0 | 0 | 1 | 0 | 0 | 3 | 30 |
| DEMAND_SURGE | 0 | 0 | 0 | 3 | 0 | 0 | 1 | 30 |
| DEMAND_COLLAPSE | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 8 |
| PRICE_SPIKE | 0 | 0 | 0 | 4 | 0 | 0 | 2 | 28 |
| PRICE_CUT_UNANSWERED | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| MARGIN_SQUEEZE | 0 | 0 | 0 | 1 | 0 | 0 | 2 | 31 |
| STOCKOUT_RISK | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 1 |
| PROMO_INEFFECTIVE | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
