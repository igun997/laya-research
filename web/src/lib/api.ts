/**
 * Typed fetch helpers for every endpoint in docs/CONTRACT.md §5.
 *
 * Base URL: `import.meta.env.PUBLIC_API_BASE` when set (docker compose injects `/api`),
 * otherwise `/api` — which is exactly how Caddy routes the browser's traffic.
 * Every call accepts an AbortSignal and rejects with a single `ApiError` type.
 */

import type {
	CategorySeriesResponse,
	DecideResponse,
	HealthResponse,
	LayaHealthResponse,
	MetaResponse,
	OverviewResponse,
	PatternsResponse,
	ProductPage,
	ProductQuery,
	ProductSeriesResponse,
	ScanRequest,
	ScanResponse,
	SearchPage,
	SearchQuery,
	SeenResponse,
	SignalsQuery,
	SignalsResponse
} from './types';

/* ------------------------------------------------------------------- base */

interface PublicEnv {
	PUBLIC_API_BASE?: string;
}

function resolveBase(): string {
	const env = import.meta.env as PublicEnv;
	const raw = env.PUBLIC_API_BASE;
	if (typeof raw === 'string' && raw.trim() !== '') {
		return raw.trim().replace(/\/+$/, '');
	}
	return '/api';
}

/** Resolved API base, without a trailing slash. */
export const API_BASE: string = resolveBase();

/* ------------------------------------------------------------------ error */

export interface ApiErrorInit {
	status: number;
	detail: string;
	url: string;
}

/** The single error type thrown by every helper in this module. */
export class ApiError extends Error {
	readonly status: number;
	readonly detail: string;
	readonly url: string;

	constructor(init: ApiErrorInit) {
		super(init.status === 0 ? `network error: ${init.detail}` : `${init.status} ${init.detail}`);
		this.name = 'ApiError';
		this.status = init.status;
		this.detail = init.detail;
		this.url = init.url;
	}

	/** True when the request never reached the server (offline, DNS, aborted socket). */
	get isNetwork(): boolean {
		return this.status === 0;
	}
}

export function isApiError(value: unknown): value is ApiError {
	return value instanceof ApiError;
}

/* --------------------------------------------------------------- plumbing */

export type QueryValue = string | number | boolean | null | undefined;

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
	const url = `${API_BASE}${path}`;
	if (!query) return url;
	const parts: string[] = [];
	for (const [key, value] of Object.entries(query)) {
		if (value === undefined || value === null || value === '') continue;
		parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
	}
	return parts.length > 0 ? `${url}?${parts.join('&')}` : url;
}

interface RequestOptions {
	method?: 'GET' | 'POST';
	body?: unknown;
	signal?: AbortSignal;
	/**
	 * Abort the request after this many milliseconds. Needed because a Laya
	 * forward pass on CPU takes 8 to 12 seconds, far longer than any other call,
	 * and a hung model must not leave a spinner running forever.
	 */
	timeoutMs?: number;
}

function isAbort(error: unknown): boolean {
	return error instanceof DOMException && error.name === 'AbortError';
}

async function readDetail(response: Response): Promise<string> {
	try {
		const text = await response.text();
		if (text === '') return response.statusText;
		try {
			const parsed: unknown = JSON.parse(text);
			if (parsed !== null && typeof parsed === 'object' && 'detail' in parsed) {
				const detail = (parsed as { detail: unknown }).detail;
				if (typeof detail === 'string') return detail;
				return JSON.stringify(detail);
			}
		} catch {
			/* not JSON — fall through to raw text */
		}
		return text.slice(0, 300);
	} catch {
		return response.statusText;
	}
}

async function request<T>(path: string, query?: Record<string, QueryValue>, options: RequestOptions = {}): Promise<T> {
	const url = buildUrl(path, query);
	const headers: Record<string, string> = { Accept: 'application/json' };
	const init: RequestInit = { method: options.method ?? 'GET', headers, signal: options.signal };
	if (options.body !== undefined) {
		headers['Content-Type'] = 'application/json';
		init.body = JSON.stringify(options.body);
	}

	// A caller-supplied signal still wins: we only add a deadline, we never remove
	// the caller's ability to abort early.
	let timer: number | undefined;
	let onCallerAbort: (() => void) | null = null;
	if (options.timeoutMs !== undefined && options.timeoutMs > 0) {
		const controller = new AbortController();
		timer = setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), options.timeoutMs);
		if (options.signal) {
			if (options.signal.aborted) controller.abort(options.signal.reason);
			else {
				onCallerAbort = () => controller.abort(options.signal?.reason);
				options.signal.addEventListener('abort', onCallerAbort, { once: true });
			}
		}
		init.signal = controller.signal;
	}

	let response: Response;
	try {
		response = await fetch(url, init);
	} catch (error) {
		if (isAbort(error)) throw error;
		throw new ApiError({
			status: 0,
			detail: error instanceof Error ? error.message : String(error),
			url
		});
	} finally {
		clearTimeout(timer);
		if (onCallerAbort !== null && options.signal) {
			options.signal.removeEventListener('abort', onCallerAbort);
		}
	}

	if (!response.ok) {
		throw new ApiError({ status: response.status, detail: await readDetail(response), url });
	}
	if (response.status === 204) {
		return undefined as T;
	}
	try {
		return (await response.json()) as T;
	} catch (error) {
		throw new ApiError({
			status: response.status,
			detail: `invalid JSON response: ${error instanceof Error ? error.message : String(error)}`,
			url
		});
	}
}

