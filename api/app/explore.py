"""Free-text explore: deterministic intent extraction over the newest day.

Two decisions worth knowing about:

* **Retrieval is deterministic SQL and never touches the model.** A free-text query
  is parsed by :func:`interpret` — an ordered phrase list plus a qualifier/metric
  token pass — into one of the seven supported intents and an optional product
  term. The term is resolved against ``products`` by the same lexical-then-trigram
  ladder ``/api/products`` uses (name ILIKE and tsvector, then ``similarity``, then
  ``word_similarity``), capped at :data:`CANDIDATE_PRODUCT_CAP` products, and the
  rows come from ``market_facts`` for the **newest day only**, ordered by the
  intent's own allowlisted ORDER BY fragment. No user value ever reaches a statement
  as anything but a bound parameter.
* **The model is advisory and slower.** ``POST /api/explore/interpret`` runs the
  same deterministic parse *and* one Laya ``choice`` forward pass over the raw
  query. An explicit phrase always wins (``source: "explicit"``); Laya supplies the
  intent only when the text carries no explicit phrase (``source: "laya"``), and
  ``browse`` is the fallback when neither is available (``source: "default"``). A
  Laya answer is only accepted when it names one of the seven supported intents, so
  a hallucinated option degrades to ``default`` rather than to an unsupported
  ORDER BY.

Extraction precedence is fixed and documented rather than incidental:
:data:`EXPLICIT_INTENT_PRIORITY` lists the intents in the order they are tested, and
the first phrase hit wins. ``"milk with low inventory"`` therefore resolves to
``low_inventory`` with product term ``milk``, and the page is milk rows for the
newest day ordered ``inventory ASC``.

A product term that matches no catalog product yields an **empty** page. Falling
back to the unfiltered newest day would silently answer a different question.

``promo`` is the one intent that filters as well as ranks: "on promo" asks for the
promoted rows, so ``total`` counts promoted rows only. Every other intent ranks the
whole newest day and leaves the row set alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import APIRouter, Query

from .db import pool
from .laya import laya
from .schemas import (
    EXPLORE_DEFAULT_LIMIT,
    EXPLORE_MAX_LIMIT,
    EXPLORE_QUERY_MAX_CHARS,
    ExploreInterpretIn,
    ExploreInterpretOut,
    ExploreOut,
)
from .search import (
    LIKE_ESCAPE,
    TRIGRAM_THRESHOLD,
    WORD_TRIGRAM_THRESHOLD,
    fact_page,
    like_pattern,
    tsquery_is_empty,
)

log = logging.getLogger("laya.api.explore")

router = APIRouter()

#: How many products one product term may pull in. The term is matched against the
#: 2.4k-row catalog, and the winning ids are capped so a broad term ("milk") cannot
#: turn the fact query into a full-day scan. This is a retrieval bound, not a
#: contract bound, so it lives here rather than in the schemas.
CANDIDATE_PRODUCT_CAP = 200

#: Product-term shape: at most this many tokens, this many characters.
TERM_MAX_TOKENS = 6
TERM_MAX_CHARS = 64

BROWSE = "browse"
LOW_INVENTORY = "low_inventory"
HIGH_SALES = "high_sales"
LOW_SALES = "low_sales"
PROMO = "promo"
HIGH_PRICE = "high_price"
LOW_PRICE = "low_price"

SUPPORTED_INTENTS: tuple[str, ...] = (
    BROWSE,
    LOW_INVENTORY,
    HIGH_SALES,
    LOW_SALES,
    PROMO,
    HIGH_PRICE,
    LOW_PRICE,
)

SOURCE_EXPLICIT = "explicit"
SOURCE_DEFAULT = "default"
SOURCE_MODEL = "laya"

#: Human descriptions, also served to Laya as the ``choice`` criteria. One entry
#: per supported intent, and the option names are the intent names themselves, so
#: the model's answer is directly usable as an intent.
INTENT_OPTIONS: dict[str, str] = {
    BROWSE: "no metric preference, just show the newest day's rows for the product as-is",
    LOW_INVENTORY: "inventory on hand is low, rows ranked by inventory ascending",
    HIGH_SALES: "units sold are high, rows ranked by units sold descending",
    LOW_SALES: "units sold are low, rows ranked by units sold ascending",
    PROMO: "the product is on promotion or discounted",
    HIGH_PRICE: "the price is high or expensive",
    LOW_PRICE: "the price is low or cheap",
}

#: Allowlisted ORDER BY fragments. ``revenue`` and ``margin_pct`` are *aliases* in
#: ``_FACT_SELECT``, not columns of ``market_facts``, so every fragment spells the
#: underlying expression out. ``day`` is pinned by a WHERE clause and is only a
#: tiebreaker here; ``store_id``/``product_id`` make the order total.
INTENT_ORDER: dict[str, str] = {
    BROWSE: "(f.units_sold * f.price) DESC, f.day DESC, f.store_id ASC, f.product_id ASC",
    LOW_INVENTORY: "f.inventory ASC, f.day DESC, f.store_id ASC, f.product_id ASC",
    HIGH_SALES: "f.units_sold DESC, f.day DESC, f.store_id ASC, f.product_id ASC",
    LOW_SALES: "f.units_sold ASC, f.day DESC, f.store_id ASC, f.product_id ASC",
    PROMO: "f.promo_flag DESC, f.units_sold DESC, f.day DESC, f.store_id ASC, f.product_id ASC",
    HIGH_PRICE: "f.price DESC, f.day DESC, f.store_id ASC, f.product_id ASC",
    LOW_PRICE: "f.price ASC, f.day DESC, f.store_id ASC, f.product_id ASC",
}

#: First hit wins. ``low_inventory`` outranks ``low_price`` so "cheap milk with low
#: inventory" is a stock question, not a price question.
EXPLICIT_INTENT_PRIORITY: tuple[str, ...] = (
    LOW_INVENTORY,
    HIGH_SALES,
    LOW_SALES,
    PROMO,
    HIGH_PRICE,
    LOW_PRICE,
    BROWSE,
)

#: Contiguous phrases, per intent, tested in :data:`EXPLICIT_INTENT_PRIORITY` order.
_PHRASES: dict[str, tuple[str, ...]] = {
    LOW_INVENTORY: (
        r"low\s+(?:inventory|stock|stock\s+levels?)",
        r"(?:inventory|stock)\s+(?:is\s+|are\s+|levels?\s+)?low",
        r"running\s+(?:out|low|short)",
        r"out\s+of\s+stock",
        r"stock[-\s]?outs?",
        r"understocked",
        r"(?:below\s+(?:the\s+)?)?safety\s+stock",
        r"(?:stok|persediaan)(?:nya)?\s+(?:menipis|rendah|sedikit|habis)",
        r"(?:stok|persediaan)(?:nya)?\s+(?:yang\s+)?(?:paling\s+)?(?:sedikit|rendah)",
        r"(?:sedikit|rendah)\s+(?:stok|persediaan)",
        r"reorder\s+soon",
    ),
    HIGH_SALES: (
        r"high\s+(?:sales|demand|units|volume)",
        r"(?:sales|units|demand|volume)\s+(?:are\s+|is\s+)?high",
        # The optional plural suffix must be inside the group: matching only
        # ``seller`` in "best sellers" would strip the phrase and leave "s" behind
        # as the product term.
        r"best[-\s]?sell(?:er|ers|ing)?s?",
        r"top[-\s]?sell(?:er|ers|ing)?s?",
        r"selling\s+well",
        r"most\s+sold",
        r"fast[-\s]?mov(?:er|ers|ing)s?",
        r"penjualan\s+(?:tinggi|tertinggi|terbanyak)",
        r"(?:produk\s+)?(?:paling\s+)?laris",
    ),
    LOW_SALES: (
        r"low\s+(?:sales|demand|units|volume)",
        r"(?:sales|units|demand|volume)\s+(?:are\s+|is\s+)?low",
        r"worst[-\s]?sell(?:er|ers|ing)?s?",
        r"slow[-\s]?mov(?:er|ers|ing)s?",
        r"not\s+selling",
        r"poor\s+sales",
        r"least\s+sold",
        r"penjualan\s+(?:rendah|terendah)",
        r"(?:produk\s+)?kurang\s+laris",
    ),
    PROMO: (
        r"on\s+promo(?:tion)?",
        r"promo(?:tion)?s?\b",
        r"discount(?:ed|s)?",
        r"on\s+(?:deal|offer)s?",
        r"special\s+offers?",
        r"markdowns?",
        r"diskon",
        r"promosi",
    ),
    HIGH_PRICE: (
        r"high\s+prices?",
        r"prices?\s+(?:are\s+|is\s+)?high",
        r"most\s+expensive",
        # ``expensive`` alone must not swallow its own negations or its in-word
        # sibling: "least expensive", "not expensive" and "inexpensive" are all
        # price-*down* questions, and the lookbehinds leave them to LOW_PRICE.
        r"(?<!\bleast\s)(?<!\bless\s)(?<!\bnot\s)(?<!\bin)expensive",
        r"pricey",
        r"premium\s+priced?",
        r"harga\s+(?:tinggi|tertinggi|mahal)",
        r"(?:paling\s+)?mahal",
    ),
    LOW_PRICE: (
        r"low\s+prices?",
        r"prices?\s+(?:are\s+|is\s+)?low",
        r"least\s+expensive",
        r"less\s+expensive",
        r"not\s+(?:too\s+)?expensive",
        # ``cheap`` must eat its own comparative/superlative, or stripping the
        # phrase leaves the fragment ("cheaper" -> "er") in the product term.
        r"cheap(?:er|est)?",
        r"budget",
        r"inexpensive",
        r"price\s+cuts?",
        r"harga\s+(?:rendah|terendah|murah)",
        r"(?:paling\s+)?murah",
    ),
    BROWSE: (
        r"browse",
        r"everything",
        r"all\s+products?",
        r"show\s+all",
        r"list\s+all",
        r"overview",
        r"anything",
        r"semua\s+produk",
        r"tampilkan\s+semua",
    ),
}

_PHRASE_RES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (
        intent,
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in _PHRASES[intent]),
    )
    for intent in EXPLICIT_INTENT_PRIORITY
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")

#: Metric nouns that say nothing about *which* product, only which number matters.
_SALES_TOKENS = frozenset({"sales", "sale", "units", "demand", "volume", "sold", "sells", "penjualan", "terjual"})
_PRICE_TOKENS = frozenset({"price", "prices", "pricing", "harga"})
_PROMO_TOKENS = frozenset(
    {"promo", "promotion", "promotions", "discount", "discounts", "deal", "deals", "offer", "offers", "markdown", "promosi", "diskon"}
)
_STOCK_TOKENS = frozenset({"inventory", "stock", "stockout", "stockouts", "understock", "stok", "persediaan"})

_HIGH_QUALIFIERS = frozenset(
    {"high", "higher", "highest", "most", "best", "top", "expensive", "pricey", "above", "surge", "spike", "tinggi", "tertinggi", "banyak", "terbanyak", "mahal"}
)
_LOW_QUALIFIERS = frozenset(
    {"low", "lower", "lowest", "least", "worst", "cheap", "cheapest", "budget", "below", "slow", "rendah", "terendah", "sedikit", "tersedikit", "murah", "termurah", "menipis"}
)

_METRIC_TOKENS = _SALES_TOKENS | _PRICE_TOKENS | _PROMO_TOKENS | _STOCK_TOKENS

#: Glue words a shopper types around the product name. Deliberately small: a token
#: that could plausibly be part of a product name ("fresh", "whole", "high" in
#: "high protein") stays in the term.
_STOPWORDS = frozenset(
    {
        "a", "an", "all", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can",
        "could", "did", "do", "does", "find", "for", "from", "get", "give", "has", "have",
        "having", "here", "i", "in", "is", "it", "its", "item", "items", "just", "levels",
        "list", "me", "more", "my", "no", "not", "of", "on", "only", "or", "order", "ordered",
        "please", "product", "products", "row", "rows", "shelf", "shelves", "show", "sku", "skus",
        "some", "sort", "sorted", "that", "the", "their", "them", "these", "those", "to", "under",
        "up", "us", "we", "what", "when", "where", "which", "who", "with", "would", "you", "your",
        # Contractions a shopper actually types ("whats running low").
        "arent", "cant", "doesnt", "dont", "hows", "im", "isnt", "theres", "wanna",
        "whats", "wheres", "whos", "wont",
        "apa", "apakah", "berapa", "cari", "dalam", "dan", "dari", "di", "ini", "itu",
        "mana", "memiliki", "pada", "paling", "produk", "saja", "saya", "tampilkan",
        "tersebut", "untuk", "yang", "dengan", "toko", "ada", "masih", "butuh",
    }
)

# Catalog names/categories are stored in English. Translate common shopper terms
# before lexical matching; unknown words remain untouched and never widen a query.
_CATALOG_TERMS = {
    "susu": "milk",
    "roti": "bread",
    "kopi": "coffee",
    "sayur": "produce",
    "sayuran": "produce",
    "buah": "produce",
    "camilan": "snacks",
}


@dataclass(frozen=True)
class Interpretation:
    """The deterministic parse of one free-text query."""

    intent: str
    product_term: str | None
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "product_term": self.product_term,
            "source": self.source,
        }


def _phrase_intent(text: str) -> str | None:
    for intent, patterns in _PHRASE_RES:
        if any(pattern.search(text) for pattern in patterns):
            return intent
    return None


_QUALIFIER_LOOKBACK = 3


def _qualifier_before(tokens: list[str], index: int) -> tuple[str, int] | None:
    """The nearest preceding qualifier for the metric token at ``index``.

    Proximity beats a query-wide scan: ``"high sales and low price milk"`` carries
    both qualifiers, and a global scan would make them cancel out. Looking back a
    few tokens attaches ``high`` to ``sales`` and ``low`` to ``price``. Returns the
    direction and its position, so the caller can drop the qualifier from the
    product term whether or not it resolved to an intent.
    """
    for back in range(1, _QUALIFIER_LOOKBACK + 1):
        position = index - back
        if position < 0:
            break
        token = tokens[position]
        if token in _HIGH_QUALIFIERS:
            return "high", position
        if token in _LOW_QUALIFIERS:
            return "low", position
    return None


def _token_intent(text: str) -> tuple[str | None, set[str]]:
    """Intent and consumed metric tokens for a query with no contiguous phrase.

    ``"high milk price"`` has no phrase but is a price question, so the qualifier
    and the metric noun are read from the token set instead. The consumed tokens
    are dropped from the product term.

    A qualifier that contradicts the only supported direction resolves to **no**
    intent rather than to its opposite: there is no ``high_inventory`` retrieval, so
    ``"milk with high inventory"`` is a browse, not a stock question. Answering with
    the reverse of what was asked would be worse than answering with the default.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    consumed: set[str] = set()
    intent: str | None = None
    for index, token in enumerate(tokens):
        if token not in _METRIC_TOKENS:
            continue
        consumed.add(token)
        found = _qualifier_before(tokens, index)
        qualifier = found[0] if found else None
        if found is not None:
            # A qualifier next to a metric noun is describing that metric, never the
            # product: "high milk price" is a price question about milk.
            consumed.add(tokens[found[1]])
        if token in _STOCK_TOKENS:
            # Inventory defaults to the low reading: a bare "stock" question is a
            # replenishment question, and that is the only stock retrieval there is.
            resolved: str | None = None if qualifier == "high" else LOW_INVENTORY
        elif token in _PROMO_TOKENS:
            resolved = PROMO
        elif token in _SALES_TOKENS:
            resolved = HIGH_SALES if qualifier == "high" else (LOW_SALES if qualifier == "low" else None)
        else:
            resolved = HIGH_PRICE if qualifier == "high" else (LOW_PRICE if qualifier == "low" else None)
        if resolved is not None and intent is None:
            intent = resolved
    return intent, consumed


