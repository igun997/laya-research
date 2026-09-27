#!/usr/bin/env python3
"""Benchmark Laya against the rule engine on the grocery datasheet.

What this measures, stated precisely, because the framing is the whole point:

    The labels come from the rule engine in ``api/app/rules.py``. They are not
    human judgements and they are not ground truth. So this benchmark measures
    **agreement with a threshold expert**, not correctness. The answerable
    question is: can a 421M System 1 decision model reproduce a deterministic
    median-and-threshold expert's calls on a grocery datasheet, and are its
    probabilities calibrated when it does?

Every metric is reported next to a trivial baseline, because an accuracy number
without its baseline is not evidence of skill. The baselines used are the ones
that are actually available for each question: majority class for the multiple
choice, the constant base rate for the calibrated yes/no, and random guessing
where the option count defines it.

Sampling is stratified and balanced by default, so every pattern class gets
measured. That inflates the apparent base rate relative to the datasheet, so the
majority-class baseline is computed on the sample rather than assumed, and
``--sample natural`` gives the prevalence-respecting alternative.

Runs are long: CPU inference measured ~8 s per state on this host, so 150 states
is roughly 20 minutes per checkpoint. Results stream to a JSONL file as they
arrive, so a run can be interrupted, inspected, and resumed.

    python3 scripts/bench_laya.py --limit 150 --checkpoint english
    python3 scripts/bench_laya.py --checkpoint typed-decisions --limit 150
    python3 scripts/bench_laya.py --report-only        # rescore what is on disk
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_BASE = os.environ.get("LAYA_BASE", "http://localhost:8090")
OUT_DIR = Path(__file__).resolve().parent.parent / "bench"
SEVERITY_RUBRIC = ["none", "info", "warn", "critical"]
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_RUBRIC)}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Api:
    def __init__(self, base: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _call(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        if payload is None:
            request = urllib.request.Request(url)
        else:
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{path} -> HTTP {exc.code}: {exc.read()[:300]!r}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"{path} -> {type(exc).__name__}: {exc}") from exc

    def meta(self) -> dict[str, Any]:
        return self._call("/api/meta")

    def questions(self) -> dict[str, Any]:
        return self._call("/api/laya/questions")

    def laya_health(self) -> dict[str, Any]:
        return self._call("/api/laya/health")

    def fingerprint(self, day: str | None = None) -> dict[str, Any]:
        suffix = f"?day={day}" if day else ""
        return self._call(f"/api/dataset/fingerprint{suffix}")

    def scan_labels(self, day: str | None = None) -> list[dict[str, Any]]:
        return self._call("/api/patterns/scan", {"day": day, "persist": False})["signals"]

    def decide(
        self, product_id: int, day: str | None = None, model: str | None = None
    ) -> dict[str, Any]:
        parts = []
        if day:
            parts.append(f"day={day}")
        if model:
            # The request-level `model` field is the only way to pin a checkpoint
            # through `laya-serve`; it ignores LAYA_MODEL entirely.
            parts.append(f"model={model}")
        suffix = ("?" + "&".join(parts)) if parts else ""
        return self._call(f"/api/decide/{product_id}{suffix}")


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def reduce_rule_labels(signals: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Product-scope rule verdict per product, using the same reduction as /api/decide.

    Kept deliberately identical to ``api/app/decide.py:rule_verdict`` so a product
    cannot be labelled one way here and another way in the UI.
    """
    by_product: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        if signal.get("subject_type") != "product":
            continue
        by_product[int(signal["subject_id"])].append(signal)

    labels: dict[int, dict[str, Any]] = {}
    for pid, hits in by_product.items():
        severity = max(hits, key=lambda s: SEVERITY_RANK.get(str(s["severity"]), 0))["severity"]
        top = max(
            hits,
            key=lambda s: (SEVERITY_RANK.get(str(s["severity"]), 0), float(s.get("score") or 0.0)),
        )
        labels[pid] = {
            "pattern": str(top["pattern"]),
            "severity": str(severity),
            "severity_index": SEVERITY_RANK.get(str(severity), 0),
            "reorder": any(
                s["pattern"] == "STOCKOUT_RISK" and SEVERITY_RANK.get(str(s["severity"]), 0) >= 2
                for s in hits
            ),
            "patterns": sorted({str(s["pattern"]) for s in hits}),
        }
    return labels


