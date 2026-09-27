<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getHealth,
		getMeta,
		getOverview,
		getPatterns,
		getSignals,
		isApiError,
		markSignalsSeen,
		search
	} from '$lib/api';
	import { StreamClient } from '$lib/stream';
	import type { StreamStatus } from '$lib/stream';
	import { fmtCompact, fmtInt } from '$lib/format';
	import type {
		DatasetFacts,
		MetaResponse,
		OverviewResponse,
		PatternDef,
		SearchFilters,
		SearchPage,
		SearchQuery,
		Severity,
		ServerFrame,
		Signal,
		SignalCounts,
		SortKey,
		TickActivity,
		TickFrame
	} from '$lib/types';

	import StatusBar from '$lib/components/StatusBar.svelte';
	import ExplorePanel from '$lib/components/ExplorePanel.svelte';
	import SearchPanel from '$lib/components/SearchPanel.svelte';
	import ResultTable from '$lib/components/ResultTable.svelte';
	import SignalFeed from '$lib/components/SignalFeed.svelte';
	import OverviewPanel from '$lib/components/OverviewPanel.svelte';
	import ProductDetail from '$lib/components/ProductDetail.svelte';
	import LayaPlayground from '$lib/components/LayaPlayground.svelte';

	/* ------------------------------------------------------------ page state */

	function defaultFilters(): SearchFilters {
		return {
			q: '',
			category: '',
			brand: '',
			store_id: '',
			format: '',
			region: '',
			promo: 'any',
			min_price: '',
			max_price: '',
			min_units: '',
			date_from: '',
			date_to: '',
			sort: 'revenue',
			limit: 50
		};
	}

	let filters = $state<SearchFilters>(defaultFilters());

	let page = $state<SearchPage | null>(null);
	let searching = $state(false);
	let searchError = $state<string | null>(null);
	let offset = $state(0);

	let meta = $state<MetaResponse | null>(null);
	let metaLoading = $state(false);
	let metaError = $state<string | null>(null);

	let overview = $state<OverviewResponse | null>(null);
	let overviewDays = $state(14);
	let overviewLoading = $state(false);
	let overviewError = $state<string | null>(null);

	let patterns = $state<PatternDef[]>([]);

	let signals = $state<Signal[]>([]);
	let signalCounts = $state<SignalCounts>({ critical: 0, warn: 0, info: 0 });
	let signalTotal = $state(0);
	let signalsLoading = $state(false);
	let signalsError = $state<string | null>(null);
	let activePatterns = $state<string[]>([]);
	let activeSeverities = $state<Severity[]>([]);
	let seenCount = $state(0);

	let selectedProductId = $state<number | null>(null);

	let status = $state<StreamStatus>('connecting');
	let tickFrames = $state(0);
	let lastTick = $state<number | null>(null);
	let liveDay = $state<string | null>(null);
	let lastFrameAt = $state<number | null>(null);
	let reconnects = $state(0);
	let streamError = $state<string | null>(null);

	let activity = $state<TickActivity[]>([]);
	let sessionRows = $state(0);
	let touchedProductCount = $state(0);
	let touchedStoreCount = $state(0);
	let rollupLag = $state<number | null>(null);
	let now = $state(Date.now());

	/**
	 * Monotonic count of accepted live ticks, handed to the explore panel so it can
	 * refresh its SQL layer on a throttle. `tickFrames` is the same number today, but
	 * it is a display statistic; this one exists purely as a change signal and is
	 * never reset, so the panel can compare it against its own last-refresh stamp.
	 */
	let liveTick = $state(0);

	/* ------------------------------------------------------ non-reactive refs */

	const MAX_SIGNALS = 200;
	const MAX_ACTIVITY = 24;
	const OVERVIEW_REFRESH_MS = 20_000;
	const HEALTH_POLL_MS = 15_000;

	const touchedProducts = new Set<number>();
	const touchedStores = new Set<number>();
	let lastOverviewAt = 0;
	let searchAbort: AbortController | null = null;
	let seedAbort: AbortController | null = null;

	/* ------------------------------------------------------------ search path */

	function numOrUndefined(raw: string): number | undefined {
		if (raw.trim() === '') return undefined;
		const parsed = Number(raw);
		return Number.isFinite(parsed) ? parsed : undefined;
	}

	function currentQuery(nextOffset: number): SearchQuery {
		return {
			q: filters.q.trim() === '' ? undefined : filters.q.trim(),
			category: filters.category === '' ? undefined : filters.category,
			brand: filters.brand === '' ? undefined : filters.brand,
			store_id: numOrUndefined(filters.store_id),
			format: filters.format === '' ? undefined : filters.format,
			region: filters.region === '' ? undefined : filters.region,
			promo: filters.promo === 'any' ? undefined : filters.promo === 'only',
			min_price: numOrUndefined(filters.min_price),
			max_price: numOrUndefined(filters.max_price),
			min_units: numOrUndefined(filters.min_units),
			date_from: filters.date_from === '' ? undefined : filters.date_from,
			date_to: filters.date_to === '' ? undefined : filters.date_to,
			sort: filters.sort,
			limit: filters.limit,
			offset: nextOffset
		};
	}

	function describeError(cause: unknown): string {
		if (isApiError(cause)) {
			return cause.status === 0 ? `galat jaringan: ${cause.detail}` : `${cause.status} ${cause.detail}`;
		}
		return cause instanceof Error ? cause.message : String(cause);
	}

	async function runSearch(nextOffset: number) {
		searchAbort?.abort();
		const controller = new AbortController();
		searchAbort = controller;
		searching = true;
		searchError = null;
		offset = nextOffset;
		try {
			const response = await search(currentQuery(nextOffset), controller.signal);
			if (controller.signal.aborted) return;
			page = response;
			searching = false;
		} catch (cause) {
			if (controller.signal.aborted) return;
			searching = false;
			searchError = describeError(cause);
		}
	}

	/* Debounce: 250 ms after the last keystroke; a tick never triggers this. */
	let debounceTimer: number | undefined;

	function scheduleSearch() {
		clearTimeout(debounceTimer);
		debounceTimer = setTimeout(() => {
			debounceTimer = undefined;
			void runSearch(0);
		}, 250);
	}

	function changeSort(sort: SortKey) {
		if (filters.sort === sort) return;
		filters.sort = sort;
		void runSearch(0);
	}

	function changePage(nextOffset: number) {
		void runSearch(Math.max(0, nextOffset));
	}

	function changeOverviewDays(days: number) {
		overviewDays = days;
		void loadOverview();
	}

	/* -------------------------------------------------------------- meta path */

	async function loadMeta(signal?: AbortSignal) {
		metaLoading = true;
		metaError = null;
		try {
			meta = await getMeta(signal);
			metaLoading = false;
			// An unfiltered /api/search has to aggregate and sort every fact row,
			// which is ~10 s at the default 7M-row dataset. The useful default for
			// a market view is the current state, so seed the window with the
			// newest day — once, and only if the user has not picked a range.
			// "all days" in the search panel clears it again.
			const newest = meta.dataset.day_max;
			if (newest && filters.date_from === '' && filters.date_to === '') {
				filters.date_from = newest;
				filters.date_to = newest;
				void runSearch(0);
			}
		} catch (cause) {
			if (signal?.aborted) return;
			metaLoading = false;
			metaError = describeError(cause);
		}
	}

	async function loadPatterns(signal?: AbortSignal) {
		try {
			const response = await getPatterns(signal);
			patterns = response.patterns;
		} catch {
			/* chips fall back to raw pattern ids */
		}
	}

	async function loadOverview() {
		overviewLoading = true;
		overviewError = null;
		lastOverviewAt = Date.now();
		try {
			overview = await getOverview(overviewDays);
			overviewLoading = false;
		} catch (cause) {
			overviewLoading = false;
			overviewError = describeError(cause);
		}
	}

	async function pollHealth() {
		try {
			const health = await getHealth();
			rollupLag = health.rollup_lag_seconds;
		} catch {
			/* the WS state already tells the user we are offline */
		}
	}

	/* ----------------------------------------------------------- signal path */

	/**
	 * The feed is seeded from the full newest-first window (no server-side pattern/severity
	 * filter) and the chips filter the rendered list, so the severity census in the header
	 * stays a true global count. Live `signal` frames are appended with the same rules.
	 */
	async function loadSignals() {
		seedAbort?.abort();
		const controller = new AbortController();
		seedAbort = controller;
		signalsLoading = true;
		signalsError = null;
		try {
			const response = await getSignals({ limit: MAX_SIGNALS }, controller.signal);
			if (controller.signal.aborted) return;
			signals = response.items.slice().sort(byFiredDesc);
			signalCounts = response.counts;
			signalTotal = response.total;
			signalsLoading = false;
		} catch (cause) {
			if (controller.signal.aborted) return;
			signalsLoading = false;
			signalsError = describeError(cause);
		}
	}

	function byFiredDesc(left: Signal, right: Signal): number {
		const leftAt = Date.parse(left.fired_at);
		const rightAt = Date.parse(right.fired_at);
		if (Number.isNaN(leftAt) || Number.isNaN(rightAt)) return right.signal_id - left.signal_id;
		if (rightAt !== leftAt) return rightAt - leftAt;
		return right.signal_id - left.signal_id;
	}

	/** Dedupe by `signal_id`: a re-fired signal updates in place, never appears twice. */
	function upsertSignal(incoming: Signal) {
		const index = signals.findIndex((existing) => existing.signal_id === incoming.signal_id);
		if (index >= 0) {
			const next = signals.slice();
			next[index] = incoming;
			signals = next.sort(byFiredDesc).slice(0, MAX_SIGNALS);
			return;
		}
		signals = [incoming, ...signals].sort(byFiredDesc).slice(0, MAX_SIGNALS);
		signalTotal += 1;
		signalCounts = {
			critical: signalCounts.critical + (incoming.severity === 'critical' ? 1 : 0),
			warn: signalCounts.warn + (incoming.severity === 'warn' ? 1 : 0),
			info: signalCounts.info + (incoming.severity === 'info' ? 1 : 0)
		};
	}

	async function markSeen(ids: number[]) {
		if (ids.length === 0) return;
		const removed = signals.filter((signal) => ids.includes(signal.signal_id));
		try {
			await markSignalsSeen(ids);
		} catch (cause) {
			signalsError = describeError(cause);
			return;
		}
		const set = new Set(ids);
		signals = signals.filter((signal) => !set.has(signal.signal_id));
		seenCount += removed.length;
		signalTotal = Math.max(0, signalTotal - removed.length);
		for (const signal of removed) {
			if (signal.severity === 'critical') signalCounts.critical = Math.max(0, signalCounts.critical - 1);
			if (signal.severity === 'warn') signalCounts.warn = Math.max(0, signalCounts.warn - 1);
			if (signal.severity === 'info') signalCounts.info = Math.max(0, signalCounts.info - 1);
		}
	}

	function togglePattern(pattern: string) {
		if (pattern === '') {
			activePatterns = [];
		} else {
			activePatterns = activePatterns.includes(pattern)
				? activePatterns.filter((entry) => entry !== pattern)
				: [...activePatterns, pattern];
		}
		stream?.subscribe(activePatterns, activeSeverities);
	}

	function toggleSeverity(severity: Severity) {
		activeSeverities = activeSeverities.includes(severity)
			? activeSeverities.filter((entry) => entry !== severity)
			: [...activeSeverities, severity];
		stream?.subscribe(activePatterns, activeSeverities);
	}

	const visibleSignals = $derived(
		signals.filter(
			(signal) =>
				(activePatterns.length === 0 || activePatterns.includes(signal.pattern)) &&
				(activeSeverities.length === 0 || activeSeverities.includes(signal.severity))
		)
	);

	/* --------------------------------------------------------- realtime path */

	let stream: StreamClient | null = null;

	function applyTick(frame: TickFrame) {
		tickFrames += 1;
		liveTick += 1;
		lastTick = frame.summary.tick;
		liveDay = frame.day;
		sessionRows += frame.summary.rows;
		for (const mutation of frame.mutations) {
			touchedProducts.add(mutation.product_id);
			touchedStores.add(mutation.store_id);
		}
		touchedProductCount = touchedProducts.size;
		touchedStoreCount = touchedStores.size;
		activity = [
			{
				tick: frame.summary.tick,
				at: frame.at,
				day: frame.day,
				rows: frame.summary.rows,
				products: frame.summary.products,
				stores: frame.summary.stores
			},
			...activity
		].slice(0, MAX_ACTIVITY);

		/* Ticks refresh the rollup-backed panels on a throttle — never the search results. */
		if (Date.now() - lastOverviewAt > OVERVIEW_REFRESH_MS) void loadOverview();
	}

	function handleFrame(frame: ServerFrame) {
		lastFrameAt = Date.now();
		switch (frame.type) {
			case 'tick':
				applyTick(frame);
				break;
			case 'signal':
				streamError = null;
				upsertSignal(frame.signal);
				break;
			case 'hello':
				streamError = null;
				if (!meta && frame.dataset) {
					meta = {
						dataset: frame.dataset as DatasetFacts,
						categories: [],
						brands: [],
						formats: [],
						regions: []
					};
				}
				break;
			case 'error':
				streamError = frame.detail;
				break;
			case 'heartbeat':
				break;
		}
	}

	/* --------------------------------------------------------------- lifecycle */

	onMount(() => {
		const controller = new AbortController();

		void loadMeta(controller.signal);
		void loadPatterns(controller.signal);
		void loadOverview();
		void loadSignals();
		void pollHealth();
		void runSearch(0);

		stream = new StreamClient({
			onFrame: handleFrame,
			onStatus: (next) => {
				status = next;
			},
			onStats: (stats) => {
				reconnects = stats.reconnects;
			}
		});
		stream.connect();

		const clock = setInterval(() => {
			now = Date.now();
		}, 1000);

		const health = setInterval(() => {
			void pollHealth();
		}, HEALTH_POLL_MS);

		return () => {
			clearInterval(clock);
			clearInterval(health);
			if (debounceTimer !== undefined) clearTimeout(debounceTimer);
			searchAbort?.abort();
			seedAbort?.abort();
			controller.abort();
			stream?.close();
			stream = null;
		};
	});
