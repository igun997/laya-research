<script lang="ts">
	import { fmtAgo, fmtCompact, fmtDay, fmtInt, fmtPct, fmtRatio, fmtSignedPct, marginBand } from '$lib/format';
	import type { OverviewResponse } from '$lib/types';

	interface Props {
		overview: OverviewResponse | null;
		loading: boolean;
		error: string | null;
		now: number;
		days: number;
		onDays?: (days: number) => void;
		onProductClick?: (productId: number) => void;
	}

	let { overview, loading, error, now, days, onDays, onProductClick }: Props = $props();

	const DAY_OPTIONS = [7, 14, 21, 30];

	const dayRows = $derived(overview?.days ?? []);
	const categories = $derived(overview?.categories ?? []);
	const movers = $derived(overview?.movers ?? []);

	const maxUnits = $derived(Math.max(1, ...dayRows.map((row) => row.units)));
	const maxRevenue = $derived(Math.max(1, ...dayRows.map((row) => row.revenue)));

	/** Bars are drawn newest-first; the array order from the API is chronological. */
	const bars = $derived([...dayRows].reverse());

	const latestDay = $derived(dayRows.length > 0 ? dayRows[dayRows.length - 1] : null);
	const prevDay = $derived(dayRows.length > 1 ? dayRows[dayRows.length - 2] : null);

	const unitsDod = $derived.by(() => {
		if (!latestDay || !prevDay || prevDay.units === 0) return null;
		return latestDay.units / prevDay.units - 1;
	});
	const revenueDod = $derived.by(() => {
		if (!latestDay || !prevDay || prevDay.revenue === 0) return null;
		return latestDay.revenue / prevDay.revenue - 1;
	});

	/** rollup staleness — materialized views are refreshed in the background by `sim`. */
	const rollupLagSeconds = $derived.by(() => {
		if (!overview) return null;
		const then = new Date(overview.rollups_as_of).getTime();
		if (Number.isNaN(then)) return null;
		return Math.max(0, (now - then) / 1000);
	});
</script>

