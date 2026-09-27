"""Laya integration: datasheet state, typed questions, and the model client.

Laya (Convai Innovations, Apache 2.0) is a non-autoregressive System 1 decision
model. It takes a block of state plus typed questions and returns typed answers
with probability distributions instead of generated text. It exposes exactly
three primitives: ``choice``, ``score`` and ``noul`` (yes/no probability).

This module maps one grocery ``(product, day)`` rollup onto one state and one
question per primitive, so a single forward pass covers the whole decision. The
rule engine in :mod:`app.rules` supplies a deterministic label for each question,
which is what makes ``scripts/bench_laya.py`` scoreable without human labelling.

Two decisions worth knowing about:

* The state is rendered as one compact line rather than JSON. The English
  checkpoint reads 512 tokens and scored 0.000 on Khmer while reporting 0.952
  confidence, so nothing about this model rewards padding the input.
* The integration is advisory. CPU inference here costs ~1-2 s per question, and
  the dashboard has to keep working when the ``laya`` container is absent, so
  :class:`LayaClient` reports unavailability instead of raising.

Upstream benchmark facts that shaped the question set, all from the model card and
``BENCHMARKS.md``: the base checkpoints are near chance zero-shot on typed
decisions (0.361 and 0.352 against a 0.461 majority-class baseline), ``score`` is
the weakest primitive (SST-5 0.372), both checkpoints ship over-confident, and
``choice`` questions should stay under ~20 options.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

import httpx

from .rules import BASELINE_DAYS, MIN_OBS, live_product_day, product_baselines
from .settings import settings

log = logging.getLogger("api.laya")

#: The question a product-day can actually answer: ``none`` plus the seven
#: product-scope patterns. Eight options, comfortably under the ~20-option ceiling
#: upstream warns about.
#:
#: ``CATEGORY_DRIFT`` and ``PRIVATE_LABEL_GAIN`` are deliberately absent. They are
#: category-scope rules, so they can never be the correct answer for a single
#: product state, and offering them would be an unfair question that inflates the
#: apparent error rate. The engine still evaluates them separately.
#: Served by GET /api/laya/questions and mirrored in scripts/laya_probe.py,
#: which runs in the model image and cannot import this module.
PATTERN_OPTIONS: dict[str, str] = {
    "none": "no decision pattern applies, behaviour is in line with its own baseline",
    "DEMAND_SURGE": "units well above the trailing baseline",
    "DEMAND_COLLAPSE": "units well below the trailing baseline",
    "PRICE_SPIKE": "average price well above the trailing baseline",
    "PRICE_CUT_UNANSWERED": "price was cut but units did not rise",
    "MARGIN_SQUEEZE": "margin fell below its baseline and is thin in absolute terms",
    "STOCKOUT_RISK": "inventory cover is below one and a half days of baseline demand",
    "PROMO_INEFFECTIVE": "most stores are promoting but units did not lift",
}

#: Category-scope patterns, listed so the contract and the benchmark can name what
#: a product state cannot answer.
CATEGORY_SCOPE_PATTERNS: tuple[str, ...] = ("CATEGORY_DRIFT", "PRIVATE_LABEL_GAIN")

#: Checkpoint names ``laya-serve`` honours in a request's ``model`` field. It
#: silently ignores any other value and lets the Router auto-select, so the API
#: validates against this list instead of forwarding an unknown name and reporting
#: a checkpoint that never answered.
KNOWN_CHECKPOINTS: tuple[str, ...] = ("english", "multilingual", "typed-decisions")

#: Ordinal rubric for the ``score`` primitive, index = level.
SEVERITY_RUBRIC: list[str] = ["none", "info", "warn", "critical"]

QUESTION_PATTERN = "pattern"
QUESTION_SEVERITY = "severity"
QUESTION_REORDER = "reorder_now"

#: The three primitives, as ``laya-serve`` names them in a question's ``type``.
QUESTION_TYPES: tuple[str, ...] = ("choice", "score", "noul")

#: Product-scope patterns only. CATEGORY_DRIFT and PRIVATE_LABEL_GAIN are
#: category-scope rules: a single product cannot carry their label, so the
#: benchmark treats them as unavailable for a product state rather than scoring
#: them as ``none``, which would be a false negative.
PRODUCT_SCOPE_PATTERNS: tuple[str, ...] = (
    "DEMAND_SURGE",
    "DEMAND_COLLAPSE",
    "PRICE_SPIKE",
    "PRICE_CUT_UNANSWERED",
    "MARGIN_SQUEEZE",
    "STOCKOUT_RISK",
    "PROMO_INEFFECTIVE",
)

#: Severity order, used to map a rule severity onto the rubric index.


def build_questions() -> dict[str, Any]:
    """The three typed questions. One per Laya primitive.

    Returned fresh each call so a caller cannot mutate the shared schema.
    """
    return {
        QUESTION_PATTERN: {
            "type": "choice",
            "instructions": (
                "Which single decision pattern best describes this grocery "
                "product-day, compared with its own trailing baseline?"
            ),
            "criteria": dict(PATTERN_OPTIONS),
        },
        QUESTION_SEVERITY: {
            "type": "score",
            "instructions": "How severe is the situation for this product-day?",
            "criteria": list(SEVERITY_RUBRIC),
        },
        QUESTION_REORDER: {
            "type": "noul",
            "instructions": (
                "Should replenishment be raised for this product today to avoid "
                "running out within the next day?"
            ),
        },
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_STATE_SQL = """
SELECT
    product_id,
    name,
    category,
    brand,
    is_private_label,
    is_perishable