</script>

<StatusBar
	dataset={meta?.dataset ?? null}
	{status}
	ticks={tickFrames}
	{lastTick}
	day={liveDay}
	rollupLagSeconds={rollupLag}
	{lastFrameAt}
	{reconnects}
	{now}
	loading={metaLoading}
	error={metaError}
/>

<main>
	<ExplorePanel
		{liveTick}
		{liveDay}
		{selectedProductId}
		{now}
		onSelectProduct={(productId) => {
			selectedProductId = productId;
		}}
	/>

	<section class="strip" aria-label="Aktivitas langsung">
		<span class="strip-title label">tick langsung</span>
		<span class="strip-stat mono">
			<span class="faint">baris berubah</span> {fmtCompact(sessionRows)}
		</span>
		<span class="strip-stat mono">
			<span class="faint">produk terdampak</span> {fmtInt(touchedProductCount)}
		</span>
		<span class="strip-stat mono">
			<span class="faint">toko terdampak</span> {fmtInt(touchedStoreCount)}
		</span>
		<span class="strip-stat mono">
			<span class="faint">pesan</span> {fmtInt(tickFrames)}
		</span>
		{#if streamError}
			<span class="error tiny">aliran data: {streamError}</span>
		{/if}
		<div class="spark">
			{#each activity as entry (entry.tick + ':' + entry.at)}
				<span
					class="spark-cell"
					title={`tick ${entry.tick} · ${entry.day} · ${entry.rows} baris · ${entry.products} produk · ${entry.stores} toko`}
					style={`--h:${Math.max(8, Math.min(100, (entry.rows / 250) * 100)).toFixed(0)}%`}
				></span>
			{:else}
				<span class="faint tiny">menunggu tick pertama…</span>
			{/each}
		</div>
	</section>

	<div class="grid">
		<div class="col col-left">
			<SearchPanel
				bind:filters
				categories={meta?.categories ?? []}
				brands={meta?.brands ?? []}
				formats={meta?.formats ?? []}
				regions={meta?.regions ?? []}
				dayMin={meta?.dataset.day_min ?? ''}
				dayMax={meta?.dataset.day_max ?? ''}
				loading={searching}
				total={page?.total ?? 0}
				rowCount={page?.items.length ?? 0}
				onchange={scheduleSearch}
			/>

			<ResultTable
				{page}
				loading={searching}
				error={searchError}
				selectedProductId={selectedProductId}
				activeSort={filters.sort}
				onSort={changeSort}
				onPage={changePage}
				onRowClick={(productId) => {
					selectedProductId = productId;
				}}
			/>

			<OverviewPanel
				{overview}
				loading={overviewLoading}
				error={overviewError}
				{now}
				days={overviewDays}
				onDays={changeOverviewDays}
				onProductClick={(productId) => {
					selectedProductId = productId;
				}}
			/>
		</div>

		<div class="col col-right">
			<SignalFeed
				signals={visibleSignals}
				{patterns}
				{activePatterns}
				{activeSeverities}
				counts={signalCounts}
				total={signalTotal}
				loading={signalsLoading}
				error={signalsError}
				{now}
				{seenCount}
				onTogglePattern={togglePattern}
				onToggleSeverity={toggleSeverity}
				onMarkSeen={markSeen}
				onRefresh={loadSignals}
			/>

			<LayaPlayground />

			{#if selectedProductId !== null}
				<ProductDetail
					productId={selectedProductId}
					days={30}
					onClose={() => {
						selectedProductId = null;
					}}
				/>
			{/if}
		</div>
	</div>
</main>

<style>
	main {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 6px 8px 8px;
		min-height: 0;
	}

	.strip {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		padding: 2px 8px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		font-size: var(--fs-sm);
	}

	.strip-title {
		color: var(--accent);
	}

	.strip-stat {
		font-size: var(--fs-sm);
	}

	.spark {
		display: flex;
		align-items: flex-end;
		gap: 2px;
		height: 18px;
		margin-left: auto;
		min-width: 120px;
	}

	.spark-cell {
		display: block;
		width: 5px;
		height: var(--h, 30%);
		background: var(--accent);
		opacity: 0.75;
		border-radius: 1px;
	}

	.spark-cell:first-child {
		opacity: 1;
	}

	.grid {
		display: grid;
		grid-template-columns: minmax(0, 1.65fr) minmax(340px, 1fr);
		gap: 6px;
		align-items: start;
		min-height: 0;
	}

	.col {
		display: flex;
		flex-direction: column;
		gap: 6px;
		min-width: 0;
	}

	.col-left :global(.results) {
		min-height: 46vh;
	}

	.col-right :global(.feed) {
		max-height: 62vh;
	}

	.tiny {
		font-size: var(--fs-xs);
	}

	@media (max-width: 1180px) {
		.grid {
			grid-template-columns: minmax(0, 1fr);
		}

		.col-right :global(.feed) {
			max-height: 50vh;
		}
	}
</style>
