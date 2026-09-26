"""Decision-pattern catalog and rule engine.

The catalog in :data:`RULES` is declarative and is served verbatim by
``GET /api/patterns``. Evaluation follows ``docs/CONTRACT.md`` §4:

* baseline for a subject at day ``d`` is the **median** over ``d-14 .. d-1``
  (``percentile_cont(0.5) WITHIN GROUP``), suppressed when fewer than
  ``min_obs = 4`` observations exist;
* a subject fires at most one severity per pattern, but all matching patterns
  are emitted;
* ``evidence`` always carries ``metric``/``value``/``baseline``/``window_days``/
  ``observations`` plus rule-specific keys;
* ``action`` is rendered server-side with the real numbers and returned verbatim
  (the UI never re-templates it).

The freshness path from §4 lives here too: ``sim`` only mutates the ``target_day``,
so the target-day rollup row is recomputed **live** from ``market_facts`` while all
earlier days come from ``mv_product_day`` / ``mv_category_day``. Both paths use the
§1 metric definitions verbatim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from psycopg.types.json import Jsonb

log = logging.getLogger("laya.api.rules")

PRODUCT = "product"
CATEGORY = "category"

SEVERITIES = ("info", "warn", "critical")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    label: str
    scope: str
    description: str
    thresholds: dict[str, float]
    severity_bands: tuple[tuple[str, str], ...]
    action_template: str
    evidence_keys: tuple[str, ...]
    baseline_days: int = 14
    min_obs: int = 4
    # True when a *smaller* metric is worse (collapse, margin, cover, ...).
    # Not part of the served catalog: `as_dict` stays exactly §5-shaped.
    lower_is_worse: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "description": self.description,
            "thresholds": dict(self.thresholds),
            "severity_bands": [{"severity": sev, "when": when} for sev, when in self.severity_bands],
            "action_template": self.action_template,
            "baseline_days": self.baseline_days,
            "min_obs": self.min_obs,
        }


RULES: tuple[Rule, ...] = (
    Rule(
        id="DEMAND_SURGE",
        label="Demand surge",
        scope=PRODUCT,
        description=(
            "Units sold are far above the 14-day median. Replenish before the next delivery "
            "window; verify the lift is real demand and not a stockout elsewhere."
        ),
        thresholds={"warn": 1.8, "critical": 2.6, "min_baseline_units": 20.0},
        severity_bands=(("critical", "lift >= 2.6"), ("warn", "lift >= 1.8")),
        action_template=(
            "Replenish {label}: {value:.0f} units vs {baseline:.0f} baseline ({lift:.2f}x). "
            "Confirm competitor stockout before raising price."
        ),
        evidence_keys=("lift", "store_count"),
    ),
    Rule(
        id="DEMAND_COLLAPSE",
        label="Demand collapse",
        scope=PRODUCT,
        description=(
            "Units sold are far below the 14-day median. Check assortment, pricing and shelf "
            "availability before the next order is placed."
        ),
        thresholds={"warn": 0.55, "critical": 0.35, "min_baseline_units": 20.0},
        severity_bands=(("critical", "ratio <= 0.35"), ("warn", "ratio <= 0.55")),
        action_template=(
            "Check {label}: {value:.0f} units vs {baseline:.0f} baseline ({lift:.2f}x). "
            "Inspect assortment, pricing and shelf availability before cutting the order."
        ),
        evidence_keys=("lift", "store_count"),
        lower_is_worse=True,
    ),
    Rule(
        id="PRICE_SPIKE",
        label="Price spike",
        scope=PRODUCT,
        description=(
            "The unweighted store mean price moved up sharply against its 14-day median. "
            "Verify the price file before the change flows into demand."
        ),
        thresholds={"warn": 1.06, "critical": 1.15},
        severity_bands=(("critical", "pr >= 1.15"), ("warn", "pr >= 1.06")),
        action_template=(
            "Review {label}: average price {value:.2f} vs {baseline:.2f} baseline ({lift:.2f}x). "
            "Verify the price file and watch unit velocity before the next order."
        ),
        evidence_keys=("lift", "store_count"),
    ),
    Rule(
        id="PRICE_CUT_UNANSWERED",
        label="Price cut unanswered",
        scope=PRODUCT,
        description=(
            "Price was cut but volume did not respond. Either the cut was not published to "
            "stores or a competitor is matching it."
        ),
        thresholds={"max_pr": 0.94, "max_lift": 1.05},
        severity_bands=(("warn", "pr <= 0.94 and lift < 1.05"),),
        action_template=(
            "Escalate {label}: price cut to {value:.2f} from {baseline:.2f} baseline "
            "({lift:.2f}x) with no volume response. Confirm the cut was published to stores."
        ),
        evidence_keys=("lift", "store_count"),
        lower_is_worse=True,
    ),
    Rule(
        id="MARGIN_SQUEEZE",
        label="Margin squeeze",
        scope=PRODUCT,
        description=(
            "Margin dropped more than 3 points below its 14-day median and sits under 18%. "
            "Cost price or promo depth is eating the contribution."
        ),
        thresholds={"gap": 0.03, "max_margin": 0.18, "critical_margin": 0.10},
        severity_bands=(("critical", "margin < 0.10"), ("warn", "margin < 0.18 and gap > 0.03")),
        action_template=(
            "Renegotiate {label}: margin {value:.1%} vs {baseline:.1%} baseline "
            "(gap {gap:.1%}). Review cost price and promotional depth for this SKU."
        ),
        evidence_keys=("margin_gap", "store_count"),
        lower_is_worse=True,
    ),
    Rule(
        id="STOCKOUT_RISK",
        label="Stockout risk",
        scope=PRODUCT,
        description=(
            "Forward cover, measured in days of median demand, is below the safe threshold. "
            "Raise the order before the next delivery window."
        ),
        thresholds={"warn": 1.2, "critical": 0.6},
        severity_bands=(("critical", "cover < 0.6"), ("warn", "cover < 1.2")),
        action_template=(
            "Replenish {label}: {cover:.1f} days of cover on {inventory:.0f} units against a "
            "{baseline:.0f} unit/day baseline. Raise the order now."
        ),
        evidence_keys=("cover", "inventory", "store_count"),
        lower_is_worse=True,
    ),
    Rule(
        id="PROMO_INEFFECTIVE",
        label="Promo ineffective",
        scope=PRODUCT,
        description=(
            "Half or more of the stores are on promotion but volume barely moved. "
            "Rework or stop the mechanic."
        ),
        thresholds={"min_promo_share": 0.5, "max_lift": 1.15},
        severity_bands=(("warn", "promo_share >= 0.5 and lift < 1.15"),),
        action_template=(
            "Rework the {label} promo: {promo_share:.1%} of stores on promo for only "
            "{lift:.2f}x volume. Reallocate the trade spend to a proven mechanic."
        ),
        evidence_keys=("promo_share", "lift", "promo_stores", "store_count"),
    ),
    Rule(
        id="CATEGORY_DRIFT",
        label="Category drift",
        scope=CATEGORY,
        description=(
            "Category units moved away from the 14-day median. Establish whether a few SKUs or "
            "the whole category drives the shift."
        ),
        thresholds={"info": 1.25, "warn": 1.6},
        severity_bands=(("warn", "lift >= 1.6"), ("info", "lift >= 1.25")),
        action_template=(
            "Investigate {category}: {value:.0f} units vs {baseline:.0f} baseline ({lift:.2f}x). "
            "Check whether a few SKUs or the whole category drives the shift."
        ),
        evidence_keys=("lift", "product_count"),
    ),
    Rule(
        id="PRIVATE_LABEL_GAIN",
        label="Private label gain",
        scope=CATEGORY,
        description=(
            "Private-label share of category units rose against its 14-day median. "
            "Confirm own-brand supply covers the higher run rate."
        ),
        thresholds={"info": 0.02},
        severity_bands=(("info", "pl_share_delta >= 0.02"),),
        action_template=(
            "Lean into private label in {category}: share {value:.1%} vs {baseline:.1%} baseline "
            "(+{delta:.1%}). Confirm own-brand supply covers the demand."
        ),
        evidence_keys=("pl_share_delta", "pl_units", "product_count"),
    ),
)

RULES_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}

BASELINE_DAYS = 14
MIN_OBS = 4

# Guard against a pathological `ANY(%s)` array on the manual scan path.
MAX_SCAN_SUBJECTS = 5000
# Bound for `LIMIT` on the unparameterised "all products" scan.
MAX_SCAN_PRODUCTS = 5000
# Bounded queue between the LISTEN thread and the event loop: a burst of ticks
# must never grow memory without limit.
TICK_QUEUE_SIZE = 64
# Ceiling for the exponential reconnect backoff on the LISTEN connection.
LISTEN_BACKOFF_MAX_SECONDS = 30.0


def catalog() -> dict[str, Any]:
    """The ``GET /api/patterns`` payload, served verbatim from the catalog."""
    return {"patterns": [rule.as_dict() for rule in RULES]}


# ---------------------------------------------------------------------------
# Rollup rows
# ---------------------------------------------------------------------------


@dataclass
class ProductDay:
    """One ``mv_product_day`` row, or the live ``target_day`` equivalent."""

    product_id: int
    day: date
    units: int
    revenue: float
    cogs: float
    avg_price: float
    inventory: int
    store_count: int
    promo_stores: int

    @property
    def margin(self) -> float:
        return (self.revenue - self.cogs) / self.revenue if self.revenue else 0.0

    @property
    def promo_share(self) -> float:
        return self.promo_stores / self.store_count if self.store_count else 0.0


@dataclass
class CategoryDay:
    """One ``mv_category_day`` row, or the live ``target_day`` equivalent."""

    category: str
    day: date
    units: int
    revenue: float
    cogs: float
    pl_units: int
    product_count: int

    @property
    def margin(self) -> float:
        return (self.revenue - self.cogs) / self.revenue if self.revenue else 0.0

    @property
    def pl_share(self) -> float:
        return self.pl_units / self.units if self.units else 0.0


@dataclass
class ProductBaseline:
    product_id: int
    med_units: float | None
    med_avg_price: float | None
    med_margin: float | None
    observations: int


@dataclass
class CategoryBaseline:
    category: str
    med_units: float | None
    med_pl_share: float | None
    observations: int


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# SQL: live target-day rollups (freshness path, §4)
# ---------------------------------------------------------------------------

_LIVE_PRODUCT_SQL = """
SELECT
    f.product_id,
    %(day)s::date                                    AS day,
    sum(f.units_sold)::bigint                        AS units,
    sum(f.units_sold * f.price)                      AS revenue,
    sum(f.units_sold * f.unit_cost)                  AS cogs,
    avg(f.price)                                     AS avg_price,
    sum(f.inventory)::bigint                         AS inventory,
    count(*)::bigint                                 AS store_count,
    count(*) FILTER (WHERE f.promo_flag)::bigint     AS promo_stores
