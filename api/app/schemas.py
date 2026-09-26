"""Pydantic response models.

Every shape here mirrors ``docs/CONTRACT.md`` §5 exactly: snake_case, no extra
fields. ``extra="forbid"`` is set on request bodies so malformed client payloads
fail loudly instead of being silently ignored.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