FROM products
WHERE product_id = ANY(%(ids)s)
"""


@dataclass(frozen=True)
class DecisionState:
    """One product-day, reduced to the numbers a decision needs."""

    product_id: int
    day: date
    name: str
    category: str
    brand: str
    is_private_label: bool
    is_perishable: bool
    units: int
    units_baseline: float | None
    avg_price: float
    price_baseline: float | None
    margin_pct: float
    margin_pct_baseline: float | None
    inventory: int
    inventory_cover_days: float | None
    store_count: int
    promo_stores: int
    observations: int

    @property
    def price_lift(self) -> float | None:
        if not self.price_baseline:
            return None
        return self.avg_price / self.price_baseline

    @property
    def unit_lift(self) -> float | None:
        if not self.units_baseline:
            return None
        return self.units / self.units_baseline


def _margin_pct(revenue: float, cogs: float) -> float:
    return ((revenue - cogs) / revenue * 100.0) if revenue > 0 else 0.0


async def states_for(
    cur: Any, day: date, product_ids: Sequence[int]
) -> dict[int, DecisionState]:
    """Assemble decision states for ``product_ids`` on ``day``.

    Reuses the rule engine's own rollup and baseline queries so the two decision
    layers are guaranteed to be looking at identical numbers. Any divergence here
    would silently invalidate the benchmark.
    """
    ids = list(dict.fromkeys(int(p) for p in product_ids))
    if not ids:
        return {}

    await cur.execute(_STATE_SQL, {"ids": ids})
    meta = {int(r["product_id"]): r for r in await cur.fetchall()}

    today = await live_product_day(cur, day, ids)
    baselines = await product_baselines(cur, ids, day, BASELINE_DAYS)

    states: dict[int, DecisionState] = {}
    for pid in ids:
        row = meta.get(pid)
        roll = today.get(pid)
        if row is None or roll is None:
            # A product with no facts on that day has no state to decide about.
            continue
        base = baselines.get(pid)
        med_units = base.med_units if base else None
        observation_count = base.observations if base else 0
        cover: float | None = None
        if med_units and med_units > 0:
            cover = roll.inventory / med_units
        states[pid] = DecisionState(
            product_id=pid,
            day=roll.day,
            name=str(row["name"]),
            category=str(row["category"]),
            brand=str(row["brand"]),
            is_private_label=bool(row["is_private_label"]),
            is_perishable=bool(row["is_perishable"]),
            units=roll.units,
            units_baseline=med_units,
            avg_price=roll.avg_price,
            price_baseline=base.med_avg_price if base else None,
            margin_pct=_margin_pct(roll.revenue, roll.cogs),
            margin_pct_baseline=(base.med_margin * 100.0) if base else None,
            inventory=roll.inventory,
            inventory_cover_days=cover,
            store_count=roll.store_count,
            promo_stores=roll.promo_stores,
            observations=observation_count,
        )
    return states


def _num(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    if value is None:
        return "unknown"
    return f"{value:{spec}}{suffix}"


def render_state(state: DecisionState) -> str:
    """One compact line. Stable key order, no JSON punctuation.

    Every clause is a number the decision actually turns on. The model reads 512
    tokens in the English checkpoint and someone else's prose is not a feature.
    """
    return (
        f"Grocery product-day. Product: {state.name}. Category: {state.category}. "
        f"Brand: {state.brand}. Private label: {state.is_private_label}. "
        f"Perishable: {state.is_perishable}. "
        f"Units today: {state.units} vs baseline median {_num(state.units_baseline)}. "
        f"Average price today: {_num(state.avg_price)} vs baseline "
        f"{_num(state.price_baseline)}. "
        f"Margin today: {_num(state.margin_pct, '.1f', '%')} vs baseline "
        f"{_num(state.margin_pct_baseline, '.1f', '%')}. "
        f"Inventory cover: {_num(state.inventory_cover_days, '.2f')} days. "
        f"Stores carrying: {state.store_count}, of which promoting: {state.promo_stores}. "
        f"Baseline computed from {state.observations} days."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """``float`` for a real number, else ``None``.

    ``bool`` is rejected: it is an ``int`` subclass, and ``True`` silently becoming
    ``1.0`` would report a nonsense confidence. Every field this feeds is declared
    ``float | None`` on the response models, so a stray string from upstream must
    become ``None`` here rather than a 5xx at serialization time.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_answer(
    kind: str,
    raw: Any,
    criteria: list[str] | None = None,
) -> dict[str, Any]:
    """Flatten one ``answers[name]`` entry into a stable shape.

    Written against the response actually returned by ``laya-serve`` 0.3.20, which
    was observed rather than assumed::

        choice -> {"choice": "STOCKOUT_RISK", "probabilities": {opt: p},
                   "confidence": 0.0585, "answer_confidence": 0.1869, ...}
        score  -> {"score": 0.7311, "legend": {"0": "none", ...},
                   "probabilities": {"0": 0.451, ...}, "answer_confidence": 0.451}
        noul   -> {"noul": 0.1558, "confidence": 0.8442, "answer_confidence": 0.8442}

    Two details matter downstream and are easy to get wrong:

    * ``answer_confidence`` is the probability of the answer the model chose, and
      it is the field worth calibrating on. ``confidence`` is a different,
      smaller quantity for ``choice`` and ``score`` (0.0585 against 0.1869 on the
      same answer), so using it for calibration would understate confidence badly.
    * ``score`` returns a continuous position on the rubric, not a level index.
      The decision is the argmax of the distribution, and ``legend`` maps that
      index back to the level name.
    """
    if not isinstance(raw, dict):
        return {"answer": raw, "confidence": None, "probabilities": None, "legend": None, "raw": raw}

    probabilities: dict[str, float] | None = None
    for key in ("probabilities", "probs", "distribution"):
        candidate = raw.get(key)
        if isinstance(candidate, dict) and candidate:
            values = {str(k): _as_float(v) for k, v in candidate.items()}
            probabilities = {k: v for k, v in values.items() if v is not None}
            break
        if isinstance(candidate, list) and candidate:
            probabilities = {
                str(i): value
                for i, v in enumerate(candidate)
                if (value := _as_float(v)) is not None
            }
            break
    if not probabilities:
        probabilities = None

    legend_raw = raw.get("legend")
    legend = {str(k): str(v) for k, v in legend_raw.items()} if isinstance(legend_raw, dict) else None

    confidence = _as_float(raw.get("answer_confidence"))
    if confidence is None:
        confidence = _as_float(raw.get("confidence"))

    answer = raw.get(kind)
    if answer is None:
        for key in ("answer", "value", "result", "choice", "score", "noul"):
            if key in raw:
                answer = raw[key]
                break

    decided_index: int | None = None
    level: str | None = None
    if probabilities:
        winner = max(probabilities, key=lambda k: probabilities[k])
        decided_index = _as_int(winner)

    if kind == "score":
        if decided_index is not None:
            level = (legend or {}).get(str(decided_index))
            if level is None and criteria and 0 <= decided_index < len(criteria):
                # A dynamically declared rubric: the caller's own level names are
                # the legend when upstream omitted one.
                level = criteria[decided_index]
            if level is None and 0 <= decided_index < len(SEVERITY_RUBRIC):
                level = SEVERITY_RUBRIC[decided_index]
        # The rubric level is the decision; the continuous `score` is a position
        # on that rubric and is kept alongside for the UI.
        answer = level if level is not None else answer
    elif kind == "noul":
        answer = raw.get("noul", answer)
    elif kind == "choice" and answer is None and probabilities:
        # A choice whose label came back under an unexpected key: the winning
        # probability's key *is* the option name for a choice question.
        answer = max(probabilities, key=lambda k: probabilities[k])

    return {
        "answer": answer,
        "level_index": decided_index,
        "confidence": confidence,
        "probabilities": probabilities,
        "legend": legend,
        "score_position": _as_float(raw.get("score")) if kind == "score" else None,
        "raw": raw,
    }