def sample_products(
    labels: dict[int, dict[str, Any]], all_ids: list[int], limit: int, mode: str, seed: int
) -> list[int]:
    """Choose which product-days to decide.

    Stratified by default: round-robin across every label class including ``none``,
    so a class with three members is not invisible in the report.
    """
    rng = random.Random(seed)
    if mode == "natural":
        return rng.sample(all_ids, min(limit, len(all_ids)))

    buckets: dict[str, list[int]] = defaultdict(list)
    for pid in all_ids:
        bucket = labels.get(pid, {}).get("pattern", "none")
        buckets[bucket].append(pid)
    for members in buckets.values():
        rng.shuffle(members)

    chosen: list[int] = []
    order = sorted(buckets, key=lambda k: (k == "none", k))
    while len(chosen) < limit and any(buckets[k] for k in order):
        for key in order:
            if buckets[key] and len(chosen) < limit:
                chosen.append(buckets[key].pop())
    return chosen


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def accuracy(preds: list[Any], truths: list[Any]) -> float | None:
    pairs = [(p, t) for p, t in zip(preds, truths) if p is not None]
    if not pairs:
        return None
    return sum(1 for p, t in pairs if p == t) / len(pairs)


def macro_f1(preds: list[Any], truths: list[Any], classes: list[str]) -> float | None:
    pairs = [(p, t) for p, t in zip(preds, truths) if p is not None]
    if not pairs:
        return None
    scores: list[float] = []
    for cls in classes:
        tp = sum(1 for p, t in pairs if p == cls and t == cls)
        fp = sum(1 for p, t in pairs if p == cls and t != cls)
        fn = sum(1 for p, t in pairs if p != cls and t == cls)
        if tp + fp + fn == 0:
            continue  # class absent from both truth and predictions
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall))
    return statistics.fmean(scores) if scores else None


def confusion(preds: list[Any], truths: list[Any], classes: list[str]) -> dict[str, dict[str, int]]:
    table = {t: dict.fromkeys(classes, 0) for t in classes}
    for p, t in zip(preds, truths):
        if t in table and p in table[t]:
            table[t][p] += 1
    return table


def brier_binary(probs: list[float], truths: list[bool]) -> float | None:
    pairs = list(zip(probs, truths))
    if not pairs:
        return None
    return statistics.fmean((p - (1.0 if t else 0.0)) ** 2 for p, t in pairs)


def log_loss_binary(probs: list[float], truths: list[bool]) -> float | None:
    pairs = list(zip(probs, truths))
    if not pairs:
        return None
    eps = 1e-12
    total = 0.0
    for p, t in pairs:
        q = min(1 - eps, max(eps, p))
        total += -(math.log(q) if t else math.log(1 - q))
    return total / len(pairs)


def auc(probs: list[float], truths: list[bool]) -> float | None:
    """Rank-based AUC with tie handling (the Mann-Whitney U statistic)."""
    positives = [(p, t) for p, t in zip(probs, truths) if t]
    negatives = [(p, t) for p, t in zip(probs, truths) if not t]
    if not positives or not negatives:
        return None
    # Average ranks, so tied probabilities split credit evenly.
    ordered = sorted(((p, t) for p, t in zip(probs, truths)), key=lambda pair: pair[0])
    ranks: list[float] = [0.0] * len(ordered)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[position] = average_rank
        index = end + 1
    rank_sum_positives = sum(rank for rank, (_, t) in zip(ranks, ordered) if t)
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum_positives - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def reliability(probs: list[float], truths: list[bool], bins: int = 10) -> tuple[list[dict[str, Any]], float]:
    """Bucketed reliability table plus expected calibration error."""
    buckets: list[dict[str, Any]] = []
    ece = 0.0
    total = len(probs)
    if not total:
        return buckets, 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        members = [
            (p, t)
            for p, t in zip(probs, truths)
            if (low <= p < high) or (b == bins - 1 and p == 1.0)
        ]
        if not members:
            continue
        mean_prob = statistics.fmean(p for p, _ in members)
        observed = statistics.fmean(1.0 if t else 0.0 for _, t in members)
        weight = len(members) / total
        ece += weight * abs(observed - mean_prob)
        buckets.append(
            {
                "bin": f"{low:.1f}-{high:.1f}",
                "n": len(members),
                "mean_probability": round(mean_prob, 4),
                "observed_rate": round(observed, 4),
                "gap": round(observed - mean_prob, 4),
            }
        )
    return buckets, ece