def _product_term(text: str, consumed: set[str]) -> str | None:
    stripped = text
    for _, patterns in _PHRASE_RES:
        for pattern in patterns:
            stripped = pattern.sub(" ", stripped)
    tokens = [
        _CATALOG_TERMS.get(token, token)
        for token in _TOKEN_RE.findall(stripped.lower())
        if token not in _STOPWORDS and token not in _METRIC_TOKENS and token not in consumed
    ]
    term = " ".join(tokens[:TERM_MAX_TOKENS])[:TERM_MAX_CHARS].strip()
    return term or None


def interpret(query: str) -> Interpretation:
    """Parse ``query`` into ``(intent, product_term, source)``, deterministically."""
    text = (query or "").strip()
    if not text:
        return Interpretation(intent=BROWSE, product_term=None, source=SOURCE_DEFAULT)

    intent = _phrase_intent(text)
    if intent is not None:
        return Interpretation(intent=intent, product_term=_product_term(text, set()), source=SOURCE_EXPLICIT)

    intent, consumed = _token_intent(text)
    term = _product_term(text, consumed)
    if intent is not None:
        return Interpretation(intent=intent, product_term=term, source=SOURCE_EXPLICIT)
    return Interpretation(intent=BROWSE, product_term=term, source=SOURCE_DEFAULT)