class LayaClient:
    """HTTP client for the ``laya`` container.

    Talks to ``laya-serve``, which exposes the same ``POST /v1/systemone`` shape
    as TypeSafe Jev. Swapping the base URL is the whole migration.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.laya_url).rstrip("/")
        self.timeout = timeout or settings.laya_timeout
        self.enabled = settings.laya_enabled
        self.checkpoint = settings.laya_checkpoint

    async def predict(
        self,
        state_text: str,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Run every question against one state in a single forward pass.

        ``model`` pins the checkpoint via the request-level ``model`` field, which is
        the only way to do it through ``laya-serve``. It honours the field only when
        the value names a Laya checkpoint and otherwise silently falls back to the
        Router's own choice, so callers must verify ``routing.model`` on the response
        rather than trust the request.

        Never raises: a decision aid that can take the dashboard down is worse
        than no decision aid.
        """
        questions = questions or build_questions()
        unavailable = {
            "available": False,
            "answers": {},
            "routing": None,
            "latency_ms": None,
        }
        if not self.enabled:
            return {**unavailable, "detail": "LAYA_ENABLED=0"}

        payload: dict[str, Any] = {"state": state_text, "questions": questions}
        requested_model = (model or self.checkpoint or "").strip()
        if requested_model:
            payload["model"] = requested_model
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/v1/systemone", json=payload)
            latency_ms = (time.monotonic() - started) * 1000.0
            if response.status_code >= 400:
                detail = response.text[:400]
                log.warning("laya returned %s: %s", response.status_code, detail)
                return {**unavailable, "latency_ms": latency_ms, "detail": detail}
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("laya unreachable at %s: %s", self.base_url, exc)
            return {**unavailable, "detail": f"{type(exc).__name__}: {exc}"}

        raw_answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(raw_answers, dict):
            return {
                **unavailable,
                "latency_ms": latency_ms,
                "detail": "response had no `answers` object",
                "raw": body,
            }

        answers = {
            name: normalize_answer(kind_for(name, questions), value, criteria_for(name, questions))
            for name, value in raw_answers.items()
        }
        routing = body.get("routing")
        if not isinstance(routing, dict):
            # Upstream has been seen to return a bare model name here. The routing
            # block is advisory metadata, so an unexpected shape becomes a detail
            # rather than a serialization failure on an otherwise good answer.
            routing = {"model": str(routing)} if isinstance(routing, str) else None
        return {
            "available": True,
            "answers": answers,
            "routing": routing,
            "latency_ms": _as_float(latency_ms),
            "raw": body if answers == {} else None,
        }

    async def health(self) -> dict[str, Any]:
        """Probe the model server. `laya-serve` documents GET /health, unauthenticated."""
        if not self.enabled:
            return {"available": False, "detail": "LAYA_ENABLED=0"}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout, 15.0)) as client:
                response = await client.get(f"{self.base_url}/health")
            elapsed_ms = (time.monotonic() - started) * 1000.0
            body: Any
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = response.text[:200]
            return {
                "available": response.status_code < 400,
                "status_code": response.status_code,
                "probe_ms": round(elapsed_ms, 1),
                "base_url": self.base_url,
                "detail": body,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "base_url": self.base_url,
                "detail": f"{type(exc).__name__}: {exc}",
            }