def soft_accuracy(probabilities: list[dict[str, float]], truths: list[str]) -> float | None:
    """Mean probability assigned to the correct answer. Upstream calls this soft acc."""
    pairs = [(p, t) for p, t in zip(probabilities, truths) if p]
    if not pairs:
        return None
    return statistics.fmean(p.get(t, 0.0) for p, t in pairs)


def multiclass_brier(
    probabilities: list[dict[str, float]], truths: list[str], classes: list[str]
) -> float | None:
    pairs = [(p, t) for p, t in zip(probabilities, truths) if p]
    if not pairs:
        return None
    total = 0.0
    for probs, truth in pairs:
        total += sum(
            (probs.get(cls, 0.0) - (1.0 if cls == truth else 0.0)) ** 2 for cls in classes
        )
    return total / len(pairs)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    api = Api(args.base, args.timeout)
    health = api.laya_health()
    print(f"laya health : available={health.get('available')} {health.get('detail')}")
    if not health.get("available") and not args.allow_unavailable:
        print(
            "FAIL: Laya is not reachable, so there is nothing to benchmark. "
            "Wait for the model to warm (first boot downloads and builds it) or pass "
            "--allow-unavailable to run the rules-only baseline.",
            file=sys.stderr,
        )
        return 1

    meta = api.meta()
    day = args.day or meta["dataset"]["day_max"]
    questions = api.questions()
    pattern_classes = list(questions["pattern_options"])
    print(f"dataset     : {meta['dataset']['facts']:,} facts, day {day}")
    print(f"checkpoint  : {args.checkpoint}   (deployed): {health.get('detail')}")

    # Pre-flight as a real request. `laya-serve` ignores the `model` field unless it
    # names a known checkpoint, and falls back to the Router silently, so asking the
    # service what it holds is not sufficient: only a canary response proves which
    # model answered. Costs one forward pass and saves an 18-minute mislabelled run.
    if args.checkpoint != "auto":
        canary = api.decide(int(api._call("/api/products?limit=1")["items"][0]["product_id"]),
                            day, model=args.checkpoint)
        routed = ((canary.get("laya") or {}).get("routing") or {}).get("model")
        print(f"canary      : requested {args.checkpoint!r}, answered by {routed!r}")
        if routed != args.checkpoint:
            print(
                f"FAIL: asked for {args.checkpoint!r} and {routed!r} answered. `laya-serve` only "
                f"honours a `model` field naming a known checkpoint; anything else silently falls "
                f"back to the Router. Refusing to measure and mislabel a different model.",
                file=sys.stderr,
            )
            return 1

    # The states must not move while they are being measured. `sim` rewrites rows
    # every few seconds, so a run with it live would label one dataset and decide
    # on another. Fingerprints are recorded before and after and compared: this is
    # a hard invariant, not advice, because it was violated once and the resulting
    # two reports were silently incomparable.
    fp_before = api.fingerprint(day)
    print(f"fingerprint : {fp_before['fingerprint']} over {fp_before['facts']:,} facts")
    if args.require_frozen and fp_before is None:
        print("FAIL: no fingerprint available", file=sys.stderr)
        return 1

    signals = api.scan_labels(day)
    labels = reduce_rule_labels(signals)
    print(f"rule labels : {len(labels)} products carry a product-scope pattern")

    all_ids = [int(r["product_id"]) for r in api._call("/api/products?limit=500")["items"]]
    # /api/products pages at 500; walk the pages to reach every product.
    total_products = api._call("/api/products?limit=1")["total"]
    page = 0
    while len(all_ids) < total_products:
        page += 1
        batch = api._call(f"/api/products?limit=500&offset={page * 500}")["items"]
        if not batch:
            break
        all_ids.extend(int(r["product_id"]) for r in batch)
    all_ids = sorted(dict.fromkeys(all_ids))
    print(f"population  : {len(all_ids)} products")

    chosen = sample_products(labels, all_ids, args.limit, args.sample, args.seed)
    print(f"sampled     : {len(chosen)} product-days ({args.sample} sampling, seed {args.seed})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else OUT_DIR / f"laya_bench_{args.checkpoint}_{day}.jsonl"
    if args.fresh and out_path.exists():
        # Truncate rather than merely ignoring history: this file was opened in
        # append mode below, so a plain "ignore existing" left stale records behind.
        out_path.unlink()
    done: dict[int, dict[str, Any]] = {}
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                done[int(record["product_id"])] = record
        print(f"resuming    : {len(done)} records already in {out_path.name}")
        routed = Counter(r.get("routed_model") for r in done.values())
        print(f"on disk     : answered by {dict(routed)}")

    started = time.monotonic()
    written = 0
    with out_path.open("a") as handle:
        for index, pid in enumerate(chosen, 1):
            if pid in done:
                continue
            try:
                result = api.decide(
                    pid, day, model=None if args.checkpoint == "auto" else args.checkpoint
                )
            except SystemExit as exc:
                print(f"  [{index}/{len(chosen)}] product {pid} failed: {exc}", file=sys.stderr)
                continue
            record = {
                "product_id": pid,
                "day": result["day"],
                "name": result["state"]["name"],
                "category": result["state"]["category"],
                "unit_lift": result["state"]["unit_lift"],
                "price_lift": result["state"]["price_lift"],
                "inventory_cover_days": result["state"]["inventory_cover_days"],
                "label": labels.get(pid)
                or {"pattern": "none", "severity": "none", "severity_index": 0, "reorder": False},
                "laya": result["laya"],
                # Which checkpoint actually answered. Recorded per record because
                # `LAYA_MODELS` only preloads: the Router stays on `auto` unless
                # `LAYA_MODEL` forces the alias, and a run labelled
                # `typed-decisions` then silently measures `english`.
                "routed_model": ((result["laya"] or {}).get("routing") or {}).get("model"),
                "agreement": result["agreement"],
                "latency_ms": result["laya"].get("latency_ms"),
            }
            handle.write(json.dumps(record, default=str) + "\n")
            handle.flush()
            done[pid] = record
            written += 1
            if index % 10 == 0 or index == len(chosen):
                elapsed = time.monotonic() - started
                rate = elapsed / max(1, written)
                remaining = (len(chosen) - index) * rate
                print(
                    f"  [{index}/{len(chosen)}] {rate:.1f}s per state, "
                    f"~{remaining / 60:.1f} min left",
                    flush=True,
                )

    records = [done[pid] for pid in chosen if pid in done]
    if not records:
        print("FAIL: no records collected", file=sys.stderr)
        return 1

    fp_after = api.fingerprint(day)
    print(f"fingerprint : {fp_after['fingerprint']} over {fp_after['facts']:,} facts")
    if fp_after["fingerprint"] != fp_before["fingerprint"]:
        print(
            f"FAIL: the datasheet changed during the run "
            f"({fp_before['fingerprint']} -> {fp_after['fingerprint']}). The labels describe one "
            f"dataset and the decisions another, so the numbers are meaningless. Stop `sim` "
            f"(make bench does this) and re-run.",
            file=sys.stderr,
        )
        return 1

    routed = Counter(r.get("routed_model") for r in records)
    print(f"\nanswered by : {dict(routed)}")
    if args.checkpoint not in routed:
        print(
            f"FAIL: nothing was answered by {args.checkpoint!r}. The deployed model is "
            f"{sorted(k for k in routed if k)}. Set LAYA_MODEL={args.checkpoint} and recreate the "
            f"laya service: preloading a checkpoint does not make the Router use it for English "
            f"text. Refusing to write a report that would mislabel the model.",
            file=sys.stderr,
        )
        return 1
    if len(routed) > 1:
        print(
            f"FAIL: mixed checkpoints in one run: {dict(routed)}. Refusing to score a run that "
            f"changed model halfway.",
            file=sys.stderr,
        )
        return 1

    report = score(records, pattern_classes, args.checkpoint, day, args.sample, meta)
    report["raw_file"] = str(out_path)
    report["dataset_fingerprint"] = fp_before["fingerprint"]
    report["dataset_facts_on_day"] = fp_before["facts"]
    report_path = out_path.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {report_path}")

    md_path = Path(args.markdown) if args.markdown else out_path.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {md_path}")
    print(render_markdown(report))
    return 0


def score(
    records: list[dict[str, Any]],
    pattern_classes: list[str],
    checkpoint: str,
    day: str,
    sample_mode: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    truths_pattern = [r["label"]["pattern"] for r in records]
    truths_severity = [r["label"]["severity"] for r in records]
    truths_reorder = [bool(r["label"]["reorder"]) for r in records]

    pred_pattern: list[Any] = []
    pred_severity: list[Any] = []
    probs_pattern: list[dict[str, float]] = []
    probs_severity: list[dict[str, float]] = []
    probs_reorder: list[float] = []

    for r in records:
        answers = (r.get("laya") or {}).get("answers") or {}
        pattern = answers.get("pattern") or {}
        severity = answers.get("severity") or {}
        reorder = answers.get("reorder_now") or {}
        pred_pattern.append(pattern.get("answer"))
        pred_severity.append(severity.get("answer"))
        probs_pattern.append(pattern.get("probabilities") or {})
        probs_severity.append(severity.get("probabilities") or {})
        noul = reorder.get("answer")
        probs_reorder.append(float(noul) if isinstance(noul, (int, float)) else 0.5)

    n = len(records)
    majority_pattern = Counter(truths_pattern).most_common(1)[0]
    majority_severity = Counter(truths_severity).most_common(1)[0]
    base_rate_reorder = sum(truths_reorder) / n

    buckets, ece = reliability(probs_reorder, truths_reorder)
    latencies = sorted(r["latency_ms"] for r in records if r.get("latency_ms"))

    def pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]

    return {
        "checkpoint": checkpoint,
        "routed_model": sorted({r.get("routed_model") for r in records if r.get("routed_model")}),
        "day": day,
        "sample_mode": sample_mode,
        "n": n,
        "dataset_facts": meta["dataset"]["facts"],
        "label_source": "api/app/rules.py threshold rules (agreement, not ground truth)",
        "class_balance": {
            "pattern": dict(Counter(truths_pattern).most_common()),
            "severity": dict(Counter(truths_severity).most_common()),
            "reorder_true": sum(truths_reorder),
        },
        "pattern": {
            "accuracy": accuracy(pred_pattern, truths_pattern),
            "baseline_majority": majority_pattern[1] / n,
            "baseline_majority_class": majority_pattern[0],
            "baseline_random": 1.0 / len(pattern_classes),
            "macro_f1": macro_f1(pred_pattern, truths_pattern, pattern_classes),
            "soft_accuracy": soft_accuracy(probs_pattern, truths_pattern),
            "multiclass_brier": multiclass_brier(probs_pattern, truths_pattern, pattern_classes),
            "confusion": confusion(pred_pattern, truths_pattern, pattern_classes),
            "accuracy_known": accuracy(
                [p if p in pattern_classes else None for p in pred_pattern], truths_pattern
            ),
        },
        "severity": {
            "accuracy": accuracy(pred_severity, truths_severity),
            "baseline_majority": majority_severity[1] / n,
            "baseline_majority_class": majority_severity[0],
            "macro_f1": macro_f1(pred_severity, truths_severity, SEVERITY_RUBRIC),
            "soft_accuracy": soft_accuracy(probs_severity, truths_severity),
            "mae_levels": _mae(pred_severity, truths_severity),
            "confusion": confusion(pred_severity, truths_severity, SEVERITY_RUBRIC),
        },
        "reorder": {
            "brier": brier_binary(probs_reorder, truths_reorder),
            "baseline_brier_all_zero": brier_binary([0.0] * n, truths_reorder),
            "baseline_brier_base_rate": brier_binary([base_rate_reorder] * n, truths_reorder),
            "log_loss": log_loss_binary(probs_reorder, truths_reorder),
            "auc": auc(probs_reorder, truths_reorder),
            "base_rate_true": base_rate_reorder,
            "mean_probability": statistics.fmean(probs_reorder),
            "ece": ece,
            "reliability": buckets,
            "accuracy_at_half": accuracy(
                [p >= 0.5 for p in probs_reorder], truths_reorder
            ),
        },
        "latency_ms": {
            "n": len(latencies),
            "min": latencies[0] if latencies else None,
            "p50": pct(latencies, 0.50),
            "p95": pct(latencies, 0.95),
            "max": latencies[-1] if latencies else None,
        },
        "agreement_rates": {
            "pattern": _rate([r["agreement"].get("pattern") for r in records]),
            "severity": _rate([r["agreement"].get("severity") for r in records]),
            "reorder": _rate([r["agreement"].get("reorder") for r in records]),
        },
    }


def _mae(preds: list[Any], truths: list[str]) -> float | None:
    pairs = [
        (SEVERITY_RANK[p], SEVERITY_RANK[t])
        for p, t in zip(preds, truths)
        if isinstance(p, str) and p in SEVERITY_RANK and t in SEVERITY_RANK
    ]
    if not pairs:
        return None
    return statistics.fmean(abs(p - t) for p, t in pairs)


def _rate(flags: list[Any]) -> float | None:
    known = [f for f in flags if f is not None]
    if not known:
        return None
    return sum(1 for f in known if f) / len(known)


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    pattern = report["pattern"]
    severity = report["severity"]
    reorder = report["reorder"]
    latency = report["latency_ms"]
    n = report["n"]

    lines: list[str] = []
    lines.append(f"# Laya vs rule engine — checkpoint `{report['checkpoint']}`, day {report['day']}")
    lines.append("")
    lines.append(
        f"{n} product-days, {report['sample_mode']} sampling, from a datasheet of "
        f"{report['dataset_facts']:,} facts."
    )
    lines.append("")
    lines.append(f"Checkpoint that actually answered: `{', '.join(report.get('routed_model') or ['unknown'])}`")
    lines.append("")
    lines.append(
        "> **The labels come from the rule engine.** They are deterministic threshold"
        " rules, not human judgements. Every number below measures *agreement with a"
        " threshold expert*, never correctness. Read the baselines."
    )
    lines.append("")
    lines.append("## Labels in this sample")
    lines.append("")
    lines.append("```")
    lines.append(f"pattern  : {report['class_balance']['pattern']}")
    lines.append(f"severity : {report['class_balance']['severity']}")
    lines.append(f"reorder  : {report['class_balance']['reorder_true']} of {n} true")
    lines.append("```")
    lines.append("")

    lines.append("## `choice` — which pattern applies")
    lines.append("")
    lines.append("| metric | Laya | baseline |")
    lines.append("|---|---|---|")
    lines.append(
        f"| accuracy | {fmt(pattern['accuracy'])} | majority class "
        f"`{pattern['baseline_majority_class']}` = {fmt(pattern['baseline_majority'])} |"
    )
    lines.append(f"| random guessing | | {fmt(pattern['baseline_random'])} |")
    lines.append(f"| macro F1 | {fmt(pattern['macro_f1'])} | |")
    lines.append(f"| soft accuracy (prob on the right answer) | {fmt(pattern['soft_accuracy'])} | |")
    lines.append(f"| multiclass Brier (lower better) | {fmt(pattern['multiclass_brier'])} | |")
    lines.append("")

    lines.append("## `score` — how severe")
    lines.append("")
    lines.append("| metric | Laya | baseline |")
    lines.append("|---|---|---|")
    lines.append(
        f"| accuracy | {fmt(severity['accuracy'])} | majority class "
        f"`{severity['baseline_majority_class']}` = {fmt(severity['baseline_majority'])} |"
    )
    lines.append(f"| macro F1 | {fmt(severity['macro_f1'])} | |")
    lines.append(f"| mean absolute error, rubric levels | {fmt(severity['mae_levels'], 3)} | |")
    lines.append(f"| soft accuracy | {fmt(severity['soft_accuracy'])} | |")
    lines.append("")

    lines.append("## `noul` — reorder now?")
    lines.append("")
    lines.append("| metric | Laya | baseline |")
    lines.append("|---|---|---|")
    lines.append(f"| Brier (lower better) | {fmt(reorder['brier'])} | "
                 f"all-zero {fmt(reorder['baseline_brier_all_zero'])}, "
                 f"base rate {fmt(reorder['baseline_brier_base_rate'])} |")
    lines.append(f"| log loss | {fmt(reorder['log_loss'])} | |")
    lines.append(f"| AUC | {fmt(reorder['auc'], 3)} | 0.5 = no discrimination |")
    lines.append(
        f"| ECE (lower better) | {fmt(reorder['ece'])} | mean probability "
        f"{fmt(reorder['mean_probability'], 3)} vs base rate {fmt(reorder['base_rate_true'], 3)} |"
    )
    lines.append("")

    if reorder.get("reliability"):
        lines.append("Reliability, by predicted probability bucket:")
        lines.append("")
        lines.append("| bucket | n | mean probability | observed rate | gap |")
        lines.append("|---|---|---|---|---|")
        for bucket in reorder["reliability"]:
            lines.append(
                f"| {bucket['bin']} | {bucket['n']} | {bucket['mean_probability']:.3f} | "
                f"{bucket['observed_rate']:.3f} | {bucket['gap']:+.3f} |"
            )
        lines.append("")

    lines.append("## Agreement with the rules")
    lines.append("")
    for key, value in report["agreement_rates"].items():
        lines.append(f"- {key}: {fmt(value)}")
    lines.append("")

    lines.append("## Latency (one call, three questions)")
    lines.append("")
    lines.append(
        f"n={latency['n']} min={fmt(latency['min'], 0)}ms p50={fmt(latency['p50'], 0)}ms "
        f"p95={fmt(latency['p95'], 0)}ms max={fmt(latency['max'], 0)}ms"
    )
    lines.append("")
    lines.append("## Confusion, `choice` (rows = rules, columns = Laya)")
    lines.append("")
    classes = list(report["pattern"]["confusion"])
    header = "| truth \\ pred | " + " | ".join(c[:14] for c in classes) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(classes) + 1))
    for truth in classes:
        row = report["pattern"]["confusion"][truth]
        lines.append(f"| {truth[:20]} | " + " | ".join(str(row[c]) for c in classes) + " |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE, help="edge URL (default: %(default)s)")
    ap.add_argument(
        "--checkpoint",
        default=os.environ.get("LAYA_CHECKPOINT", "english"),
        help="checkpoint to pin via the request model field, or `auto` to let the Router choose",
    )
    ap.add_argument("--limit", type=int, default=150, help="states to decide (default: %(default)s)")
    ap.add_argument("--sample", default="stratified", choices=["stratified", "natural"])
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--day", default=None)
    ap.add_argument("--out", default=None, help="JSONL output path")
    ap.add_argument("--markdown", default=None, help="markdown report path")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--fresh", action="store_true", help="ignore existing JSONL and start over")
    ap.add_argument("--allow-unavailable", action="store_true")
    ap.add_argument(
        "--require-frozen",
        action="store_true",
        help="fail if the dataset fingerprint is missing (default: warn)",
    )
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