def intent_questions() -> dict[str, Any]:
    """The one ``choice`` question the interpret endpoint asks, fresh each call."""
    return {
        "intent": {
            "type": "choice",
            "instructions": (
                "A shopper typed the free text below into a grocery datasheet search "
                "box. Which single retrieval intent does it ask for?"
            ),
            "criteria": dict(INTENT_OPTIONS),
        }
    }


def render_query_state(query: str, day: date | None, products: int) -> str:
    """One compact line for the intent question. Same style as ``render_state``."""
    return (
        f"Grocery datasheet free-text search. Newest day: {day.isoformat() if day else 'unknown'}. "
        f"Catalog size: {products} products across stores, one fact row per store and product. "
        f"Query: {query}"
    )


def _empty_page(limit: int) -> dict[str, Any]:
    return {
        "total": 0,
        "limit": limit,
        "offset": 0,
        "items": [],
        "totals": {"revenue": 0.0, "units": 0, "margin_pct": 0.0, "rows": 0},
    }


async def _context(cur: Any) -> tuple[date | None, int]:
    """Newest fact day and catalog size, in one round trip."""
    await cur.execute(
        "SELECT (SELECT max(day) FROM market_facts) AS day,"
        "       (SELECT count(*) FROM products)   AS products"
    )
    row = await cur.fetchone() or {}
    return row.get("day"), int(row.get("products") or 0)