FROM market_facts f
WHERE f.day = %(day)s::date AND f.product_id = ANY(%(ids)s)
GROUP BY f.product_id
"""

_LIVE_CATEGORY_SQL = """
SELECT
    p.category,
    %(day)s::date                                                        AS day,
    sum(f.units_sold)::bigint                                            AS units,
    sum(f.units_sold * f.price)                                          AS revenue,
    sum(f.units_sold * f.unit_cost)                                      AS cogs,
    sum(f.units_sold) FILTER (WHERE p.is_private_label)::bigint          AS pl_units,
    count(DISTINCT f.product_id)::bigint                                 AS product_count
FROM market_facts f
JOIN products p USING (product_id)
WHERE f.day = %(day)s::date AND p.category = ANY(%(categories)s)
GROUP BY p.category
"""


async def live_product_day(cur: Any, day: date, product_ids: Sequence[int]) -> dict[int, ProductDay]:
    """Recompute the ``day`` rollup for ``product_ids`` live from ``market_facts``."""
    ids = list(dict.fromkeys(int(p) for p in product_ids))
    if not ids:
        return {}
    await cur.execute(_LIVE_PRODUCT_SQL, {"day": day, "ids": ids})
    return {
        int(row["product_id"]): ProductDay(
            product_id=int(row["product_id"]),
            day=row["day"],
            units=int(row["units"] or 0),
            revenue=float(row["revenue"] or 0.0),
            cogs=float(row["cogs"] or 0.0),
            avg_price=float(row["avg_price"] or 0.0),
            inventory=int(row["inventory"] or 0),
            store_count=int(row["store_count"] or 0),
            promo_stores=int(row["promo_stores"] or 0),
        )
        for row in await cur.fetchall()
    }


async def live_category_day(cur: Any, day: date, categories: Sequence[str]) -> dict[str, CategoryDay]:
    """Recompute the ``day`` category rollup live from ``market_facts``."""
    cats = list(dict.fromkeys(str(c) for c in categories if c))
    if not cats:
        return {}
    await cur.execute(_LIVE_CATEGORY_SQL, {"day": day, "categories": cats})
    return {
        str(row["category"]): CategoryDay(
            category=str(row["category"]),
            day=row["day"],
            units=int(row["units"] or 0),
            revenue=float(row["revenue"] or 0.0),
            cogs=float(row["cogs"] or 0.0),
            pl_units=int(row["pl_units"] or 0),
            product_count=int(row["product_count"] or 0),
        )
        for row in await cur.fetchall()
    }


# ---------------------------------------------------------------------------
# SQL: baselines (median over d-14 .. d-1)
# ---------------------------------------------------------------------------

_PRODUCT_BASELINE_SQL = """
SELECT
    h.product_id,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY h.units)     AS med_units,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY h.avg_price) AS med_avg_price,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY h.margin)    AS med_margin,
    count(*)::bigint                                         AS observations
