<script lang="ts">
	/**
	 * Free-text explore: natural language in, deterministic SQL rows out.
	 *
	 * Two layers, deliberately kept apart because they cost wildly different things:
	 *
	 * * the **live SQL layer** — `GET /api/explore`, plain bound SQL over the newest
	 *   day. It runs on typing (debounced) and on live ticks (throttled) and never
	 *   touches the model.
	 * * the **Laya layer** — `POST /api/explore/interpret`, one CPU forward pass
	 *   after typing pauses (or an explicit rerun). Never re-run on a live tick.
	 *
	 * An explicit phrase in the query beats the model. When that happens the panel
	 * says so and names what the model wanted instead, because "the model agreed"
	 * and "the model was overruled" are different results and only one of them is
	 * evidence about the model.
	 */
	import { explore, interpretExplore, isApiError } from '$lib/api';
	import { fmtAgo, fmtCompact, fmtDay, fmtInt, fmtPct, fmtPrice, marginBand } from '$lib/format';
	import { untrack } from 'svelte';
	import type {
		ExploreIntent,
		ExploreInterpretResponse,
		ExploreResponse,
		ExploreSource,
		LayaAnswer,
		LayaResult,
		SearchRow
	} from '$lib/types';

	interface Props {
		/** Increments on every live tick frame; drives the throttled SQL refresh. */
		liveTick?: number;
		/** Day the simulator is writing, if a tick has been seen. */
		liveDay?: string | null;
		selectedProductId?: number | null;
		onSelectProduct?: (productId: number) => void;
		/** Shared 1 Hz clock from the page, used for the "updated Ns ago" line. */
		now?: number;
	}

	let {
		liveTick = 0,
		liveDay = null,
		selectedProductId = null,
		onSelectProduct,
		now = Date.now()
	}: Props = $props();

	/* --------------------------------------------------------------- constants */

	const ROWS = 20;
	/** Throttle for tick-driven SQL refreshes. Ticks land every few seconds. */
	const REFRESH_MS = 15_000;
	/** Hard floor so a burst of ticks or a day rollover cannot stampede the API. */
	const MIN_REFRESH_GAP_MS = 3_000;

	const INTENTS: ExploreIntent[] = [
		'browse',
		'low_inventory',
		'high_sales',
		'low_sales',
		'promo',
		'high_price',
		'low_price'
	];

	/** What each intent means in words. The API applies the ordering/filter. */
	const INTENT_GLOSS: Record<ExploreIntent, string> = {
		browse: 'tanpa metrik khusus',
		low_inventory: 'stok terendah per toko',
		high_sales: 'unit terjual terbanyak',
		low_sales: 'unit terjual tersedikit',
		promo: 'hanya baris promosi',
		high_price: 'harga tertinggi',
		low_price: 'harga terendah'
	};

	const INTENT_LABEL: Record<ExploreIntent, string> = {
		browse: 'jelajahi',
		low_inventory: 'stok menipis',
		high_sales: 'penjualan tinggi',
		low_sales: 'penjualan rendah',
		promo: 'promosi',
		high_price: 'harga tinggi',
		low_price: 'harga rendah'
	};

	const SOURCE_LABEL: Record<ExploreSource, string> = {
		explicit: 'frasa',
		default: 'bawaan',
		laya: 'Laya'
	};

	const SOURCE_NOTE: Record<ExploreSource, string> = {
		explicit: 'frasa ditemukan dalam pertanyaan',
		default: 'tidak ada frasa yang cocok; gunakan bawaan',
		laya: 'intent dari model diterapkan'
	};

	const EXAMPLES = [
		'produk mana yang stoknya menipis?',
		'penjualan roti tertinggi',
		'sayur diskon',
		'kopi paling mahal',
		'camilan harga murah'
	];

	/* ------------------------------------------------------------------- state */

	let query = $state('produk mana yang stoknya menipis?');
	let data = $state<ExploreResponse | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let updatedAt = $state<number | null>(null);

	let laya = $state<ExploreInterpretResponse | null>(null);
	/** The exact query text the advisory was computed for; anything else is stale. */
	let layaFor = $state<string | null>(null);
	let layaLoading = $state(false);
	let layaQueued = $state(false);
	let layaError = $state<string | null>(null);
	let layaElapsed = $state(0);

	/* -------------------------------------------------------- non-reactive refs */

	let fastSeq = 0;
	let layaSeq = 0;
	let lastFastAt = 0;
	let fastAbort: AbortController | null = null;
	let layaAbort: AbortController | null = null;
	let layaTicker: ReturnType<typeof setInterval> | null = null;
	let layaTimer: ReturnType<typeof setTimeout> | null = null;

	/* ------------------------------------------------------------------ derive */

	const trimmed = $derived(query.trim());
	const interpretation = $derived(data?.interpretation ?? null);
	const rows = $derived(data?.page.items ?? []);
	const totals = $derived(data?.page.totals ?? null);

	const advisory = $derived(laya !== null && layaFor === trimmed ? laya : null);
	const advisoryResult = $derived<LayaResult | null>(advisory?.laya ?? null);
	const advisoryRows = $derived(advisory?.page.items ?? []);
	const advisoryIntent = $derived<ExploreIntent | null>(advisory?.interpretation.intent ?? null);
	const advisorySource = $derived<ExploreSource | null>(advisory?.interpretation.source ?? null);

	/** The model's own intent pick, found by value so no question name is assumed. */
	const modelIntent = $derived.by(() => {
		const answers = advisoryResult?.answers ?? {};
		for (const [name, answer] of Object.entries(answers)) {
			if (isIntent(answer.answer)) return { name, chosen: answer.answer, answer };
		}
		return null;
	});

	const overrideNote = $derived.by(() => {
		if (!advisory || advisorySource === null) return null;
		if (advisorySource === 'explicit') {
			if (modelIntent && advisoryIntent && modelIntent.chosen !== advisoryIntent) {
				return `frasa eksplisit diutamakan: model memilih ${INTENT_LABEL[modelIntent.chosen]}, pertanyaan Anda meminta ${INTENT_LABEL[advisoryIntent]}`;
			}
			if (modelIntent) return `frasa eksplisit ditemukan; model juga memilih ${INTENT_LABEL[modelIntent.chosen]}`;
			return 'frasa eksplisit ditemukan; model tidak memilih intent';
		}
		if (advisorySource === 'laya') {
			return 'tidak ada frasa eksplisit; API memakai intent dari model';
		}
		return 'tidak ada intent dari frasa atau model; API memakai bawaan';
	});

	/* ------------------------------------------------------------------ helpers */

	function isIntent(value: unknown): value is ExploreIntent {
		return typeof value === 'string' && (INTENTS as string[]).includes(value);
	}

	function describeError(cause: unknown): string {
		if (isApiError(cause)) {
			return cause.status === 0 ? `galat jaringan: ${cause.detail}` : `${cause.status} ${cause.detail}`;
		}
		return cause instanceof Error ? cause.message : String(cause);
	}

	function sortedProbabilities(answer: LayaAnswer | undefined | null): [string, number][] {
		if (!answer?.probabilities) return [];
		return Object.entries(answer.probabilities).sort((left, right) => right[1] - left[1]);
	}

	function barWidth(probability: number): string {
		return `${Math.max(1, Math.min(100, probability * 100))}%`;
	}

	function rowKey(row: SearchRow): string {
		return `${row.day}:${row.store_id}:${row.product_id}`;
	}

	/* --------------------------------------------------------- live SQL layer */

	async function runExplore() {
		const text = trimmed;
		if (text === '') {
			fastAbort?.abort();
			++fastSeq;
			data = null;
			error = null;
			loading = false;
			updatedAt = null;
			return;
		}
		fastAbort?.abort();
		const controller = new AbortController();
		fastAbort = controller;
		const token = ++fastSeq;
		lastFastAt = Date.now();
		loading = true;
		error = null;
		try {
			const response = await explore({ query: text, limit: ROWS }, controller.signal);
			if (controller.signal.aborted || token !== fastSeq) return;
			data = response;
			updatedAt = Date.now();
			loading = false;
		} catch (cause) {
			if (controller.signal.aborted || token !== fastSeq) return;
			loading = false;
			error = describeError(cause);
		}
	}

	let debounceTimer: ReturnType<typeof setTimeout> | null = null;

	/** SQL results arrive first; the model waits until typing pauses. */
	function scheduleExplore() {
		if (debounceTimer !== null) clearTimeout(debounceTimer);
		debounceTimer = setTimeout(() => {
			debounceTimer = null;
			void runExplore();
		}, 350);
	}
	function scheduleLaya() {
		if (layaTimer !== null) clearTimeout(layaTimer);
		if (trimmed === '') return;
		layaQueued = true;
		layaTimer = setTimeout(() => {
			layaTimer = null;
			void runLaya();
		}, 900);
	}


	function onQueryInput() {
		// The advisory belongs to the text it was computed from, so any edit retires it.
		cancelLaya();
		fastAbort?.abort();
		++fastSeq;
		data = null;
		loading = false;
		scheduleExplore();
		scheduleLaya();
	}

	function submit() {
		if (debounceTimer !== null) {
			clearTimeout(debounceTimer);
			debounceTimer = null;
		}
		cancelLaya();
		void runExplore();
		void runLaya();
	}

	function useExample(example: string) {
		query = example;
		submit();
	}

	/* ------------------------------------------------------------ Laya layer */

	function cancelLaya() {
		if (layaTimer !== null) clearTimeout(layaTimer);
		layaTimer = null;
		layaQueued = false;
		layaAbort?.abort();
		layaAbort = null;
		if (layaTicker !== null) {
			clearInterval(layaTicker);
			layaTicker = null;
		}
		layaSeq += 1;
		layaLoading = false;
		laya = null;
		layaFor = null;
		layaError = null;
		layaElapsed = 0;
	}

	async function runLaya() {
		const text = trimmed;
		if (text === '') return;
		if (layaTimer !== null) clearTimeout(layaTimer);
		layaTimer = null;
		layaQueued = false;
		layaAbort?.abort();
		if (layaTicker !== null) clearInterval(layaTicker);
		const controller = new AbortController();
		layaAbort = controller;
		const token = ++layaSeq;
		layaLoading = true;
		layaError = null;
		laya = null;
		layaFor = null;
		layaElapsed = 0;
		const startedAt = Date.now();
		// Local handle: a superseded run must not clear the newer run's ticker.
		const ticker = setInterval(() => {
			layaElapsed = Math.round((Date.now() - startedAt) / 1000);
		}, 250);
		layaTicker = ticker;
		try {
			const response = await interpretExplore({ query: text, limit: ROWS }, controller.signal);
			if (controller.signal.aborted || token !== layaSeq) return;
			laya = response;
			layaFor = text;
			layaLoading = false;
		} catch (cause) {
			if (controller.signal.aborted || token !== layaSeq) return;
			layaLoading = false;
			layaError = describeError(cause);
		} finally {
			clearInterval(ticker);
			if (layaTicker === ticker) layaTicker = null;
		}
	}

	/* ------------------------------------------------------- tick refresh path */

	/**
	 * Live ticks refresh the SQL layer only, on a throttle, and immediately when
	 * the simulator's day moves past the day the rows were read from — that is the
	 * one tick that actually changes the answer. Never a model call.
	 */
	$effect(() => {
		const tick = liveTick;
		const day = liveDay;
		untrack(() => {
			if (tick <= 0) return;
			if (trimmed === '') return;
			const at = Date.now();
			if (at - lastFastAt < MIN_REFRESH_GAP_MS) return;
			const dayMoved = day !== null && data !== null && day !== data.day;
			if (dayMoved || at - lastFastAt > REFRESH_MS) void runExplore();
		});
	});

	$effect(() => {
		// Mount-only kick. Deliberately untracked: `runExplore` reads the current
		// query text, and if the effect tracked it, every keystroke would fire an
		// immediate request and defeat the debounce below.
		untrack(() => {
			void runExplore();
			void runLaya();
		});
		return () => {
			fastAbort?.abort();
			layaAbort?.abort();
			if (debounceTimer !== null) clearTimeout(debounceTimer);
			if (layaTimer !== null) clearTimeout(layaTimer);
			if (layaTicker !== null) clearInterval(layaTicker);
		};
	});
