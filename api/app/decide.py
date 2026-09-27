"""HTTP surface for the Laya decision model.

Two endpoints, both deliberately thin:

``GET /api/laya/health``
    Whether the model server is reachable, and what the client is configured with.

``GET /api/laya/questions``
    The frozen question schema. The benchmark fetches this rather than importing
    the module, so the option dictionaries the benchmark scores against cannot
    drift from the ones the API sends.

``GET /api/decide/{product_id}``
    The whole point of the integration: one product-day, decided twice. Laya's
    three typed answers with their probability distributions, next to the rule
    engine's verdict for the identical state, plus whether they agree.

The agreement booleans are the honest part of this endpoint and are defined
explicitly:

* ``pattern``   - Laya's chosen option equals the rule engine's top-severity
                  product pattern, or ``none`` when the engine fired nothing.
* ``severity``  - Laya's rubric level equals the engine's highest severity.
* ``reorder``   - Laya's P(true) at or above 0.5 equals whether ``STOCKOUT_RISK``
                  fired at warn or critical.

A single product-day can fire several rules at once, so the engine's verdict is
reduced to the highest severity, ties broken by score. That reduction is a choice,
and it is the same one the benchmark makes, which is why it lives here once.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import rules
from .db import pool
from .laya import (
    CATEGORY_SCOPE_PATTERNS,
    KNOWN_CHECKPOINTS,
    QUESTION_PATTERN,
    QUESTION_REORDER,
    QUESTION_SEVERITY,
    PATTERN_OPTIONS,
    SEVERITY_RUBRIC,
    build_questions,
    laya,
    render_state,
    states_for,
)
from .schemas import PlaygroundIn, PlaygroundOut

router = APIRouter(tags=["laya"])

SEVERITY_RANK: dict[str, int] = {name: i for i, name in enumerate(SEVERITY_RUBRIC)}

#: Laya's ``noul`` probability above which we call it a yes.
NOUL_THRESHOLD = 0.5


def _product_hits(signals: list[dict[str, Any]], product_id: int) -> list[dict[str, Any]]:
    return [
        signal
        for signal in signals
        if signal.get("subject_type") == "product" and int(signal.get("subject_id", -1)) == product_id
    ]


def rule_verdict(signals: list[dict[str, Any]], product_id: int) -> dict[str, Any]:
    """Reduce the rule engine's hits for one product to a single comparable verdict."""
    hits = _product_hits(signals, product_id)
    if not hits:
        return {
            "pattern": "none",
            "severity": "none",
            "severity_index": 0,
            "reorder": False,
            "patterns": [],
            "hits": [],
        }

    severity = max(hits, key=lambda s: SEVERITY_RANK.get(str(s["severity"]), 0))["severity"]
    top = max(
        hits,
        key=lambda s: (SEVERITY_RANK.get(str(s["severity"]), 0), float(s.get("score") or 0.0)),
    )
    reorder = any(
        s["pattern"] == "STOCKOUT_RISK" and SEVERITY_RANK.get(str(s["severity"]), 0) >= 2
        for s in hits
    )
    return {
        "pattern": str(top["pattern"]),
        "severity": str(severity),
        "severity_index": SEVERITY_RANK.get(str(severity), 0),
        "reorder": reorder,
        "patterns": [str(s["pattern"]) for s in hits],
        "hits": hits,
    }


