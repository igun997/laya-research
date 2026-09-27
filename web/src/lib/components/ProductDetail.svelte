<script lang="ts">
	import { getProductSeries, isApiError } from '$lib/api';
	import { fmtCompact, fmtDay, fmtInt, fmtPct, fmtPrice, marginBand } from '$lib/format';
	import Sparkline from './Sparkline.svelte';
	import LayaVerdict from './LayaVerdict.svelte';
	import type { ProductSeriesResponse } from '$lib/types';

	interface Props {
		productId: number;
		/** Days of history to request (GET /api/products/{id}/series?days=N). */
		days?: number;
		onClose?: () => void;
	}

	let { productId, days = 30, onClose }: Props = $props();

	let data = $state<ProductSeriesResponse | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);

	// Client-side fetch only: never runs during SSR (ssr = false, and this is an effect).
	$effect(() => {
		const id = productId;
		const controller = new AbortController();
		loading = true;
		error = null;
		getProductSeries(id, days, controller.signal)
			.then((response) => {
				data = response;
				loading = false;
			})
			.catch((cause: unknown) => {
				if (controller.signal.aborted) return;
				loading = false;
				error = isApiError(cause) ? cause.detail : String(cause);
			});
		return () => controller.abort();
	});

	const series = $derived(data?.series ?? []);
	const latest = $derived(series.length > 0 ? series[series.length - 1] : null);
	const unitsValues = $derived(series.map((point) => point.units));
	const priceValues = $derived(series.map((point) => point.avg_price));
	const inventoryValues = $derived(series.map((point) => point.inventory));
	const promoValues = $derived(series.map((point) => point.promo_stores));

	const priceChange = $derived.by(() => {
		if (series.length < 2) return null;
		const first = series[0].avg_price;
		if (first === 0) return null;
		return (series[series.length - 1].avg_price - first) / first;
	});

	const unitsChange = $derived.by(() => {
		if (series.length < 2) return null;
		const first = series[0].units;
		if (first === 0) return null;
		return (series[series.length - 1].units - first) / first;
	});
</script>