async def _candidate_ids(cur: Any, term: str) -> list[int]:
    """Products matching ``term`` lexically, then by trigram similarity.

    Mirrors ``/api/products``: name ILIKE and the tsvector first, then ``pg_trgm``
    similarity, then ``word_similarity`` for a single word inside a long name. The
    ladder only advances when the previous rung matched **nothing**, so it can never
    widen a good match.

    Every rung is bounded: evaluated against the 2.4k-row catalog and capped at
    :data:`CANDIDATE_PRODUCT_CAP` best-matching products, so a broad term ("milk")
    cannot turn the fact query into a full-day scan. No rung matching means an empty
    page, never an unfiltered fallback.
    """
    # A category name is a dimension filter, not a fuzzy product name. Otherwise
    # "snacks" can match a brand while omitting part of the Snacks category.
    await cur.execute(
        "SELECT product_id FROM products WHERE lower(category) = lower(%s) ORDER BY product_id",
        (term,),
    )
    category_ids = [int(row["product_id"]) for row in await cur.fetchall()]
    if category_ids:
        return category_ids

    lexical = [f"p.name ILIKE %(like)s {LIKE_ESCAPE}"]
    params: dict[str, Any] = {"like": like_pattern(term), "term": term, "cap": CANDIDATE_PRODUCT_CAP}
    if not await tsquery_is_empty(cur, term):
        lexical.append("p.search_tsv @@ websearch_to_tsquery('english', %(term)s)")

    rungs: list[tuple[str, dict[str, Any]]] = [
        (" OR ".join(lexical), params),
        ("similarity(p.name, %(term)s) >= %(thr)s", {**params, "thr": TRIGRAM_THRESHOLD}),
        ("word_similarity(%(term)s, p.name) > %(wthr)s", {**params, "wthr": WORD_TRIGRAM_THRESHOLD}),
    ]
    for condition, rung_params in rungs:
        await cur.execute(
            f"SELECT p.product_id FROM products p WHERE ({condition}) "  # noqa: S608 - static SQL
            "ORDER BY similarity(p.name, %(term)s) DESC, p.product_id ASC LIMIT %(cap)s",
            rung_params,
        )
        ids = [int(row["product_id"]) for row in await cur.fetchall()]
        if ids:
            return ids
    return []


