"""Pydantic response models.

The API shapes documented in ``README.md`` use snake_case, no extra
fields. ``extra="forbid"`` is set on request bodies so malformed client payloads
fail loudly instead of being silently ignored.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# health / meta
# ---------------------------------------------------------------------------


class HealthOut(BaseModel):
    status: str
    db: bool
    rollup_lag_seconds: float | None
    dataset: dict[str, Any]


class DatasetFacts(BaseModel):
    facts: int
    products: int
    stores: int
    day_min: date | None = None
    day_max: date | None = None
    seed: int | None = None


class CategoryCount(BaseModel):
    category: str
    products: int


class MetaOut(BaseModel):
    dataset: DatasetFacts
    categories: list[CategoryCount]
    brands: list[str]
    formats: list[str]
    regions: list[str]


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------


class ProductListItem(BaseModel):
    product_id: int
    sku: str
    name: str
    brand: str
    category: str
    subcategory: str
    uom: str
    pack_size: float
    is_private_label: bool
    is_perishable: bool
    list_price: float
    latest_day: date | None = None
    latest_units: int | None = None
    latest_price: float | None = None
    latest_margin_pct: float | None = None


class ProductPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ProductListItem]


class SeriesPoint(BaseModel):
    day: date
    units: int
    avg_price: float
    revenue: float
    margin_pct: float
    inventory: int
    promo_stores: int
    store_count: int


class ProductSeriesOut(BaseModel):
    product: ProductListItem
    series: list[SeriesPoint]


class CategorySeriesPoint(BaseModel):
    day: date
    units: int
    revenue: float
    cogs: float
    margin_pct: float
    pl_units: int
    pl_share: float
    product_count: int


class CategorySeriesOut(BaseModel):
    category: str
    series: list[CategorySeriesPoint]


# ---------------------------------------------------------------------------
# fact-grain search
# ---------------------------------------------------------------------------


class SearchItem(BaseModel):
    day: date
    store_id: int
    store_name: str
    region: str
    format: str
    product_id: int
    sku: str
    product: str
    brand: str
    category: str
    price: float
    unit_cost: float
    units_sold: int
    revenue: float
    margin_pct: float
    promo_flag: bool
    inventory: int
    on_order: int


class SearchTotals(BaseModel):
    revenue: float
    units: int
    margin_pct: float
    rows: int


class SearchPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SearchItem]
    totals: SearchTotals


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


class DayPoint(BaseModel):
    day: date
    units: int
    revenue: float
    cogs: float
    margin_pct: float
    pl_units: int
    pl_share: float


class CategorySummary(BaseModel):
    category: str
    units: int
    revenue: float
    margin_pct: float
    pl_share: float
    units_dod_pct: float | None = None


class Mover(BaseModel):
    product_id: int
    name: str
    category: str
    units: int
    baseline: float
    lift: float
    revenue: float


class SignalCounts(BaseModel):
    critical: int = 0
    warn: int = 0
    info: int = 0


class OverviewOut(BaseModel):
    days: list[DayPoint]
    categories: list[CategorySummary]
    movers: list[Mover]
    signal_counts: SignalCounts
    as_of: str
    rollups_as_of: str | None = None


# ---------------------------------------------------------------------------
# patterns / signals
# ---------------------------------------------------------------------------


class SeverityBand(BaseModel):
    severity: str
    when: str


class PatternOut(BaseModel):
    id: str
    label: str
    scope: str
    description: str
    thresholds: dict[str, float]
    severity_bands: list[SeverityBand]
    action_template: str
    baseline_days: int
    min_obs: int


class PatternsOut(BaseModel):
    patterns: list[PatternOut]


class SignalOut(BaseModel):
    signal_id: int
    fired_at: str
    day: date
    pattern: str
    severity: str
    subject_type: str
    subject_id: int
    subject_label: str
    score: float
    evidence: dict[str, Any]
    action: str


class SignalsOut(BaseModel):
    items: list[SignalOut]
    counts: SignalCounts
    total: int


class SeenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_ids: list[int] = Field(default_factory=list)


class SeenOut(BaseModel):
    updated: int


class ScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date | None = None
    product_ids: list[int] | None = None
    categories: list[str] | None = None
    persist: bool = True


class ScanOut(BaseModel):
    scanned: int
    signals: list[SignalOut]


# ---------------------------------------------------------------------------
# Laya results — shared by /api/decide, /api/explore/interpret, /api/laya/playground
# ---------------------------------------------------------------------------
#
# Matched field-for-field by ``web/src/lib/types.ts``. ``answer`` is ``Any``
# because upstream may return an option name, rubric level, or probability.
# Rejecting an unexpected JSON type with a 5xx would take down an advisory panel.


class LayaAnswerOut(BaseModel):
    answer: Any = None
    level_index: int | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    legend: dict[str, str] | None = None
    score_position: float | None = None


class LayaRoutingOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    repo: str | None = None
    reason: str | None = None


class LayaResultOut(BaseModel):
    available: bool
    answers: dict[str, LayaAnswerOut] = Field(default_factory=dict)
    routing: LayaRoutingOut | None = None
    latency_ms: float | None = None
    #: Present when ``available`` is false: why the model could not answer.
    detail: str | None = None


# ---------------------------------------------------------------------------
# explore (free text -> deterministic retrieval, plus advisory Laya)
# ---------------------------------------------------------------------------

EXPLORE_QUERY_MAX_CHARS = 200
EXPLORE_DEFAULT_LIMIT = 20
EXPLORE_MAX_LIMIT = 100


class ExploreInterpretation(BaseModel):
    intent: str
    product_term: str | None = None
    #: ``explicit`` | ``default`` | ``laya``
    source: str


class ExploreOut(BaseModel):
    query: str
    day: str
    interpretation: ExploreInterpretation
    page: SearchPage


class ExploreInterpretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=EXPLORE_QUERY_MAX_CHARS)
    limit: int = Field(default=EXPLORE_DEFAULT_LIMIT, ge=1, le=EXPLORE_MAX_LIMIT)

    @model_validator(mode="after")
    def _check_query(self) -> "ExploreInterpretIn":
        # A whitespace-only box has nothing to interpret, and this endpoint costs a
        # multi-second forward pass. Reject it here rather than spend the model call
        # on text the extractor will strip to nothing.
        if not self.query.strip():
            raise ValueError("query must contain at least one non-whitespace character")
        return self


class ExploreInterpretOut(BaseModel):
    query: str
    day: str
    interpretation: ExploreInterpretation
    page: SearchPage
    #: The slower, advisory pass. Never trusted over an explicit phrase.
    laya: LayaResultOut


# ---------------------------------------------------------------------------
# Laya playground (free text state + caller-defined typed questions)
# ---------------------------------------------------------------------------

PLAYGROUND_STATE_MAX_CHARS = 4000
PLAYGROUND_MAX_QUESTIONS = 8
PLAYGROUND_MAX_OPTIONS = 20
PLAYGROUND_INSTRUCTIONS_MAX_CHARS = 500
QUESTION_NAME_MAX_CHARS = 64


class PlaygroundQuestion(BaseModel):
    """One caller-defined typed question: ``choice``, ``score`` or ``noul``."""

    model_config = ConfigDict(extra="forbid")

    type: str
    instructions: str = Field(default="", max_length=PLAYGROUND_INSTRUCTIONS_MAX_CHARS)
    #: ``choice`` -> option name to description; ``score`` -> ordered rubric levels.
    criteria: dict[str, str] | list[str] | None = None


class PlaygroundIn(BaseModel):
    """The playground request, validated to the same bounds the endpoint enforces.

    The question schema is checked here rather than in the route so a malformed
    payload is a 422 with a precise message, and so the *declared* ``type`` is
    guaranteed present when :func:`app.laya.normalize_answer` reads it: an unknown
    question name normalizes by its type, never by falling back to ``choice``.
    """

    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=PLAYGROUND_STATE_MAX_CHARS)
    questions: dict[str, PlaygroundQuestion] = Field(
        min_length=1, max_length=PLAYGROUND_MAX_QUESTIONS
    )
    model: str | None = None

    @model_validator(mode="after")
    def _check_questions(self) -> "PlaygroundIn":
        for name, question in self.questions.items():
            if not name.strip():
                raise ValueError("question names must not be blank")
            if len(name) > QUESTION_NAME_MAX_CHARS:
                raise ValueError(
                    f"question name {name!r} is longer than {QUESTION_NAME_MAX_CHARS} characters"
                )
            if question.type not in ("choice", "score", "noul"):
                raise ValueError(
                    f"question {name!r}: unknown type {question.type!r}; "
                    "expected one of ['choice', 'score', 'noul']"
                )
            if question.type == "choice":
                if not isinstance(question.criteria, dict) or not question.criteria:
                    raise ValueError(
                        f"question {name!r}: a choice question needs a non-empty "
                        "criteria object of option name -> description"
                    )
                if len(question.criteria) > PLAYGROUND_MAX_OPTIONS:
                    raise ValueError(
                        f"question {name!r}: {len(question.criteria)} options exceeds the "
                        f"{PLAYGROUND_MAX_OPTIONS}-option ceiling"
                    )
            elif question.type == "score":
                if not isinstance(question.criteria, list) or len(question.criteria) < 2:
                    raise ValueError(
                        f"question {name!r}: a score question needs an ordered criteria "
                        "list of at least two rubric levels"
                    )
                if len(question.criteria) > PLAYGROUND_MAX_OPTIONS:
                    raise ValueError(
                        f"question {name!r}: {len(question.criteria)} levels exceeds the "
                        f"{PLAYGROUND_MAX_OPTIONS}-level ceiling"
                    )
            elif question.criteria is not None:
                raise ValueError(f"question {name!r}: a noul question takes no criteria")
        return self


class PlaygroundOut(BaseModel):
    state: str
    questions: dict[str, PlaygroundQuestion]
    laya: LayaResultOut