<section class="panel overview">
	<header>
		<h2>Ringkasan pasar</h2>
		<div class="hdr-right">
			{#if overview}
				<span class="mono tiny faint" title={`rollups_as_of ${overview.rollups_as_of}`}>
					ringkasan {fmtAgo(overview.rollups_as_of, now)}
				</span>
			{/if}
			<span class="chips">
				{#each DAY_OPTIONS as option (option)}
					<button
						type="button"
						class="chip"
						aria-pressed={days === option}
						onclick={() => onDays?.(option)}
					>{option} hari</button>
				{/each}
			</span>
		</div>
	</header>

	<div class="panel-body">
		{#if error}
			<p class="error">{error}</p>
		{:else if !overview}
			<p class="empty">{loading ? 'memuat ringkasan…' : 'belum ada ringkasan'}</p>
		{:else}
			{#if rollupLagSeconds !== null && rollupLagSeconds > 60}
				<p class="stale-note">
					ringkasan tertinggal {Math.round(rollupLagSeconds)} dtk dari tick langsung.
					Diagram harian dan tabel kategori memakai ringkasan; produk teratas memakai data langsung.
				</p>
			{/if}

			<div class="kpis">
				<div class="kpi">
					<span class="label">hari terbaru</span>
					<span class="mono">{latestDay ? latestDay.day : '—'}</span>
				</div>
				<div class="kpi">
					<span class="label">unit terjual</span>
					<span class="mono">{latestDay ? fmtInt(latestDay.units) : '—'}</span>
					{#if unitsDod !== null}
						<span class="mono dod" class:up={unitsDod >= 0} class:down={unitsDod < 0}>{fmtSignedPct(unitsDod)}</span>
					{/if}
				</div>
				<div class="kpi">
					<span class="label">pendapatan</span>
					<span class="mono">{latestDay ? fmtCompact(latestDay.revenue) : '—'}</span>
					{#if revenueDod !== null}
						<span class="mono dod" class:up={revenueDod >= 0} class:down={revenueDod < 0}>{fmtSignedPct(revenueDod)}</span>
					{/if}
				</div>
				<div class="kpi">
					<span class="label">margin</span>
					<span class="mono margin-{marginBand(latestDay?.margin_pct)}">
						{latestDay ? fmtPct(latestDay.margin_pct) : '—'}
					</span>
				</div>
				<div class="kpi">
					<span class="label">porsi merek toko</span>
					<span class="mono">{latestDay ? fmtPct(latestDay.pl_share) : '—'}</span>
				</div>
				<div class="kpi">
					<span class="label">diperbarui</span>
					<span class="mono faint">{fmtAgo(overview.as_of, now)}</span>
				</div>
			</div>

			<div class="grid">
				<div class="block">
					<h3>Penjualan per hari</h3>
					<div class="bars">
						{#each bars as row (row.day)}
							<div class="bar-row">
								<span class="bar-day mono">{fmtDay(row.day)}</span>
								<span class="bar-track" title={`unit terjual ${fmtInt(row.units)}`}>
									<span class="bar bar-units" style={`width:${((row.units / maxUnits) * 100).toFixed(2)}%`}></span>
								</span>
								<span class="bar-val mono">{fmtCompact(row.units)}</span>
								<span class="bar-track" title={`pendapatan ${fmtCompact(row.revenue)}`}>
									<span class="bar bar-rev" style={`width:${((row.revenue / maxRevenue) * 100).toFixed(2)}%`}></span>
								</span>
								<span class="bar-val mono">{fmtCompact(row.revenue)}</span>
								<span class="bar-margin mono margin-{marginBand(row.margin_pct)}">{fmtPct(row.margin_pct)}</span>
							</div>
						{/each}
					</div>
					<p class="legend faint">
						<span class="key key-units"></span>unit terjual
						<span class="key key-rev"></span>pendapatan
					</p>
				</div>

				<div class="block">
					<h3>Produk yang naik <span class="faint">berdasarkan kenaikan</span></h3>
					{#if movers.length === 0}
						<p class="empty">belum ada produk dengan pembanding yang valid</p>
					{:else}
						<table class="movers">
							<thead>
								<tr>
									<th>produk</th>
									<th>kategori</th>
									<th class="num">terjual</th>
									<th class="num">pembanding</th>
									<th class="num">kenaikan</th>
									<th class="num">pendapatan</th>
								</tr>
							</thead>
							<tbody>
								{#each movers as mover (mover.product_id)}
									<tr
										tabindex="0"
										onclick={() => onProductClick?.(mover.product_id)}
										onkeydown={(event) => {
											if (event.key === 'Enter' || event.key === ' ') {
												event.preventDefault();
												onProductClick?.(mover.product_id);
											}
										}}
									>
										<td class="trunc" title={mover.name}>{mover.name}</td>
										<td class="trunc faint" title={mover.category}>{mover.category}</td>
										<td class="num">{fmtInt(mover.units)}</td>
										<td class="num faint">{fmtInt(mover.baseline)}</td>
										<td class="num lift">{fmtRatio(mover.lift)}</td>
										<td class="num">{fmtCompact(mover.revenue)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					{/if}
				</div>
			</div>

			<div class="block">
				<h3>Kategori <span class="faint">{fmtInt(categories.length)}</span></h3>
				<div class="cat-scroll">
					<table class="cats">
						<thead>
							<tr>
								<th>kategori</th>
								<th class="num">terjual</th>
								<th class="num">pendapatan</th>
								<th class="num">margin</th>
								<th class="num">porsi merek toko</th>
								<th class="num">perubahan harian</th>
							</tr>
						</thead>
						<tbody>
							{#each categories as category (category.category)}
								<tr>
									<td class="trunc" title={category.category}>{category.category}</td>
									<td class="num">{fmtInt(category.units)}</td>
									<td class="num">{fmtCompact(category.revenue)}</td>
									<td class="num margin-{marginBand(category.margin_pct)}">{fmtPct(category.margin_pct)}</td>
									<td class="num">
										<span class="pl-bar" title={fmtPct(category.pl_share)}>
											<span class="pl-fill" style={`width:${(Math.min(1, Math.max(0, category.pl_share)) * 100).toFixed(1)}%`}></span>
										</span>
										{fmtPct(category.pl_share, 0)}
									</td>
									<td
										class="num"
										class:up={category.units_dod_pct > 0}
										class:down={category.units_dod_pct < 0}
									>{fmtSignedPct(category.units_dod_pct)}</td>
								</tr>
							{/each}
							{#if categories.length === 0}
								<tr><td colspan="6" class="empty">belum ada ringkasan kategori</td></tr>
							{/if}
						</tbody>
					</table>
				</div>
			</div>
		{/if}
	</div>
</section>

<style>
	.overview {
		min-height: 0;
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.chips {
		display: inline-flex;
		gap: 3px;
	}

	.kpis {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 14px;
		padding-bottom: 5px;
		border-bottom: 1px solid var(--border);
		margin-bottom: 6px;
	}

	.kpi {
		display: flex;
		align-items: baseline;
		gap: 5px;
	}

	.dod {
		font-size: var(--fs-xs);
	}

	.up {
		color: var(--ok);
	}

	.down {
		color: var(--critical);
	}

	.stale-note {
		margin: 0 0 5px;
		padding: 3px 6px;
		font-size: var(--fs-xs);
		color: var(--warn);
		background: var(--warn-soft);
		border: 1px solid var(--warn);
		border-radius: var(--radius);
	}

	.grid {
		display: grid;
		grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.3fr);
		gap: 10px;
	}

	.block h3 {
		font-size: var(--fs-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
		margin-bottom: 3px;
	}

	.bars {
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.bar-row {
		display: grid;
		grid-template-columns: 38px 1fr 46px 1fr 44px;
		align-items: center;
		gap: 4px;
		font-size: var(--fs-xs);
	}

	.bar-day {
		color: var(--text-faint);
	}

	.bar-track {
		display: block;
		height: 9px;
		background: var(--bg-sunken);
		border-radius: 2px;
		overflow: hidden;
	}

	.bar {
		display: block;
		height: 100%;
		min-width: 1px;
	}

	.bar-units {
		background: var(--accent);
	}

	.bar-rev {
		background: var(--ok);
	}

	.bar-val {
		text-align: right;
	}

	.bar-margin {
		text-align: right;
	}

	.legend {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: var(--fs-xs);
		margin: 4px 0 0;
	}

	.key {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: 2px;
		margin-left: 6px;
	}

	.key-units {
		background: var(--accent);
	}

	.key-rev {
		background: var(--ok);
	}

	table {
		font-size: var(--fs-xs);
	}

	.movers tr,
	.cats tr {
		height: var(--row-h);
	}

	.movers tbody tr {
		cursor: pointer;
	}

	.movers tbody tr:hover {
		background: var(--bg-hover);
	}

	.lift {
		color: var(--accent);
		font-weight: 600;
	}

	.cat-scroll {
		max-height: 220px;
		overflow: auto;
	}

	.trunc {
		max-width: 190px;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.pl-bar {
		display: inline-block;
		width: 34px;
		height: 7px;
		background: var(--bg-sunken);
		border-radius: 2px;
		overflow: hidden;
		margin-right: 4px;
		vertical-align: middle;
	}

	.pl-fill {
		display: block;
		height: 100%;
		background: var(--accent);
	}

	@media (max-width: 1180px) {
		.grid {
			grid-template-columns: 1fr;
		}
	}
</style>