async def _page(
    cur: Any,
    day: date | None,
    interpretation: Interpretation,
    limit: int,
) -> dict[str, Any]:
    if day is None:
        return _empty_page(limit)
    # Every intent reaching here is already one of the supported seven, but the
    # lookup stays total so an unsupported value can never become a 5xx on a route
    # that is supposed to be the fast one.
    order = INTENT_ORDER.get(interpretation.intent) or INTENT_ORDER[BROWSE]
    clauses = ["f.day = %(day)s"]
    params: dict[str, Any] = {"day": day, "limit": limit, "offset": 0}
    if interpretation.intent == PROMO:
        # The one intent that *filters* rather than only ranks: "on promo" asks for
        # the promoted rows, and ranking them first while counting the rest would
        # report a total that answers a different question.
        clauses.append("f.promo_flag")
    if interpretation.product_term:
        ids = await _candidate_ids(cur, interpretation.product_term)
        if not ids:
            return _empty_page(limit)
        clauses.append("f.product_id = ANY(%(ids)s)")
        params["ids"] = ids
    return await fact_page(cur, clauses, params, order, limit, 0)


async def _explore(
    query: str,
    interpretation: Interpretation,
    limit: int,
) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            day, _products = await _context(cur)
            page = await _page(cur, day, interpretation, limit)
    return {
        "query": query,
        "day": day.isoformat() if day else "",
        "interpretation": interpretation.as_dict(),
        "page": page,
    }