/* ---------------------------------------------------------------- helpers */

/** `GET /api/health` */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
	return request<HealthResponse>('/health', undefined, { signal });
}

/** `GET /api/meta` — dataset size, categories, brands, formats, regions. */
export function getMeta(signal?: AbortSignal): Promise<MetaResponse> {
	return request<MetaResponse>('/meta', undefined, { signal });
}

/** `GET /api/products` — catalog search (full-text + trigram fallback server-side). */
export function getProducts(query: ProductQuery = {}, signal?: AbortSignal): Promise<ProductPage> {
	return request<ProductPage>(
		'/products',
		{
			q: query.q,
			category: query.category,
			brand: query.brand,
			private_label: query.private_label,
			perishable: query.perishable,
			limit: query.limit,
			offset: query.offset
		},
		{ signal }
	);
}

/** `GET /api/search` — fact-grain search, the workhorse. */
export function search(query: SearchQuery = {}, signal?: AbortSignal): Promise<SearchPage> {
	return request<SearchPage>(
		'/search',
		{
			q: query.q,
			product_id: query.product_id,
			store_id: query.store_id,
			category: query.category,
			brand: query.brand,
			format: query.format,
			region: query.region,
			promo: query.promo,
			min_price: query.min_price,
			max_price: query.max_price,
			min_units: query.min_units,
			date_from: query.date_from,
			date_to: query.date_to,
			sort: query.sort,
			limit: query.limit,
			offset: query.offset
		},
		{ signal }
	);
}

/** `GET /api/overview` — day rollups, category table, top movers. */
export function getOverview(days = 14, signal?: AbortSignal): Promise<OverviewResponse> {
	return request<OverviewResponse>('/overview', { days }, { signal });
}

/** `GET /api/patterns` — declarative rule catalog, served verbatim. */
export function getPatterns(signal?: AbortSignal): Promise<PatternsResponse> {
	return request<PatternsResponse>('/patterns', undefined, { signal });
}

/** `GET /api/signals` */
export function getSignals(query: SignalsQuery = {}, signal?: AbortSignal): Promise<SignalsResponse> {
	return request<SignalsResponse>(
		'/signals',
		{
			pattern: query.pattern,
			severity: query.severity,
			subject_type: query.subject_type,
			subject_id: query.subject_id,
			since: query.since,
			limit: query.limit,
			only_unseen: query.only_unseen
		},
		{ signal }
	);
}

/** `POST /api/signals/seen` */
export function markSignalsSeen(signalIds: number[], signal?: AbortSignal): Promise<SeenResponse> {
	return request<SeenResponse>('/signals/seen', undefined, {
		method: 'POST',
		body: { signal_ids: signalIds },
		signal
	});
}

/** `POST /api/patterns/scan` — `persist: false` computes without writing. */
export function scan(body: ScanRequest, signal?: AbortSignal): Promise<ScanResponse> {
	return request<ScanResponse>('/patterns/scan', undefined, { method: 'POST', body, signal });
}

/** `GET /api/products/{id}/series` */
export function getProductSeries(productId: number, days = 30, signal?: AbortSignal): Promise<ProductSeriesResponse> {
	return request<ProductSeriesResponse>(`/products/${productId}/series`, { days }, { signal });
}

/** `GET /api/categories/{category}/series` */
export function getCategorySeries(category: string, days = 30, signal?: AbortSignal): Promise<CategorySeriesResponse> {
	return request<CategorySeriesResponse>(`/categories/${encodeURIComponent(category)}/series`, { days }, { signal });
}

/* ------------------------------------------------- Laya decision model */

/**
 * `GET /api/laya/health`
 *
 * Cheap: it probes `laya-serve`'s own `/health`, which does not run inference.
 */
export function getLayaHealth(signal?: AbortSignal): Promise<LayaHealthResponse> {
	return request<LayaHealthResponse>('/laya/health', undefined, { signal });
}

/**
 * `GET /api/decide/{productId}`
 *
 * Runs a full CPU forward pass, measured at 8 to 12 seconds on this host, so the
 * default timeout is far above the client default and callers must show a pending
 * state. Never aborts quickly: a slow answer is still an answer.
 */
export function getDecide(
	productId: number,
	options: { day?: string; timeoutMs?: number } = {},
	signal?: AbortSignal
): Promise<DecideResponse> {
	return request<DecideResponse>(
		`/decide/${productId}`,
		{ day: options.day },
		{ signal, timeoutMs: options.timeoutMs ?? 180_000 }
	);
}
