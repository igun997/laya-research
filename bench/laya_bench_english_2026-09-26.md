# Laya vs rule engine — checkpoint `english`, day 2026-09-26

150 product-days, stratified sampling, from a datasheet of 7,056,000 facts.

Checkpoint that actually answered: `english`

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
| accuracy | 0.1133 | majority class `DEMAND_SURGE` = 0.2267 |
| random guessing | | 0.1250 |
| macro F1 | 0.0691 | |
| soft accuracy (prob on the right answer) | 0.1356 | |
| multiclass Brier (lower better) | 0.8723 | |

## `score` — how severe

| metric | Laya | baseline |
|---|---|---|
| accuracy | 0.1733 | majority class `critical` = 0.4267 |
| macro F1 | 0.0878 | |
| mean absolute error, rubric levels | 1.840 | |
| soft accuracy | 0.0000 | |

## `noul` — reorder now?

| metric | Laya | baseline |
|---|---|---|
| Brier (lower better) | 0.0484 | all-zero 0.0133, base rate 0.0132 |
| log loss | 0.2389 | |
| AUC | 0.416 | 0.5 = no discrimination |
| ECE (lower better) | 0.1801 | mean probability 0.193 vs base rate 0.013 |

Reliability, by predicted probability bucket:

| bucket | n | mean probability | observed rate | gap |
|---|---|---|---|---|
| 0.0-0.1 | 2 | 0.097 | 0.000 | -0.097 |
| 0.1-0.2 | 88 | 0.166 | 0.011 | -0.154 |
| 0.2-0.3 | 57 | 0.231 | 0.018 | -0.214 |
| 0.3-0.4 | 2 | 0.338 | 0.000 | -0.338 |
| 0.4-0.5 | 1 | 0.402 | 0.000 | -0.402 |

## Agreement with the rules

- pattern: 0.1133
- severity: 0.1733
- reorder: 0.9867

## Latency (one call, three questions)

n=150 min=4344ms p50=5515ms p95=7415ms max=8514ms

## Confusion, `choice` (rows = rules, columns = Laya)

| truth \ pred | none | DEMAND_SURGE | DEMAND_COLLAPS | PRICE_SPIKE | PRICE_CUT_UNAN | MARGIN_SQUEEZE | STOCKOUT_RISK | PROMO_INEFFECT |
|---|---|---|---|---|---|---|---|---|
| none | 0 | 8 | 0 | 0 | 1 | 1 | 24 | 0 |
| DEMAND_SURGE | 0 | 13 | 0 | 0 | 0 | 3 | 18 | 0 |
| DEMAND_COLLAPSE | 1 | 2 | 0 | 0 | 0 | 0 | 6 | 0 |
| PRICE_SPIKE | 0 | 8 | 0 | 0 | 0 | 3 | 23 | 0 |
| PRICE_CUT_UNANSWERED | 0 | 1 | 0 | 0 | 0 | 0 | 2 | 0 |
| MARGIN_SQUEEZE | 0 | 13 | 0 | 0 | 1 | 3 | 17 | 0 |
| STOCKOUT_RISK | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 |
| PROMO_INEFFECTIVE | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