def kind_for(question_name: str, questions: dict[str, Any] | None = None) -> str:
    """Which primitive a question belongs to.

    A **declared type wins**: callers that pass an arbitrary question set (the
    free-text playground) name their own questions, so normalizing by name alone
    would silently flatten every unknown question into ``choice`` and drop the
    rubric level of a ``score`` or the probability of a ``noul``. Only when the
    question set does not declare a usable type do we fall back to the three frozen
    question names, and then to ``choice``.
    """
    declared = ((questions or {}).get(question_name) or {}).get("type")
    if isinstance(declared, str) and declared in QUESTION_TYPES:
        return declared
    if question_name == QUESTION_PATTERN:
        return "choice"
    if question_name == QUESTION_SEVERITY:
        return "score"
    if question_name == QUESTION_REORDER:
        return "noul"
    # Unknown question with no declared type: guess from the name so an added
    # question still renders.
    return "choice"


def criteria_for(question_name: str, questions: dict[str, Any] | None = None) -> list[str] | None:
    """Ordered option names of a question, when it has any.

    ``choice`` criteria are a mapping (option -> description) and ``score``
    criteria are already an ordered list of rubric levels. The order is what makes
    an index from a missing ``legend`` recoverable, so it is preserved.
    """
    criteria = ((questions or {}).get(question_name) or {}).get("criteria")
    if isinstance(criteria, dict):
        return [str(key) for key in criteria]
    if isinstance(criteria, list):
        return [str(item) for item in criteria]
    return None


laya = LayaClient()