<aside class="panel detail">
	<header>
		<h2>product</h2>
		<div class="hdr-right">
			{#if data}
				<span class="mono tiny faint">#{data.product.product_id} {data.product.sku}</span>
			{/if}
			<button type="button" class="tiny" onclick={() => onClose?.()} title="close detail">✕</button>
		</div>
	</header>

	<div class="panel-body">
		{#if error}
			<p class="error">{error}</p>
		{:else if !data}
			<p class="empty">{loading ? 'loading series…' : 'no product selected'}</p>
		{:else}
			<div class="head">
				<h3 title={data.product.name}>{data.product.name}</h3>
				<p class="sub faint">
					{data.product.brand} · {data.product.category} / {data.product.subcategory} ·
					{data.product.uom}
					{#if data.product.pack_size !== 1}
						· pack {data.product.pack_size}
					{/if}
				</p>
				<p class="tags">
					{#if data.product.is_private_label}<span class="badge sev-info">private label</span>{/if}
					{#if data.product.is_perishable}<span class="badge sev-warn">perishable</span>{/if}
					<span class="badge faint">list {fmtPrice(data.product.list_price)}</span>
				</p>
			</div>

			<div class="kpis">
				<div class="kpi">
					<span class="label">price</span>
					<span class="mono big">{latest ? fmtPrice(latest.avg_price) : fmtPrice(data.product.latest_price)}</span>
					{#if priceChange !== null}
						<span class="mono tiny" class:up={priceChange >= 0} class:down={priceChange < 0}>
							{priceChange >= 0 ? '+' : ''}{(priceChange * 100).toFixed(1)}%
						</span>
					{/if}
				</div>
				<div class="kpi">
					<span class="label">units</span>
					<span class="mono big">{latest ? fmtInt(latest.units) : fmtInt(data.product.latest_units)}</span>
					{#if unitsChange !== null}
						<span class="mono tiny" class:up={unitsChange >= 0} class:down={unitsChange < 0}>
							{unitsChange >= 0 ? '+' : ''}{(unitsChange * 100).toFixed(1)}%
						</span>
					{/if}
				</div>
				<div class="kpi">
					<span class="label">margin</span>
					<span class="mono big margin-{marginBand(latest?.margin_pct ?? data.product.latest_margin_pct)}">
						{fmtPct(latest ? latest.margin_pct : data.product.latest_margin_pct)}
					</span>
				</div>
				<div class="kpi">
					<span class="label">inventory</span>
					<span class="mono big">{latest ? fmtInt(latest.inventory) : '—'}</span>
				</div>
			</div>

			<div class="chart">
				<div class="chart-head">
					<span class="label">units / day</span>
					<span class="mono tiny faint">
						{series.length > 0 ? `${fmtDay(series[0].day)} → ${fmtDay(series[series.length - 1].day)}` : '—'}
					</span>
				</div>
				<Sparkline
					values={unitsValues}
					zeroBased
					label="units"
					format={(value) => fmtInt(value)}
				/>
			</div>

			<div class="chart">
				<div class="chart-head">
					<span class="label">avg price / day</span>
					<span class="mono tiny faint">
						{latest ? `store mean over ${fmtInt(latest.store_count)} stores` : '—'}
					</span>
				</div>
				<Sparkline
					values={priceValues}
					label="avg price"
					format={(value) => value.toFixed(2)}
				/>
			</div>

			<div class="chart">
				<div class="chart-head">
					<span class="label">inventory / day</span>
					<span class="mono tiny faint">stockout when cover &lt; 1.2 days</span>
				</div>
				<Sparkline
					values={inventoryValues}
					zeroBased
					area={false}
					height={30}
					label="inventory"
					format={(value) => fmtInt(value)}
				/>
			</div>

			<div class="chart">
				<div class="chart-head">
					<span class="label">promo stores / day</span>
					<span class="mono tiny faint">
						{latest ? `${fmtPct(latest.promo_stores / Math.max(1, latest.store_count))} of stores` : '—'}
					</span>
				</div>
				<Sparkline
					values={promoValues}
					zeroBased
					area={false}
					height={30}
					label="promo stores"
					format={(value) => fmtInt(value)}
				/>
			</div>

			<table class="recent">
				<thead>
					<tr>
						<th>day</th>
						<th class="num">units</th>
						<th class="num">price</th>
						<th class="num">revenue</th>
						<th class="num">margin</th>
						<th class="num">inv</th>
					</tr>
				</thead>
				<tbody>
					{#each [...series].reverse().slice(0, 10) as point (point.day)}
						<tr>
							<td class="mono">{fmtDay(point.day)}</td>
							<td class="num">{fmtInt(point.units)}</td>
							<td class="num">{fmtPrice(point.avg_price)}</td>
							<td class="num">{fmtCompact(point.revenue)}</td>
							<td class="num margin-{marginBand(point.margin_pct)}">{fmtPct(point.margin_pct)}</td>
							<td class="num">{fmtInt(point.inventory)}</td>
						</tr>
					{/each}
					{#if series.length === 0}
						<tr><td colspan="6" class="empty">no series rows</td></tr>
					{/if}
				</tbody>
			</table>
		{/if}

		<LayaVerdict {productId} />
	</div>
</aside>

<style>
	.detail {
		min-height: 0;
		overflow: hidden;
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.panel-body {
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	.head h3 {
		font-size: var(--fs-lg);
		line-height: 1.2;
	}

	.sub {
		margin: 1px 0 0;
		font-size: var(--fs-sm);
	}

	.tags {
		display: flex;
		gap: 4px;
		flex-wrap: wrap;
		margin: 3px 0 0;
	}

	.kpis {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 4px 10px;
		padding: 5px 0;
		border-top: 1px solid var(--border);
		border-bottom: 1px solid var(--border);
	}

	.kpi {
		display: flex;
		align-items: baseline;
		gap: 5px;
		min-width: 0;
	}

	.big {
		font-size: var(--fs-lg);
		font-weight: 600;
	}

	.chart-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 6px;
	}

	.up {
		color: var(--ok);
	}

	.down {
		color: var(--critical);
	}

	.recent {
		font-size: var(--fs-xs);
	}

	.recent tr {
		height: var(--row-h);
	}

	.tiny {
		font-size: var(--fs-xs);
	}
</style>