FROM (
    SELECT
        m.product_id,
        m.units,
        m.avg_price,
        CASE WHEN m.revenue > 0 THEN (m.revenue - m.cogs) / m.revenue ELSE 0 END AS margin
    FROM mv_product_day m
    WHERE m.product_id = ANY(%(ids)s)
      AND m.day >= %(from_day)s::date
      AND m.day <= %(to_day)s::date
) h
GROUP BY h.product_id
"""

_CATEGORY_BASELINE_SQL = """
SELECT
    h.category,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY h.units)    AS med_units,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY h.pl_share) AS med_pl_share,
    count(*)::bigint                                        AS observations
FROM (
    SELECT
        c.category,
        c.units,
        CASE WHEN c.units > 0 THEN c.pl_units::float8 / c.units ELSE 0 END AS pl_share
    FROM mv_category_day c
    WHERE c.category = ANY(%(categories)s)
      AND c.day >= %(from_day)s::date
      AND c.day <= %(to_day)s::date
) h
GROUP BY h.category
"""


async def product_baselines(
    cur: Any,
    product_ids: Sequence[int],
    target_day: date,
    baseline_days: int = BASELINE_DAYS,
) -> dict[int, ProductBaseline]:
    ids = list(dict.fromkeys(int(p) for p in product_ids))
    if not ids:
        return {}
    await cur.execute(
        _PRODUCT_BASELINE_SQL,
        {
            "ids": ids,
            "from_day": target_day - timedelta(days=baseline_days),
            "to_day": target_day - timedelta(days=1),
        },
    )
    return {
        int(row["product_id"]): ProductBaseline(
            product_id=int(row["product_id"]),
            med_units=_f(row["med_units"]),
            med_avg_price=_f(row["med_avg_price"]),
            med_margin=_f(row["med_margin"]),
            observations=int(row["observations"] or 0),
        )
        for row in await cur.fetchall()
    }


async def category_baselines(
    cur: Any,
    categories: Sequence[str],
    target_day: date,
    baseline_days: int = BASELINE_DAYS,
) -> dict[str, CategoryBaseline]:
    cats = list(dict.fromkeys(str(c) for c in categories if c))
    if not cats:
        return {}
    await cur.execute(
        _CATEGORY_BASELINE_SQL,
        {
            "categories": cats,
            "from_day": target_day - timedelta(days=baseline_days),
            "to_day": target_day - timedelta(days=1),
        },
    )
    return {
        str(row["category"]): CategoryBaseline(
            category=str(row["category"]),
            med_units=_f(row["med_units"]),
            med_pl_share=_f(row["med_pl_share"]),
            observations=int(row["observations"] or 0),
        )
        for row in await cur.fetchall()
    }


# ---------------------------------------------------------------------------
# Hits
# ---------------------------------------------------------------------------


@dataclass
class RuleHit:
    pattern: str
    severity: str
    subject_type: str
    subject_id: int
    subject_label: str
    day: date
    score: float
    evidence: dict[str, Any]
    action: str

    def fingerprint(self) -> tuple:
        """Identity of an emitted frame: a hit only re-emits when this changes."""
        return (
            self.pattern,
            self.subject_type,
            self.subject_id,
            self.day.isoformat(),
            self.severity,
            round(float(self.score), 4),
            self.action,
        )

    def as_signal(self, signal_id: int = 0, fired_at: str | None = None) -> dict[str, Any]:
        from .db import now_iso

        return {
            "signal_id": signal_id,
            "fired_at": fired_at or now_iso(),
            "day": self.day,
            "pattern": self.pattern,
            "severity": self.severity,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "subject_label": self.subject_label,
            "score": round(float(self.score), 4),
            "evidence": self.evidence,
            "action": self.action,
        }


def _band(rule: Rule, value: float) -> str | None:
    """Highest matching severity for a rule, or ``None`` when nothing fires.

    ``severity_bands`` is declared worst-first, so the first band whose numeric
    threshold is crossed wins. Rules that carry no numeric threshold for a band
    (``PRICE_CUT_UNANSWERED``, ``PROMO_INEFFECTIVE``, ``PRIVATE_LABEL_GAIN``)
    fire whenever their call site reached this point.
    """
    for severity, _when in rule.severity_bands:
        threshold = rule.thresholds.get(severity)
        if threshold is None:
            return severity
        if rule.lower_is_worse:
            if value <= threshold:
                return severity
        elif value >= threshold:
            return severity
    return None


def _base_evidence(metric: str, value: float, baseline: float | None, observations: int) -> dict[str, Any]:
    return {
        "metric": metric,
        "value": round(float(value), 4),
        "baseline": None if baseline is None else round(float(baseline), 4),
        "window_days": BASELINE_DAYS,
        "observations": int(observations),
    }


def _action(rule: Rule, **context: Any) -> str:
    """Render ``action`` server-side; the numbers are baked in for the UI."""
    defaults: dict[str, Any] = {
        "label": "",
        "category": "",
        "value": 0.0,
        "baseline": 0.0,
        "lift": 0.0,
        "delta": 0.0,
        "gap": 0.0,
        "cover": 0.0,
        "inventory": 0.0,
        "promo_share": 0.0,
        "store_count": 0,
    }
    defaults.update(context)
    try:
        return rule.action_template.format(**defaults)
    except (KeyError, ValueError, TypeError):  # pragma: no cover - template/context mismatch
        log.exception("action render failed for %s", rule.id)
        return rule.action_template


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_products(
    rows: Iterable[ProductDay],
    baselines: dict[int, ProductBaseline],
    labels: dict[int, str] | None = None,
    min_obs: int = MIN_OBS,
) -> list[RuleHit]:
    """Evaluate every product-scope rule for the given target-day rows."""
    labels = labels or {}
    hits: list[RuleHit] = []
    for row in rows:
        base = baselines.get(row.product_id)
        if base is None or base.observations < min_obs:
            continue
        obs = base.observations
        med_units = base.med_units
        label = labels.get(row.product_id) or f"product {row.product_id}"
        units = float(row.units)
        margin = row.margin

        # lift / ratio need a strictly positive unit baseline to be meaningful.
        lift: float | None = None
        if med_units is not None and med_units > 0:
            lift = units / med_units

        pr: float | None = None
        if base.med_avg_price is not None and base.med_avg_price > 0:
            pr = row.avg_price / base.med_avg_price

        # --- DEMAND_SURGE -------------------------------------------------
        rule = RULES_BY_ID["DEMAND_SURGE"]
        if lift is not None and med_units is not None and med_units >= rule.thresholds["min_baseline_units"]:
            severity = _band(rule, lift)
            if severity:
                evidence = _base_evidence("units", units, med_units, obs)
                evidence.update({"lift": round(lift, 4), "store_count": row.store_count})
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=PRODUCT,
                        subject_id=row.product_id,
                        subject_label=label,
                        day=row.day,
                        score=lift,
                        evidence=evidence,
                        action=_action(rule, label=label, value=units, baseline=med_units, lift=lift),
                    )
                )

        # --- DEMAND_COLLAPSE ----------------------------------------------
        rule = RULES_BY_ID["DEMAND_COLLAPSE"]
        if lift is not None and med_units is not None and med_units >= rule.thresholds["min_baseline_units"]:
            severity = _band(rule, lift)
            if severity:
                evidence = _base_evidence("units", units, med_units, obs)
                evidence.update({"lift": round(lift, 4), "store_count": row.store_count})
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=PRODUCT,
                        subject_id=row.product_id,
                        subject_label=label,
                        day=row.day,
                        score=max(0.0, 1.0 - lift),
                        evidence=evidence,
                        action=_action(rule, label=label, value=units, baseline=med_units, lift=lift),
                    )
                )

        # --- PRICE_SPIKE ---------------------------------------------------
        rule = RULES_BY_ID["PRICE_SPIKE"]
        if pr is not None and base.med_avg_price is not None:
            severity = _band(rule, pr)
            if severity:
                evidence = _base_evidence("avg_price", row.avg_price, base.med_avg_price, obs)
                evidence.update({"lift": round(pr, 4), "store_count": row.store_count})
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=PRODUCT,
                        subject_id=row.product_id,
                        subject_label=label,
                        day=row.day,
                        score=pr,
                        evidence=evidence,
                        action=_action(rule, label=label, value=row.avg_price, baseline=base.med_avg_price, lift=pr),
                    )
                )

        # --- PRICE_CUT_UNANSWERED ------------------------------------------
        rule = RULES_BY_ID["PRICE_CUT_UNANSWERED"]
        if (
            pr is not None
            and lift is not None
            and base.med_avg_price is not None
            and pr <= rule.thresholds["max_pr"]
            and lift < rule.thresholds["max_lift"]
        ):
            severity = _band(rule, pr)
            if severity:
                evidence = _base_evidence("avg_price", row.avg_price, base.med_avg_price, obs)
                evidence.update({"lift": round(lift, 4), "store_count": row.store_count})
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=PRODUCT,
                        subject_id=row.product_id,
                        subject_label=label,
                        day=row.day,
                        score=pr,
                        evidence=evidence,
                        action=_action(rule, label=label, value=row.avg_price, baseline=base.med_avg_price, lift=lift),
                    )
                )

        # --- MARGIN_SQUEEZE ------------------------------------------------
        rule = RULES_BY_ID["MARGIN_SQUEEZE"]
        if base.med_margin is not None:
            gap = base.med_margin - margin
            if gap > rule.thresholds["gap"] and margin < rule.thresholds["max_margin"]:
                severity = _band(rule, margin)
                if severity:
                    evidence = _base_evidence("margin", margin, base.med_margin, obs)
                    evidence.update(
                        {
                            "margin_gap": round(gap, 4),
                            "store_count": row.store_count,
                            "revenue": round(row.revenue, 2),
                            "cogs": round(row.cogs, 2),
                        }
                    )
                    hits.append(
                        RuleHit(
                            pattern=rule.id,
                            severity=severity,
                            subject_type=PRODUCT,
                            subject_id=row.product_id,
                            subject_label=label,
                            day=row.day,
                            score=gap,
                            evidence=evidence,
                            action=_action(
                                rule, label=label, value=margin, baseline=base.med_margin, gap=gap
                            ),
                        )
                    )

        # --- STOCKOUT_RISK -------------------------------------------------
        rule = RULES_BY_ID["STOCKOUT_RISK"]
        if med_units is not None and med_units > 0:
            cover = float(row.inventory) / med_units
            severity = _band(rule, cover)
            if severity:
                evidence = _base_evidence("cover", cover, med_units, obs)
                evidence.update(
                    {
                        "cover": round(cover, 4),
                        "inventory": row.inventory,
                        "store_count": row.store_count,
                        "lift": None if lift is None else round(lift, 4),
                    }
                )
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=PRODUCT,
                        subject_id=row.product_id,
                        subject_label=label,
                        day=row.day,
                        score=max(0.0, rule.thresholds["warn"] - cover),
                        evidence=evidence,
                        action=_action(
                            rule,
                            label=label,
                            cover=cover,
                            inventory=float(row.inventory),
                            baseline=med_units,
                        ),
                    )
                )

        # --- PROMO_INEFFECTIVE ---------------------------------------------
        rule = RULES_BY_ID["PROMO_INEFFECTIVE"]
        if lift is not None and row.promo_share >= rule.thresholds["min_promo_share"] and lift < rule.thresholds["max_lift"]:
            severity = _band(rule, row.promo_share)
            if severity:
                evidence = _base_evidence("share", row.promo_share, None, obs)
                evidence.update(
                    {
                        "promo_share": round(row.promo_share, 4),
                        "lift": round(lift, 4),
                        "promo_stores": row.promo_stores,
                        "store_count": row.store_count,
                    }
                )
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=PRODUCT,
                        subject_id=row.product_id,
                        subject_label=label,
                        day=row.day,
                        score=row.promo_share,
                        evidence=evidence,
                        action=_action(
                            rule, label=label, promo_share=row.promo_share, lift=lift
                        ),
                    )
                )

    return hits


def evaluate_categories(
    rows: Iterable[CategoryDay],
    baselines: dict[str, CategoryBaseline],
    min_obs: int = MIN_OBS,
) -> list[RuleHit]:
    """Evaluate every category-scope rule for the given target-day rows."""
    hits: list[RuleHit] = []
    for row in rows:
        base = baselines.get(row.category)
        if base is None or base.observations < min_obs:
            continue
        obs = base.observations
        label = row.category
        units = float(row.units)

        lift: float | None = None
        if base.med_units is not None and base.med_units > 0:
            lift = units / base.med_units

        # --- CATEGORY_DRIFT ------------------------------------------------
        rule = RULES_BY_ID["CATEGORY_DRIFT"]
        if lift is not None and base.med_units is not None:
            severity = _band(rule, lift)
            if severity:
                evidence = _base_evidence("units", units, base.med_units, obs)
                evidence.update({"lift": round(lift, 4), "product_count": row.product_count})
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=CATEGORY,
                        subject_id=_category_key(row.category),
                        subject_label=label,
                        day=row.day,
                        score=lift,
                        evidence=evidence,
                        action=_action(
                            rule, category=label, value=units, baseline=base.med_units, lift=lift
                        ),
                    )
                )

        # --- PRIVATE_LABEL_GAIN --------------------------------------------
        rule = RULES_BY_ID["PRIVATE_LABEL_GAIN"]
        if base.med_pl_share is not None:
            share = row.pl_share
            delta = share - base.med_pl_share
            severity = _band(rule, delta)
            if severity:
                evidence = _base_evidence("share", share, base.med_pl_share, obs)
                evidence.update(
                    {
                        "pl_share_delta": round(delta, 4),
                        "pl_units": row.pl_units,
                        "product_count": row.product_count,
                    }
                )
                hits.append(
                    RuleHit(
                        pattern=rule.id,
                        severity=severity,
                        subject_type=CATEGORY,
                        subject_id=_category_key(row.category),
                        subject_label=label,
                        day=row.day,
                        score=delta,
                        evidence=evidence,
                        action=_action(
                            rule, category=label, value=share, baseline=base.med_pl_share, delta=delta
                        ),
                    )
                )

    return hits


# ---------------------------------------------------------------------------
# Category subject ids
# ---------------------------------------------------------------------------

# `signals.subject_id` is an integer, but the category scope is keyed by name.
# The mapping is deterministic (a hash of the name) so repeated evaluations,
# restarts and re-generations land on the same id.
_CATEGORY_ID_SALT = 0x1A7A_C47
_CATEGORY_ID_MOD = 2_000_000_000


def _category_key(category: str) -> int:
    """Stable positive integer id for a category name."""
    h = 0
    for ch in category:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (h ^ _CATEGORY_ID_SALT) % _CATEGORY_ID_MOD + 1


def category_key(category: str) -> int:
    return _category_key(category)


# ---------------------------------------------------------------------------
# Scan orchestration (shared by POST /api/patterns/scan and the tick worker)
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO signals
    (day, pattern, severity, subject_type, subject_id, subject_label, score, evidence, action)
VALUES
    (%(day)s, %(pattern)s, %(severity)s, %(subject_type)s, %(subject_id)s,
     %(subject_label)s, %(score)s, %(evidence)s, %(action)s)
ON CONFLICT (pattern, subject_type, subject_id, day) DO UPDATE
SET severity      = EXCLUDED.severity,
    score         = EXCLUDED.score,
    evidence      = EXCLUDED.evidence,
    action        = EXCLUDED.action,
    subject_label = EXCLUDED.subject_label,
    fired_at      = now()
RETURNING signal_id, fired_at, day, pattern, severity, subject_type, subject_id,
          subject_label, score, evidence, action
"""


