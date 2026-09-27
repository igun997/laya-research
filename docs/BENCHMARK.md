Laya vs the rule engine reports, 150 product-days each, stratified sampling (seed 13), day 2026-09-26.

All reports were measured on the identical frozen dataset (`dataset_fingerprint` `4a85672eaca868e58785e85a7aa69368`). `sim` was stopped for the runs; the harness re-reads the fingerprint after each run and aborts if it moved, so the columns cannot have been scored on different data.

| | `english` | `typed-decisions` | baseline |
|---|---|---|---|

**`choice` — which of 8 patterns applies**

| accuracy | 0.113 **below** | 0.027 **below** | majority class |
| macro F1 | 0.069 | 0.022 | — |
| soft accuracy | 0.136 | 0.122 | — |
| multiclass Brier | 0.872 | 0.897 | — (lower better) |
| random guessing | 0.125 | 0.125 | 0.125 |

**`score` — how severe, 4 ordinal levels**

| accuracy | 0.173 **below** | 0.040 **below** | majority class |
| macro F1 | 0.088 | 0.047 | — |
| mean absolute error (levels) | 1.840 | 1.407 | — (lower better) |
| soft accuracy | 0.000 | 0.000 | — |

**`noul` — reorder now? (calibrated probability)**

| Brier | 0.0484 **below** | 0.1390 **below** | all-zero |
| Brier vs base rate | 0.0484 **below** | 0.1390 **below** | base rate |
| log loss | 0.2389 | 0.4657 | — (lower better) |
| AUC | 0.416 **below** | 0.669 **beats** | 0.5 = no discrimination |
| ECE | 0.1801 | 0.3544 | — (lower better) |
| mean probability | 0.193 | 0.368 | base rate 0.013 |

**Latency, one call covering all three questions, CPU**

| | `english` | `typed-decisions` |
|---|---|---|
| min ms | 4344 | 4463 |
| p50 ms | 5515 | 5676 |
| p95 ms | 7415 | 7641 |
| max ms | 8514 | 8129 |

**Agreement with the rule engine** (the labels' own source)

| | `english` | `typed-decisions` |
|---|---|---|
| pattern | 0.113 | 0.027 |
| severity | 0.173 | 0.040 |
| reorder | 0.987 | 0.987 |

**Sample composition**

- `english`: {'pattern': {'DEMAND_SURGE': 34, 'MARGIN_SQUEEZE': 34, 'PRICE_SPIKE': 34, 'none': 34, 'DEMAND_COLLAPSE': 9, 'PRICE_CUT_UNANSWERED': 3, 'STOCKOUT_RISK': 2}, 'severity': {'critical': 64, 'warn': 52, 'none': 34}, 'reorder_true': 2}
- `typed-decisions`: {'pattern': {'DEMAND_SURGE': 34, 'MARGIN_SQUEEZE': 34, 'PRICE_SPIKE': 34, 'none': 34, 'DEMAND_COLLAPSE': 9, 'PRICE_CUT_UNANSWERED': 3, 'STOCKOUT_RISK': 2}, 'severity': {'critical': 64, 'warn': 52, 'none': 34}, 'reorder_true': 2}

The `reorder` row is the one that most invites a wrong reading: positives are rare (2 of 150), so a model that always answers *no* scores high on agreement while carrying no information. That is why the AUC and the Brier-versus-baseline rows are printed next to it.

