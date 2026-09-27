/**
 * TypeScript mirrors of the frozen service contract (docs/CONTRACT.md §1, §4, §5, §6).
 * Field names are snake_case and MUST match the API responses exactly.
 * No `any` anywhere in this file: unknown-shaped payloads use `Record<string, unknown>`.
 */

export type Severity = 'critical' | 'warn' | 'info';
export type SubjectType = 'product' | 'category';
export type SortKey = 'revenue' | 'units' | 'price' | 'margin' | 'day';

export type Evidence = Record<string, unknown>;

/* ------------------------------------------------------------------ §5 meta */

export interface DatasetFacts {
	facts: number;
	products: number;
	stores: number;
	day_min: string;
	day_max: string;
	seed: number;
}

export interface CategoryCount {
	category: string;
	products: number;
}

export interface MetaResponse {
	dataset: DatasetFacts;
	categories: CategoryCount[];
	brands: string[];
	formats: string[];
	regions: string[];
}

export interface HealthResponse {
	status: string;
	db: boolean;
	rollup_lag_seconds: number | null;
	dataset: DatasetFacts | null;
}

/* -------------------------------------------------------------- §5 products */

export interface ProductListItem {
	product_id: number;
	sku: string;
	name: string;
	brand: string;
	category: string;
	subcategory: string;
	uom: string;
	pack_size: number;
	is_private_label: boolean;
	is_perishable: boolean;
	list_price: number;
	latest_day: string | null;
	latest_units: number | null;
	latest_price: number | null;
	latest_margin_pct: number | null;
}

export interface ProductPage {
	total: number;
	limit: number;
	offset: number;
	items: ProductListItem[];
}

export interface ProductQuery {
	q?: string;
	category?: string;
	brand?: string;
	private_label?: boolean;
	perishable?: boolean;
	limit?: number;
	offset?: number;
}

/* ---------------------------------------------------------------- §5 search */

export interface SearchRow {
	day: string;
	store_id: number;
	store_name: string;
	region: string;
	format: string;
	product_id: number;
	sku: string;
	product: string;
	brand: string;
	category: string;
	price: number;
	unit_cost: number;
	units_sold: number;
	revenue: number;
	margin_pct: number;
	promo_flag: boolean;
	inventory: number;
	on_order: number;
}

export interface SearchTotals {
	revenue: number;
	units: number;
	margin_pct: number;
	rows: number;
}

export interface SearchPage {
	total: number;
	limit: number;
	offset: number;
	items: SearchRow[];
	totals: SearchTotals;
}

export interface SearchQuery {
	q?: string;
	product_id?: number;
	store_id?: number;
	category?: string;
	brand?: string;
	format?: string;
	region?: string;
	promo?: boolean;
	min_price?: number;
	max_price?: number;
	min_units?: number;
	date_from?: string;
	date_to?: string;
	sort?: SortKey;
	limit?: number;
	offset?: number;
}

/* -------------------------------------------------------------- §5 overview */

export interface OverviewDay {
	day: string;
	units: number;
	revenue: number;
	cogs: number;
	margin_pct: number;
	pl_units: number;
	pl_share: number;
}

export interface OverviewCategory {
	category: string;
	units: number;
	revenue: number;
	margin_pct: number;
	pl_share: number;
	units_dod_pct: number;
}

export interface OverviewMover {
	product_id: number;
	name: string;
	category: string;
	units: number;
	baseline: number;
	lift: number;
	revenue: number;
}

export interface SignalCounts {
	critical: number;
	warn: number;
	info: number;
}

export interface OverviewResponse {
	days: OverviewDay[];
	categories: OverviewCategory[];
	movers: OverviewMover[];
	signal_counts: SignalCounts;
	as_of: string;
	rollups_as_of: string;
}

/* -------------------------------------------------------------- §4 patterns */

export interface PatternSeverityBand {
	severity: Severity;
	when: string;
}

export interface PatternDef {
	id: string;
	label: string;
	scope: SubjectType;
	description: string;
	thresholds: Record<string, number>;
	severity_bands: PatternSeverityBand[];
	action_template: string;
	baseline_days: number;
	min_obs: number;
}

export interface PatternsResponse {
	patterns: PatternDef[];
}

/* --------------------------------------------------------------- §5 signals */

export interface Signal {
	signal_id: number;
	fired_at: string;
	day: string;
	pattern: string;
	severity: Severity;
	subject_type: SubjectType;
	subject_id: number;
	subject_label: string;
	score: number;
	evidence: Evidence;
	action: string;
}

export interface SignalsResponse {
	items: Signal[];
	counts: SignalCounts;
	total: number;
}

export interface SignalsQuery {
	pattern?: string;
	severity?: Severity;
	subject_type?: SubjectType;
	subject_id?: number;
	since?: string;
	limit?: number;
	only_unseen?: boolean;
}

export interface SeenResponse {
	updated: number;
}

/* ------------------------------------------------------------------ §5 scan */

export interface ScanRequest {
	day?: string | null;
	product_ids?: number[] | null;
	categories?: string[] | null;
	persist?: boolean;
}