async def resolve_target_day(cur: Any, day: date | None = None) -> date | None:
    """``None`` means "the latest day present in ``market_facts``"."""
    if day is not None:
        return day
    await cur.execute("SELECT max(day) AS day FROM market_facts")
    row = await cur.fetchone()
    return None if not row else row["day"]


async def all_product_ids(cur: Any) -> list[int]:
    await cur.execute(
        "SELECT product_id FROM products ORDER BY product_id LIMIT %s", (MAX_SCAN_PRODUCTS,)
    )
    return [int(r["product_id"]) for r in await cur.fetchall()]


async def product_labels(cur: Any, product_ids: Sequence[int]) -> dict[int, str]:
    ids = list(dict.fromkeys(int(p) for p in product_ids))
    if not ids:
        return {}
    await cur.execute("SELECT product_id, name FROM products WHERE product_id = ANY(%s)", (ids,))
    return {int(r["product_id"]): str(r["name"]) for r in await cur.fetchall()}


async def categories_for_products(cur: Any, product_ids: Sequence[int]) -> list[str]:
    """Distinct categories touched by ``product_ids`` — drives category rules on a tick."""
    ids = list(dict.fromkeys(int(p) for p in product_ids))
    if not ids:
        return []
    await cur.execute(
        "SELECT DISTINCT category FROM products WHERE product_id = ANY(%s) ORDER BY category", (ids,)
    )
    return [str(r["category"]) for r in await cur.fetchall()]


