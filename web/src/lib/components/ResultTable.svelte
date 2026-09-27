<script lang="ts">
	import { fmtCompact, fmtDay, fmtInt, fmtPct, fmtPrice, marginBand } from '$lib/format';
	import type { SearchPage, SearchRow, SortKey } from '$lib/types';

	interface Props {
		page: SearchPage | null;
		loading: boolean;
		error: string | null;
		selectedProductId: number | null;
		/** Sort key currently requested from the API (drives the header indicator). */
		activeSort?: SortKey;
		onSort?: (sort: SortKey) => void;
		onPage?: (offset: number) => void;
		onRowClick?: (productId: number) => void;
	}

	let {
		page,
		loading,
		error,
		selectedProductId,
		activeSort = 'revenue',
		onSort,
		onPage,
		onRowClick
	}: Props = $props();

	const COLUMNS: { key: string; label: string; sortable?: SortKey; numeric?: boolean }[] = [
		{ key: 'day', label: 'hari', sortable: 'day' },
		{ key: 'store', label: 'toko' },
		{ key: 'product', label: 'produk' },
		{ key: 'brand', label: 'merek' },
		{ key: 'category', label: 'kategori' },
		{ key: 'price', label: 'harga', sortable: 'price', numeric: true },
		{ key: 'units', label: 'terjual', sortable: 'units', numeric: true },
		{ key: 'revenue', label: 'pendapatan', sortable: 'revenue', numeric: true },
		{ key: 'margin', label: 'margin', sortable: 'margin', numeric: true },
		{ key: 'promo', label: 'promo' },
		{ key: 'inventory', label: 'stok', numeric: true }
	];

	const items = $derived(page?.items ?? []);
	const totals = $derived(page?.totals ?? null);
	const limit = $derived(page?.limit ?? 50);
	const offset = $derived(page?.offset ?? 0);
	const total = $derived(page?.total ?? 0);
	const pageIndex = $derived(limit > 0 ? Math.floor(offset / limit) : 0);
	const pageCount = $derived(limit > 0 ? Math.max(1, Math.ceil(total / limit)) : 1);
	const firstRow = $derived(total === 0 ? 0 : offset + 1);
	const lastRow = $derived(Math.min(offset + items.length, total));

	function headerClick(sortable: SortKey | undefined) {
		if (sortable) onSort?.(sortable);
	}

	function rowActivate(row: SearchRow) {
		onRowClick?.(row.product_id);
	}

	function onRowKey(event: KeyboardEvent, row: SearchRow) {
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			rowActivate(row);
		}
	}
</script>

<section class="panel results">
	<header>
		<h2>Hasil pencarian</h2>
		<div class="hdr-right mono tiny">
			{#if loading}
				<span class="faint">memuat…</span>
			{:else if page}
				<span class="faint">{fmtInt(firstRow)}–{fmtInt(lastRow)} dari {fmtInt(total)}</span>
			{:else}
				<span class="faint">belum ada pencarian</span>
			{/if}
			<span class="pager">
				<button type="button" disabled={pageIndex <= 0} onclick={() => onPage?.(0)} title="halaman pertama">«</button>
				<button
					type="button"
					disabled={pageIndex <= 0}
					onclick={() => onPage?.(Math.max(0, offset - limit))}
					title="halaman sebelumnya"
				>‹</button>
				<span class="faint">hal {fmtInt(pageIndex + 1)}/{fmtInt(pageCount)}</span>
				<button
					type="button"
					disabled={offset + limit >= total}
					onclick={() => onPage?.(offset + limit)}
					title="halaman berikutnya"
				>›</button>
				<button
					type="button"
					disabled={offset + limit >= total}
					onclick={() => onPage?.(Math.max(0, (pageCount - 1) * limit))}
					title="halaman terakhir"
				>»</button>
			</span>
		</div>
	</header>

	{#if error}
		<p class="error panel-body">{error}</p>
	{:else}
		<div class="scroll">
			<table>
				<thead>
					<tr>
						{#each COLUMNS as column (column.key)}
							<th
								class:num={column.numeric}
								class:sortable={Boolean(column.sortable)}
								class:active={column.sortable === activeSort}
								onclick={() => headerClick(column.sortable)}
								aria-sort={column.sortable === activeSort ? 'descending' : 'none'}
							>
								{column.label}{#if column.sortable === activeSort}<span class="arrow">▾</span>{/if}
							</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each items as row (row.day + ':' + row.store_id + ':' + row.product_id)}
						<tr
							class:selected={row.product_id === selectedProductId}
							tabindex="0"
							onclick={() => rowActivate(row)}
							onkeydown={(event) => onRowKey(event, row)}
						>
							<td class="mono">{fmtDay(row.day)}</td>
							<td class="trunc" title={`${row.store_name} (${row.region} / ${row.format})`}>
								{row.store_name}
							</td>
							<td class="trunc" title={`${row.sku} ${row.product}`}>{row.product}</td>
							<td class="trunc" title={row.brand}>{row.brand}</td>
							<td class="trunc" title={row.category}>{row.category}</td>
							<td class="num">{fmtPrice(row.price)}</td>
							<td class="num">{fmtInt(row.units_sold)}</td>
							<td class="num">{fmtCompact(row.revenue)}</td>
							<td class="num margin-{marginBand(row.margin_pct)}">{fmtPct(row.margin_pct)}</td>
							<td class="promo-cell">
								{#if row.promo_flag}<span class="badge promo">promo</span>{:else}<span class="faint">—</span>{/if}
							</td>
							<td class="num">{fmtInt(row.inventory)}</td>
						</tr>
					{/each}
					{#if items.length === 0}
						<tr>
							<td colspan={COLUMNS.length} class="empty">
								{loading ? 'mencari…' : 'tidak ada baris yang cocok dengan filter'}
							</td>
						</tr>
					{/if}
				</tbody>
				{#if totals}
					<tfoot>
						<tr>
							<td colspan="6" class="faint">total (semua hasil, {fmtInt(totals.rows)} baris)</td>
							<td class="num">{fmtInt(totals.units)}</td>
							<td class="num">{fmtCompact(totals.revenue)}</td>
							<td class="num margin-{marginBand(totals.margin_pct)}">{fmtPct(totals.margin_pct)}</td>
							<td colspan="2"></td>
						</tr>
					</tfoot>
				{/if}
			</table>
		</div>
	{/if}
</section>

<style>
	.results {
		min-height: 0;
		flex: 1 1 auto;
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.pager {
		display: inline-flex;
		align-items: center;
		gap: 3px;
	}

	.pager button {
		padding: 0 5px;
		line-height: 1.4;
	}

	.scroll {
		overflow: auto;
		min-height: 0;
		flex: 1 1 auto;
	}

	table {
		font-size: var(--fs-sm);
	}

	th.sortable {
		cursor: pointer;
	}

	th.sortable:hover {
		color: var(--accent);
	}

	th.active {
		color: var(--accent);
	}

	.arrow {
		margin-left: 3px;
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
		max-width: 220px;
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

	.tiny {
		font-size: var(--fs-xs);
	}
</style>