</script>

<section class="panel explore">
	<header>
		<h2>Tanya data pasar</h2>
		<div class="hdr-right mono tiny">
			<span class="tag">bahasa alami</span>
			{#if updatedAt !== null}
				<span class="faint">SQL diperbarui {fmtAgo(new Date(updatedAt).toISOString(), now)}</span>
			{/if}
		</div>
	</header>

	<div class="panel-body">
		<form class="ask" class:thinking={layaLoading || layaQueued} onsubmit={(event) => { event.preventDefault(); submit(); }}>
			<label class="sr-only" for="explore-query">Pertanyaan pencarian</label>
			<input
				id="explore-query"
				class="q"
				type="search"
				autocomplete="off"
				maxlength="200"
				placeholder="Produk mana yang stoknya menipis?"
				bind:value={query}
				oninput={onQueryInput}
			/>
			<button type="submit" class="go" disabled={loading || trimmed === ''}>
				{loading ? 'mencari…' : 'Cari'}
			</button>
			<button
				type="button"
				class="run"
				disabled={layaLoading || trimmed === ''}
				title="Jalankan ulang Laya untuk pertanyaan ini"
				onclick={runLaya}
			>{layaLoading ? 'Laya berjalan…' : 'Jalankan ulang Laya'}</button>
		</form>
		<div class="laya" class:pending={layaLoading || layaQueued}>
			<div class="laya-head">
				<span class="label">Model Laya <span class="faint">menafsirkan setelah Anda berhenti mengetik · saran</span></span>
				{#if advisoryResult?.available && advisoryResult.latency_ms !== null}
					<span class="mono tiny faint">
						{advisoryResult.routing?.model ?? 'model'} · {Math.round(advisoryResult.latency_ms)} ms
					</span>
				{/if}
			</div>

			{#if layaQueued}
				<p class="state pending mono tiny" role="status">menunggu Anda selesai mengetik sebelum menjalankan Laya…</p>
			{:else if layaLoading}
				<p class="state pending mono tiny" role="status">
					menjalankan model di CPU… {layaElapsed} dtk
					<span class="faint">(tanpa GPU; dapat memakan waktu beberapa detik)</span>
				</p>
			{:else if layaError}
				<p class="state bad mono tiny">{layaError}</p>
			{:else if advisoryResult && !advisoryResult.available}
				<p class="state bad mono tiny">
					model tidak tersedia: {advisoryResult.detail ?? 'tanpa keterangan'}
					<span class="faint">hasil SQL langsung tetap tersedia di bawah</span>
				</p>
			{:else if advisory && advisoryResult}
				<p class="state mono tiny">
					{#if advisoryIntent}
						<span class="intent">{INTENT_LABEL[advisoryIntent]}</span>
						<span class="faint">· sumber yang diterapkan</span>
						<span class="src src-{advisorySource}">{advisorySource ? SOURCE_LABEL[advisorySource] : '—'}</span>
					{:else}
						<span class="faint">model tidak memilih intent</span>
					{/if}
				</p>

				{#if overrideNote}
					<p class="state note tiny">{overrideNote}</p>
				{/if}

				{#each Object.entries(advisoryResult.answers) as [name, answer] (name)}
					<div class="answer">
						<div class="answer-head">
							<span class="q">{name === 'intent' ? 'maksud pertanyaan' : name.replace(/_/g, ' ')}</span>
							<span class="chosen mono">{isIntent(answer.answer) ? INTENT_LABEL[answer.answer] : String(answer.answer ?? '—')}</span>
							<span class="conf mono tiny faint">
								p={answer.confidence === null ? '—' : answer.confidence.toFixed(3)}
							</span>
						</div>
						{#if answer.score_position !== null}
							<span class="mono tiny faint">posisi rubrik {answer.score_position.toFixed(3)}</span>
						{/if}
						<ul class="probs">
							{#each sortedProbabilities(answer) as [option, probability] (option)}
								<li class:chosen={option === String(answer.answer)}>
									<span class="opt mono tiny">{isIntent(option) ? INTENT_LABEL[option] : option}</span>
									<span class="track"><span class="fill" style:width={barWidth(probability)}></span></span>
									<span class="val mono tiny">{fmtPct(probability)}</span>
								</li>
							{/each}
						</ul>
					</div>
				{/each}

				<p class="state faint tiny">
					Skor keyakinan dari model belum dikalibrasi untuk data pasar ini.
				</p>

				{#if advisoryRows.length > 0}
					<details class="model-view">
						<summary class="mono tiny">
							hasil berdasarkan intent model: {fmtInt(advisory?.page.total ?? 0)} baris pada {advisory?.day}
							{#if advisoryIntent !== interpretation?.intent}
								<span class="differs">· berbeda dari hasil SQL langsung ({interpretation?.intent ? INTENT_LABEL[interpretation.intent] : '—'})</span>
							{/if}
						</summary>
						<table class="mini">
							<thead>
								<tr>
									<th>toko</th>
									<th>produk</th>
									<th class="num">terjual</th>
									<th class="num">stok</th>
									<th class="num">harga</th>
								</tr>
							</thead>
							<tbody>
								{#each advisoryRows.slice(0, 8) as row (rowKey(row))}
									<tr
										tabindex="0"
										onclick={() => onSelectProduct?.(row.product_id)}
										onkeydown={(event) => {
											if (event.key === 'Enter' || event.key === ' ') {
												event.preventDefault();
												onSelectProduct?.(row.product_id);
											}
										}}
									>
										<td class="trunc" title={row.store_name}>{row.store_name}</td>
										<td class="trunc" title={`${row.sku} ${row.product}`}>{row.product}</td>
										<td class="num">{fmtInt(row.units_sold)}</td>
										<td class="num">{fmtInt(row.inventory)}</td>
										<td class="num">{fmtPrice(row.price)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</details>
				{/if}
			{:else}
				<p class="state faint tiny">Ketik pertanyaan untuk menjalankan Laya; hasil SQL muncul lebih dulu.</p>
			{/if}
		</div>

		<div class="examples">
			<span class="label">contoh</span>
			{#each EXAMPLES as example (example)}
				<button type="button" class="chip" onclick={() => useExample(example)}>{example}</button>
			{/each}
		</div>

		<p class="status mono tiny" aria-live="polite">
			{#if interpretation}
				<span class="intent">{INTENT_LABEL[interpretation.intent]}</span>
				<span class="faint">· {INTENT_GLOSS[interpretation.intent]}</span>
				<span class="faint">· istilah {interpretation.product_term ?? 'tidak ada'}</span>
				<span class="src src-{interpretation.source}" title={SOURCE_NOTE[interpretation.source]}>{SOURCE_LABEL[interpretation.source]}</span>
				<span class="faint">· hari {data?.day ?? '—'} · {fmtInt(rows.length)} dari {fmtInt(data?.page.total ?? 0)} baris</span>
			{:else if loading}
				<span class="faint">mencari…</span>
			{:else}
				<span class="faint">Ketik pertanyaan atau pilih contoh.</span>
			{/if}
		</p>

		<p class="layer label">
			SQL langsung <span class="faint">deterministik · diperbarui oleh tick · tanpa model</span>
		</p>

		{#if error}
			<p class="error">{error}</p>
		{:else if rows.length > 0}
			<div class="scroll table-wrap">
				<table>
					<caption class="sr-only">
						Hasil SQL langsung untuk pertanyaan ini, hanya hari data terbaru.
					</caption>
					<thead>
						<tr>
							<th>hari</th>
							<th>toko</th>
							<th>produk</th>
							<th>merek</th>
							<th class="num">harga</th>
							<th class="num">terjual</th>
							<th class="num">margin</th>
							<th>promo</th>
							<th class="num">stok</th>
						</tr>
					</thead>
					<tbody>
						{#each rows as row (rowKey(row))}
							<tr
								class:selected={row.product_id === selectedProductId}
								tabindex="0"
								onclick={() => onSelectProduct?.(row.product_id)}
								onkeydown={(event) => {
									if (event.key === 'Enter' || event.key === ' ') {
										event.preventDefault();
										onSelectProduct?.(row.product_id);
									}
								}}
							>
								<td class="mono">{fmtDay(row.day)}</td>
								<td class="trunc" title={`${row.store_name} (${row.region} / ${row.format})`}>{row.store_name}</td>
								<td class="trunc" title={`${row.sku} ${row.product}`}>{row.product}</td>
								<td class="trunc" title={row.brand}>{row.brand}</td>
								<td class="num">{fmtPrice(row.price)}</td>
								<td class="num">{fmtInt(row.units_sold)}</td>
								<td class="num margin-{marginBand(row.margin_pct)}">{fmtPct(row.margin_pct)}</td>
								<td class="promo-cell">
									{#if row.promo_flag}<span class="badge promo">promo</span>{:else}<span class="faint">—</span>{/if}
								</td>
								<td class="num">{fmtInt(row.inventory)}</td>
							</tr>
						{/each}
					</tbody>
					{#if totals}
						<tfoot>
							<tr>
								<td colspan="5" class="faint">
									{fmtInt(totals.rows)} baris · semua yang cocok
									{#if (data?.page.total ?? 0) > rows.length}
										· menampilkan {fmtInt(rows.length)} hasil pertama
									{/if}
								</td>
								<td class="num">{fmtInt(totals.units)}</td>
								<td class="num margin-{marginBand(totals.margin_pct)}">{fmtPct(totals.margin_pct)}</td>
								<td colspan="2" class="num">{fmtCompact(totals.revenue)}</td>
							</tr>
						</tfoot>
					{/if}
				</table>
			</div>
		{:else}
			<p class="empty">
				{loading ? 'mencari data pasar…' : 'tidak ada baris yang cocok; coba istilah lain'}
			</p>
		{/if}

		<p class="footnote faint tiny">
			Klik baris untuk membuka rincian produk. Stok menunjukkan jumlah per toko,
			bukan jumlah total per produk.
		</p>

	</div>
</section>

<style>
	.explore {
		border-color: var(--accent);
		box-shadow: inset 3px 0 0 var(--accent);
	}

	.explore > header {
		background: var(--accent-soft);
		border-bottom-color: var(--accent);
	}

	.explore > header h2 {
		color: var(--accent);
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.tag {
		font-size: var(--fs-xs);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--accent);
		border: 1px solid var(--accent);
		border-radius: 999px;
		padding: 0 6px;
	}

	.panel-body {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}

	.ask {
		display: flex;
		gap: 6px;
		align-items: stretch;
	}

	.ask.thinking {
		position: relative;
	}

	.ask.thinking::after {
		content: '';
		position: absolute;
		inset: -3px;
		border: 2px solid var(--accent);
		border-radius: 5px;
		box-shadow: 0 0 12px var(--accent);
		pointer-events: none;
		animation: query-glow 1.8s ease-in-out infinite;
	}

	@keyframes query-glow {
		0%, 100% { opacity: 0.25; }
		50% { opacity: 0.85; }
	}

	@media (prefers-reduced-motion: reduce) {
		.ask.thinking::after { animation: none; opacity: 0.7; }
	}

	.q {
		flex: 1 1 auto;
		padding: 4px 8px;
		font-size: var(--fs-md);
		min-width: 0;
	}

	.go {
		flex: 0 0 auto;
		border-color: var(--accent);
		color: var(--accent);
	}

	.run {
		flex: 0 0 auto;
	}

	.examples {
		display: flex;
		align-items: center;
		gap: 5px;
		flex-wrap: wrap;
	}

	.status {
		display: flex;
		align-items: baseline;
		gap: 5px;
		flex-wrap: wrap;
		margin: 0;
	}

	.intent {
		color: var(--accent);
		font-weight: 600;
	}

	.src {
		font-size: var(--fs-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		border: 1px solid var(--border);
		border-radius: 2px;
		padding: 0 4px;
	}

	.src-explicit {
		color: var(--ok);
		border-color: var(--ok);
	}

	.src-laya {
		color: var(--accent);
		border-color: var(--accent);
	}

	.src-default {
		color: var(--text-faint);
	}

	.layer {
		margin: 2px 0 0;
		color: var(--text-dim);
	}

	.table-wrap {
		max-height: 34vh;
		border: 1px solid var(--border);
		border-radius: var(--radius);
	}

	table {
		font-size: var(--fs-sm);
	}

	tbody tr {
		height: var(--row-h);
		cursor: pointer;
	}

	tbody tr:hover {
		background: var(--bg-hover);
	}

	tbody tr.selected {
		background: var(--bg-active);
	}

	tbody tr:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}

	.trunc {
		max-width: 190px;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.promo-cell {
		text-align: center;
	}

	tfoot td {
		position: sticky;
		bottom: 0;
		background: var(--bg-sunken);
		border-top: 1px solid var(--border-strong);
		border-bottom: none;
		font-weight: 600;
	}

	.footnote {
		margin: 0;
	}

	.laya {
		margin-top: 3px;
		border-top: 1px dashed var(--border-strong);
		padding-top: 6px;
	}

	.laya-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		flex-wrap: wrap;
	}

	.state {
		margin: 3px 0 0;
		line-height: 1.45;
	}

	.state.pending {
		color: var(--text-dim);
	}

	.state.bad {
		color: var(--warn);
	}

	.note {
		color: var(--text-dim);
	}

	.answer {
		margin: 5px 0 0;
	}

	.answer-head {
		display: flex;
		align-items: baseline;
		gap: 8px;
		flex-wrap: wrap;
	}

	.answer-head .q {
		font-size: var(--fs-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-faint);
		flex: 0 0 auto;
	}

	.chosen {
		color: var(--accent);
		font-size: var(--fs-sm);
	}

	.probs {
		list-style: none;
		margin: 3px 0 0;
		padding: 0;
	}

	.probs li {
		display: grid;
		grid-template-columns: minmax(80px, 24%) 1fr auto;
		align-items: center;
		gap: 6px;
		padding: 1px 0;
	}

	.opt {
		color: var(--text-faint);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.probs li.chosen .opt {
		color: var(--text);
	}

	.track {
		display: block;
		height: 6px;
		background: var(--bg-sunken);
		border-radius: 3px;
		overflow: hidden;
	}

	.fill {
		display: block;
		height: 100%;
		background: var(--border-strong);
	}

	.probs li.chosen .fill {
		background: var(--accent);
	}

	.val {
		color: var(--text-dim);
		min-width: 42px;
		text-align: right;
	}

	.model-view {
		margin-top: 5px;
	}

	.model-view summary {
		cursor: pointer;
		color: var(--text-dim);
	}

	.differs {
		color: var(--warn);
	}

	.mini {
		margin-top: 4px;
		font-size: var(--fs-xs);
	}

	.mini tbody tr {
		height: var(--row-h);
		cursor: pointer;
	}

	.tiny {
		font-size: var(--fs-xs);
	}

	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