def _row_to_signal(row: Any) -> dict[str, Any]:
    fired = row["fired_at"]
    return {
        "signal_id": int(row["signal_id"]),
        "fired_at": fired.isoformat() if hasattr(fired, "isoformat") else str(fired),
        "day": row["day"],
        "pattern": row["pattern"],
        "severity": row["severity"],
        "subject_type": row["subject_type"],
        "subject_id": int(row["subject_id"]),
        "subject_label": row["subject_label"],
        "score": float(row["score"]),
        "evidence": row["evidence"],
        "action": row["action"],
    }


async def persist_hits(cur: Any, hits: Sequence[RuleHit]) -> list[dict[str, Any]]:
    """Upsert hits; returns the persisted rows in §5 signal shape."""
    stored: list[dict[str, Any]] = []
    for hit in hits:
        await cur.execute(
            _UPSERT_SQL,
            {
                "day": hit.day,
                "pattern": hit.pattern,
                "severity": hit.severity,
                "subject_type": hit.subject_type,
                "subject_id": hit.subject_id,
                "subject_label": hit.subject_label,
                "score": round(float(hit.score), 4),
                "evidence": Jsonb(hit.evidence),
                "action": hit.action,
            },
        )
        row = await cur.fetchone()
        if row is not None:
            stored.append(_row_to_signal(row))
    return stored


