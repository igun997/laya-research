#!/usr/bin/env python3
"""Compare two or more `bench_laya.py` reports side by side, with their baselines.

Generated rather than typed, because every number in the README's Laya section is
copied from here and hand-transcription is how a benchmark table ends up wrong.

    python3 scripts/bench_compare.py bench/*.report.json
    python3 scripts/bench_compare.py bench/*.report.json --inject README.md
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

MARKER = "<!-- BENCH_RESULTS -->"


def load(paths: list[str]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for pattern in paths:
        for match in sorted(glob.glob(pattern)) or [pattern]:
            path = Path(match)
            if not path.exists():
                print(f"skipping missing {path}", file=sys.stderr)
                continue
            report = json.loads(path.read_text())
            report["_path"] = str(path)
            reports.append(report)
    return reports


def f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def verdict(value: float | None, baseline: float | None, higher_is_better: bool) -> str:
    """Say plainly whether the model beat its trivial baseline."""
    if value is None or baseline is None:
        return ""
    if abs(value - baseline) < 1e-9:
        return "= baseline"
    better = value > baseline if higher_is_better else value < baseline
    return "**beats**" if better else "**below**"


def render(reports: list[dict[str, Any]], title: str) -> str:
    if not reports:
        return "_no reports_"
    names = [r["checkpoint"] for r in reports]
    out: list[str] = []

    out.append(
        f"{title} reports, {reports[0]['n']} product-days each, "
        f"{reports[0]['sample_mode']} sampling (seed 13), day {reports[0]['day']}."
    )
    out.append("")
    out.append(
        f"All reports were measured on the identical frozen dataset "
        f"(`dataset_fingerprint` `{reports[0].get('dataset_fingerprint') or 'unavailable'}`). "
        f"`sim` was stopped for the runs; the harness re-reads the fingerprint after each run and "
        f"aborts if it moved, so the columns cannot have been scored on different data."
    )
    out.append("")
    out.append("| | " + " | ".join(f"`{n}`" for n in names) + " | baseline |")
    out.append("|---|" + "---|" * (len(names) + 1))

    def row(
        label: str,
        getter,
        baseline_text: str,
        higher_is_better: bool,
        baseline_value=None,
        digits: int = 3,
    ) -> None:
        cells = []
        for report in reports:
            value = getter(report)
            mark = ""
            if baseline_value is not None:
                mark = " " + verdict(value, baseline_value(report) if callable(baseline_value) else baseline_value, higher_is_better)
            cells.append(f(value, digits) + mark)
        out.append(f"| {label} | " + " | ".join(cells) + f" | {baseline_text} |")

    out.append("")
    out.append("**`choice` — which of 8 patterns applies**")
    out.append("")
    row("accuracy", lambda r: r["pattern"]["accuracy"], "majority class", True,
        lambda r: r["pattern"]["baseline_majority"])
    row("macro F1", lambda r: r["pattern"]["macro_f1"], "—", True)
    row("soft accuracy", lambda r: r["pattern"]["soft_accuracy"], "—", True)
    row("multiclass Brier", lambda r: r["pattern"]["multiclass_brier"], "— (lower better)", False)
    out.append(f"| random guessing | " + " | ".join(f(1 / 8) for _ in names) + " | 0.125 |")
    out.append("")

    out.append("**`score` — how severe, 4 ordinal levels**")
    out.append("")
    row("accuracy", lambda r: r["severity"]["accuracy"], "majority class", True,
        lambda r: r["severity"]["baseline_majority"])
    row("macro F1", lambda r: r["severity"]["macro_f1"], "—", True)
    row("mean absolute error (levels)", lambda r: r["severity"]["mae_levels"], "— (lower better)", False)
    row("soft accuracy", lambda r: r["severity"]["soft_accuracy"], "—", True)
    out.append("")

    out.append("**`noul` — reorder now? (calibrated probability)**")
    out.append("")
    row("Brier", lambda r: r["reorder"]["brier"], "all-zero", False,
        lambda r: r["reorder"]["baseline_brier_all_zero"], digits=4)
    row("Brier vs base rate", lambda r: r["reorder"]["brier"], "base rate", False,
        lambda r: r["reorder"]["baseline_brier_base_rate"], digits=4)
    row("log loss", lambda r: r["reorder"]["log_loss"], "— (lower better)", False, digits=4)
    row("AUC", lambda r: r["reorder"]["auc"], "0.5 = no discrimination", True, 0.5, digits=3)
    row("ECE", lambda r: r["reorder"]["ece"], "— (lower better)", False, digits=4)
    out.append(
        "| mean probability | "
        + " | ".join(f(r["reorder"]["mean_probability"]) for r in reports)
        + f" | base rate {f(reports[0]['reorder']['base_rate_true'])} |"
    )
    out.append("")

    out.append("**Latency, one call covering all three questions, CPU**")
    out.append("")
    out.append(
        "| | " + " | ".join(f"`{n}`" for n in names) + " |"
    )
    out.append("|---|" + "---|" * len(names))
    for key in ("min", "p50", "p95", "max"):
        out.append(
            f"| {key} ms | "
            + " | ".join(f(r["latency_ms"][key], 0) for r in reports)
            + " |"
        )
    out.append("")

    out.append("**Agreement with the rule engine** (the labels' own source)")
    out.append("")
    out.append("| | " + " | ".join(f"`{n}`" for n in names) + " |")
    out.append("|---|" + "---|" * len(names))
    for key in ("pattern", "severity", "reorder"):
        out.append(
            f"| {key} | "
            + " | ".join(f(r["agreement_rates"][key]) for r in reports)
            + " |"
        )
    out.append("")

    out.append("**Sample composition**")
    out.append("")
    for report in reports:
        out.append(f"- `{report['checkpoint']}`: {report['class_balance']}")
    out.append("")
    out.append(
        "The `reorder` row is the one that most invites a wrong reading: positives are rare "
        f"({reports[0]['class_balance']['reorder_true']} of {reports[0]['n']}), so a model that always "
        "answers *no* scores high on agreement while carrying no information. That is why the AUC and "
        "the Brier-versus-baseline rows are printed next to it."
    )
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+", help="report.json paths or globs")
    ap.add_argument("--title", default="Laya vs the rule engine")
    ap.add_argument("--inject", default=None, help=f"file containing {MARKER} to replace")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    reports = load(args.reports)
    if not reports:
        print("no reports found", file=sys.stderr)
        return 1

    # A comparison of two runs over different data is not a comparison. Each report
    # records the dataset fingerprint it was measured on; refuse to publish a table
    # that silently mixes them.
    prints = {r.get("dataset_fingerprint") for r in reports}
    if len(prints) > 1:
        print(
            "FAIL: these reports were measured on different datasets: "
            + ", ".join(f"{r['checkpoint']}={r.get('dataset_fingerprint')}" for r in reports)
            + ". Re-run them back to back with `sim` stopped.",
            file=sys.stderr,
        )
        return 1
    if prints == {None}:
        print(
            "WARNING: no dataset fingerprint in these reports (produced by an older harness); "
            "cannot prove they share a dataset.",
            file=sys.stderr,
        )
    else:
        print(f"dataset fingerprint: {prints.pop()} (identical across all reports)")
    body = render(reports, args.title)

    if args.out:
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print(f"wrote {args.out}")

    if args.inject:
        target = Path(args.inject)
        text = target.read_text(encoding="utf-8")
        if MARKER not in text:
            print(f"FAIL: {target} has no {MARKER} marker", file=sys.stderr)
            return 1
        target.write_text(text.replace(MARKER, body), encoding="utf-8")
        print(f"injected into {target}")

    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