def _agreement(laya_result: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    answers = laya_result.get("answers") or {}
    pattern = answers.get(QUESTION_PATTERN) or {}
    severity = answers.get(QUESTION_SEVERITY) or {}
    reorder = answers.get(QUESTION_REORDER) or {}

    laya_pattern = pattern.get("answer")
    laya_level = severity.get("answer")
    noul = reorder.get("answer")
    laya_reorder = (
        float(noul) >= NOUL_THRESHOLD if isinstance(noul, (int, float)) else None
    )

    return {
        "pattern": (laya_pattern == verdict["pattern"]) if laya_pattern is not None else None,
        "severity": (laya_level == verdict["severity"]) if laya_level is not None else None,
        "reorder": (
            (laya_reorder == verdict["reorder"]) if laya_reorder is not None else None
        ),
        "laya": {
            "pattern": laya_pattern,
            "severity": laya_level,
            "severity_index": severity.get("level_index"),
            "reorder_probability": noul,
            "reorder": laya_reorder,
        },
        "rules": {
            "pattern": verdict["pattern"],
            "severity": verdict["severity"],
            "severity_index": verdict["severity_index"],
            "reorder": verdict["reorder"],
        },
    }


@router.get("/laya/health")
async def laya_health() -> dict[str, Any]:
    """Is the model server up, and what is the client pointed at."""
    probe = await laya.health()
    return {
        **probe,
        "enabled": laya.enabled,
        "url": laya.base_url,
        "timeout_seconds": laya.timeout,
        "checkpoint_options": list(PATTERN_OPTIONS),
    }


@router.get("/laya/questions")
async def laya_questions() -> dict[str, Any]:
    """The frozen typed-question schema, so nothing downstream can drift."""
    return {
        "questions": build_questions(),
        "pattern_options": dict(PATTERN_OPTIONS),
        "category_scope_patterns": list(CATEGORY_SCOPE_PATTERNS),
        "severity_rubric": list(SEVERITY_RUBRIC),
        "noul_threshold": NOUL_THRESHOLD,
    }


@router.post("/laya/playground", response_model=PlaygroundOut)
async def laya_playground(body: PlaygroundIn) -> dict[str, Any]:
    """Ask Laya caller-defined typed questions about caller-written free text.

    One CPU forward pass, ~5.5 s p50 here, so this is an explicit *Run* action and
    never something a keystroke triggers.

    The state and the question schema are both bounded by :class:`PlaygroundIn`
    (4000 characters of state, 8 questions, 20 options or rubric levels each), and
    the declared ``type`` is what selects the normalization: an arbitrary question
    name is flattened as the primitive it declared, not defaulted to ``choice``.

    Availability is reported, never raised: an unreachable or disabled model
    answers ``{"available": false, "detail": ...}`` with HTTP 200, exactly like
    ``/api/decide``.
    """
    if body.model is not None and body.model not in KNOWN_CHECKPOINTS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown checkpoint {body.model!r}; expected one of {list(KNOWN_CHECKPOINTS)}",
        )

    questions = {
        name: question.model_dump(exclude_none=True) for name, question in body.questions.items()
    }
    result = await laya.predict(body.state, questions=questions, model=body.model)

    return {
        "state": body.state,
        "questions": questions,
        "laya": result,
    }


@router.get("/decide/{product_id}")
async def decide(
    product_id: int,
    day: date | None = Query(default=None, description="defaults to the newest dataset day"),
    model: str | None = Query(
        default=None,
        description="Laya checkpoint to answer: english | multilingual | typed-decisions",
    ),
) -> dict[str, Any]:
    """Decide one product-day with Laya and with the rule engine, side by side."""
    if model is not None and model not in KNOWN_CHECKPOINTS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown checkpoint {model!r}; expected one of {list(KNOWN_CHECKPOINTS)}",
        )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            target = await rules.resolve_target_day(cur, day)
            if target is None:
                raise HTTPException(status_code=404, detail="dataset is empty")

            states = await states_for(cur, target, [product_id])
            state = states.get(product_id)
            if state is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"product {product_id} has no facts on {target.isoformat()}",
                )

            scan = await rules.scan(cur, day=target, product_ids=[product_id], persist=False)

    state_text = render_state(state)
    result = await laya.predict(state_text, model=model)
    verdict = rule_verdict(list(scan.signals), product_id)

    return {
        "product_id": product_id,
        "day": target.isoformat(),
        "state": {
            "name": state.name,
            "category": state.category,
            "brand": state.brand,
            "is_private_label": state.is_private_label,
            "is_perishable": state.is_perishable,
            "units": state.units,
            "units_baseline": state.units_baseline,
            "unit_lift": state.unit_lift,
            "avg_price": state.avg_price,
            "price_baseline": state.price_baseline,
            "price_lift": state.price_lift,
            "margin_pct": state.margin_pct,
            "margin_pct_baseline": state.margin_pct_baseline,
            "inventory": state.inventory,
            "inventory_cover_days": state.inventory_cover_days,
            "store_count": state.store_count,
            "promo_stores": state.promo_stores,
            "observations": state.observations,
        },
        "state_text": state_text,
        "laya": result,
        "rules": verdict,
        "agreement": _agreement(result, verdict),
    }