@dataclass
class ScanResult:
    scanned: int = 0
    day: date | None = None
    hits: list[RuleHit] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)


async def scan(
    cur: Any,
    day: date | None = None,
    product_ids: Sequence[int] | None = None,
    categories: Sequence[str] | None = None,
    persist: bool = True,
) -> ScanResult:
    """Evaluate the rule catalog for one day and optionally persist the hits.

    ``product_ids`` and ``categories`` are independent filters, and each scope
    evaluates the subjects that survive them. ``None`` means "unrestricted" for
    that dimension; an empty list means "no subject" and scans nothing.

    So ``product_ids=[1,2,3]`` evaluates the product rules for those three SKUs
    and the category rules for the categories they belong to, while
    ``categories=["Dairy"]`` evaluates the category rule for Dairy plus the
    product rules for every SKU in Dairy.
    """
    target = await resolve_target_day(cur, day)
    if target is None:
        return ScanResult(scanned=0, day=None)

    # --- which products ---------------------------------------------------
    if product_ids is not None:
        ids = list(dict.fromkeys(int(p) for p in product_ids))
    elif categories:
        cats_in = list(dict.fromkeys(str(c) for c in categories if c))
        await cur.execute(
            "SELECT product_id FROM products WHERE category = ANY(%s) ORDER BY product_id", (cats_in,)
        )
        ids = [int(r["product_id"]) for r in await cur.fetchall()]
    else:
        ids = await all_product_ids(cur)
    ids = ids[:MAX_SCAN_SUBJECTS]

    # --- which categories -------------------------------------------------
    if categories is not None:
        cats = list(dict.fromkeys(str(c) for c in categories if c))
    elif product_ids is not None:
        cats = await categories_for_products(cur, ids)
    else:
        await cur.execute("SELECT DISTINCT category FROM products ORDER BY category")
        cats = [str(r["category"]) for r in await cur.fetchall()]

    result = ScanResult(day=target)

    if ids:
        labels = await product_labels(cur, ids)
        rows = await live_product_day(cur, target, ids)
        baselines = await product_baselines(cur, ids, target)
        result.scanned += len(ids)
        result.hits.extend(evaluate_products(rows.values(), baselines, labels))

    if cats:
        rows = await live_category_day(cur, target, cats)
        baselines = await category_baselines(cur, cats, target)
        result.scanned += len(cats)
        result.hits.extend(evaluate_categories(rows.values(), baselines))

    if persist and result.hits:
        result.signals = await persist_hits(cur, result.hits)
    else:
        result.signals = [hit.as_signal(0, None) for hit in result.hits]

    return result