@router.get("/explore", response_model=ExploreOut)
async def explore(
    query: str = Query(default="", max_length=EXPLORE_QUERY_MAX_CHARS),
    limit: int = Query(default=EXPLORE_DEFAULT_LIMIT, ge=1, le=EXPLORE_MAX_LIMIT),
) -> dict[str, Any]:
    """Fast, deterministic explore: no model call, newest day only."""
    return await _explore(query, interpret(query), limit)


@router.post("/explore/interpret", response_model=ExploreInterpretOut)
async def explore_interpret(body: ExploreInterpretIn) -> dict[str, Any]:
    """The same explore, plus one advisory Laya pass over the raw query.

    The deterministic parse always runs and always decides the retrieval: explicit
    phrases win outright. Laya is consulted once and its intent is used only when
    the text had no explicit phrase and the answer names a supported intent.
    """
    deterministic = interpret(body.query)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            day, products = await _context(cur)

    # The model call is deliberately made with no pool connection checked out: it
    # is a multi-second CPU forward pass and holding a connection through it would
    # starve the dashboard's own queries.
    result = await laya.predict(
        render_query_state(body.query, day, products),
        questions=intent_questions(),
    )

    interpretation = deterministic
    if deterministic.source != SOURCE_EXPLICIT and result.get("available"):
        answer = ((result.get("answers") or {}).get("intent") or {}).get("answer")
        if isinstance(answer, str) and answer in SUPPORTED_INTENTS:
            interpretation = Interpretation(intent=answer, product_term=deterministic.product_term, source=SOURCE_MODEL)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            page = await _page(cur, day, interpretation, body.limit)

    return {
        "query": body.query,
        "day": day.isoformat() if day else "",
        "interpretation": interpretation.as_dict(),
        "page": page,
        "laya": result,
    }