export interface ScanResponse {
	scanned: number;
	signals: Signal[];
}

/* -------------------------------------------------------------- §5 series */

export interface SeriesPoint {
	day: string;
	units: number;
	avg_price: number;
	revenue: number;
	margin_pct: number;
	inventory: number;
	promo_stores: number;
	store_count: number;
}

export interface ProductSeriesResponse {
	product: ProductListItem;
	series: SeriesPoint[];
}

export interface CategorySeriesResponse {
	category: string;
	series: SeriesPoint[];
}

/* ---------------------------------------------------- §6.2 realtime frames */

export interface HelloFrame {
	type: 'hello';
	at: string;
	dataset: DatasetFacts;
}

export interface TickMutation {
	store_id: number;
	product_id: number;
}

export interface TickSummary {
	rows: number;
	products: number;
	stores: number;
	tick: number;
}

export interface TickFrame {
	type: 'tick';
	at: string;
	day: string;
	mutations: TickMutation[];
	summary: TickSummary;
}

export interface SignalFrame {
	type: 'signal';
	at: string;
	signal: Signal;
}

export interface HeartbeatFrame {
	type: 'heartbeat';
	at: string;
}

export interface ErrorFrame {
	type: 'error';
	at: string;
	detail: string;
}

export interface PongFrame {
	type: 'pong';
	at: string;
}

/** Every frame the server can push; `type` narrows the payload. */
export type ServerFrame = HelloFrame | TickFrame | SignalFrame | HeartbeatFrame | ErrorFrame;

/** Client -> server frames (§6.2). */
export interface PingFrame {
	type: 'ping';
}

export interface SubscribeFrame {
	type: 'subscribe';
	patterns: string[];
	severities: Severity[];
}

export type ClientFrame = PingFrame | SubscribeFrame;

/* ----------------------------------------------------- UI state (web-only) */

/** Promo tri-state used by the search panel. */
export type PromoFilter = 'any' | 'only' | 'exclude';

/** Raw search-panel state. Text fields stay strings so inputs can be cleared. */
export interface SearchFilters {
	q: string;
	category: string;
	brand: string;
	store_id: string;
	format: string;
	region: string;
	promo: PromoFilter;
	min_price: string;
	max_price: string;
	min_units: string;
	date_from: string;
	date_to: string;
	sort: SortKey;
	limit: number;
}

/** One live simulator tick, as shown on the activity strip. */
export interface TickActivity {
	tick: number;
	at: string;
	day: string;
	rows: number;
	products: number;
	stores: number;
}

/* -------------------------------------------------------- Laya decision model */

/** One option's probability from a Laya distribution. */
export type LayaProbabilities = Record<string, number>;

/** A flattened typed answer from one Laya primitive (`choice`, `score`, `noul`). */
export interface LayaAnswer {
	/** The decided value: an option name, a rubric level, or P(true) for `noul`. */
	answer: string | number | null;
	/** Rubric index behind `answer` when the primitive was `score`. */
	level_index: number | null;
	/** Probability of the answer the model chose (`answer_confidence` upstream). */
	confidence: number | null;
	probabilities: LayaProbabilities | null;
	/** Maps a rubric index to its level name, for `score`. */
	legend: Record<string, string> | null;
	/** Continuous position on the rubric, for `score`. */
	score_position: number | null;
}

export interface LayaRouting {
	model?: string;
	repo?: string;
	reason?: string;
}

export interface LayaResult {
	available: boolean;
	answers: Record<string, LayaAnswer>;
	routing: LayaRouting | null;
	latency_ms: number | null;
	/** Present when `available` is false: why the model could not answer. */
	detail?: string;
}

export interface RuleVerdict {
	pattern: string;
	severity: string;
	severity_index: number;
	reorder: boolean;
	patterns: string[];
	hits: Signal[];
}

export interface DecideStateView {
	name: string;
	category: string;
	brand: string;
	is_private_label: boolean;
	is_perishable: boolean;
	units: number;
	units_baseline: number | null;
	unit_lift: number | null;
	avg_price: number;
	price_baseline: number | null;
	price_lift: number | null;
	margin_pct: number;
	margin_pct_baseline: number | null;
	inventory: number;
	inventory_cover_days: number | null;
	store_count: number;
	promo_stores: number;
	observations: number;
}

export interface DecideAgreement {
	pattern: boolean | null;
	severity: boolean | null;
	reorder: boolean | null;
	laya: {
		pattern: string | null;
		severity: string | null;
		severity_index: number | null;
		reorder_probability: number | null;
		reorder: boolean | null;
	};
	rules: {
		pattern: string;
		severity: string;
		severity_index: number;
		reorder: boolean;
	};
}

export interface DecideResponse {
	product_id: number;
	day: string;
	state: DecideStateView;
	state_text: string;
	laya: LayaResult;
	rules: RuleVerdict;
	agreement: DecideAgreement;
}

export interface LayaHealthResponse {
	available: boolean;
	enabled: boolean;
	url: string;
	timeout_seconds: number;
	status_code?: number;
	probe_ms?: number;
	detail?: unknown;
	checkpoint_options?: string[];
}
