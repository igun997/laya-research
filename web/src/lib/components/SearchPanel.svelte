<script lang="ts">
	import { fmtInt } from '$lib/format';
	import type { CategoryCount, SearchFilters } from '$lib/types';

	interface Props {
		filters: SearchFilters;
		categories: CategoryCount[];
		brands: string[];
		formats: string[];
		regions: string[];
		dayMin: string;
		dayMax: string;
		loading: boolean;
		total: number;
		rowCount: number;
		onchange?: () => void;
	}

	let {
		filters = $bindable(),
		categories,
		brands,
		formats,
		regions,
		dayMin,
		dayMax,
		loading,
		total,
		rowCount,
		onchange
	}: Props = $props();

	function touch() {
		onchange?.();
	}

	function clearAll() {
		filters.q = '';
		filters.category = '';
		filters.brand = '';
		filters.store_id = '';
		filters.format = '';
		filters.region = '';
		filters.promo = 'any';
		filters.min_price = '';
		filters.max_price = '';
		filters.min_units = '';
		filters.date_from = '';
		filters.date_to = '';
		touch();
	}

	const activeCount = $derived(
		[
			filters.q,
			filters.category,
			filters.brand,
			filters.store_id,
			filters.format,
			filters.region,
			filters.min_price,
			filters.max_price,
			filters.min_units,
			filters.date_from,
			filters.date_to
		].filter((value) => value !== '').length + (filters.promo === 'any' ? 0 : 1)
	);
</script>

<section class="panel search">
	<header>
		<h2>Pencarian terstruktur</h2>
		<div class="hdr-right">
			<span class="faint mono tiny">
				{loading ? 'mencari…' : `${fmtInt(total)} cocok · halaman ${fmtInt(rowCount)}`}
			</span>
			{#if activeCount > 0}
				<button type="button" class="tiny" onclick={clearAll}>hapus filter ({activeCount})</button>
			{/if}
		</div>
	</header>

	<div class="panel-body">
		<div class="row row-q">
			<input
				class="q"
				type="search"
				placeholder="Teks lengkap: milk, SKU-000123, Produce…"
				bind:value={filters.q}
				oninput={touch}
				aria-label="Pencarian teks lengkap"
			/>
		</div>

		<div class="row">
			<label class="field">
				<span class="label">kategori</span>
				<select bind:value={filters.category} onchange={touch}>
					<option value="">semua</option>
					{#each categories as item (item.category)}
						<option value={item.category}>{item.category} ({item.products})</option>
					{/each}
				</select>
			</label>

			<label class="field">
				<span class="label">merek</span>
				<select bind:value={filters.brand} onchange={touch}>
					<option value="">semua</option>
					{#each brands as brand (brand)}
						<option value={brand}>{brand}</option>
					{/each}
				</select>
			</label>

			<label class="field narrow">
				<span class="label">ID toko</span>
				<input
					type="number"
					min="1"
					placeholder="semua"
					bind:value={filters.store_id}
					oninput={touch}
				/>
			</label>
		</div>

		<div class="row">
			<label class="field">
				<span class="label">format</span>
				<select bind:value={filters.format} onchange={touch}>
					<option value="">semua</option>
					{#each formats as format (format)}
						<option value={format}>{format}</option>
					{/each}
				</select>
			</label>

			<label class="field">
				<span class="label">wilayah</span>
				<select bind:value={filters.region} onchange={touch}>
					<option value="">semua</option>
					{#each regions as region (region)}
						<option value={region}>{region}</option>
					{/each}
				</select>
			</label>

			<label class="field">
				<span class="label">promo</span>
				<select bind:value={filters.promo} onchange={touch}>
					<option value="any">semua</option>
					<option value="only">hanya promo</option>
					<option value="exclude">tanpa promo</option>
				</select>
			</label>
		</div>

		<div class="row">
			<label class="field narrow">
				<span class="label">harga minimum</span>
				<input type="number" step="0.01" min="0" placeholder="0" bind:value={filters.min_price} oninput={touch} />
			</label>
			<label class="field narrow">
				<span class="label">harga maksimum</span>
				<input type="number" step="0.01" min="0" placeholder="∞" bind:value={filters.max_price} oninput={touch} />
			</label>
			<label class="field narrow">
				<span class="label">terjual minimum</span>
				<input type="number" min="0" placeholder="0" bind:value={filters.min_units} oninput={touch} />
			</label>
		</div>

		<div class="row">
			<label class="field">
				<span class="label">dari</span>
				<input type="date" min={dayMin} max={dayMax} bind:value={filters.date_from} onchange={touch} />
			</label>
			<label class="field">
				<span class="label">sampai</span>
				<input type="date" min={dayMin} max={dayMax} bind:value={filters.date_to} onchange={touch} />
			</label>
			<button
				type="button"
				class="tiny"
				title="hapus rentang tanggal dan cari di semua hari (lebih lambat)"
				onclick={() => {
					filters.date_from = '';
					filters.date_to = '';
					touch();
				}}>semua hari</button>
			<button
				type="button"
				class="tiny"
				title="cari hanya pada hari data terbaru"
				onclick={() => {
					filters.date_from = dayMax;
					filters.date_to = dayMax;
					touch();
				}}>hari terbaru</button>
			<label class="field">
				<span class="label">urutkan</span>
				<select bind:value={filters.sort} onchange={touch}>
					<option value="revenue">pendapatan</option>
					<option value="units">unit terjual</option>
					<option value="price">harga</option>
					<option value="margin">margin</option>
					<option value="day">hari</option>
				</select>
			</label>
			<label class="field narrow">
				<span class="label">baris per halaman</span>
				<select bind:value={filters.limit} onchange={touch}>
					<option value={25}>25</option>
					<option value={50}>50</option>
					<option value={100}>100</option>
					<option value={250}>250</option>
					<option value={500}>500</option>
				</select>
			</label>
		</div>

		{#if filters.date_from && filters.date_to && filters.date_from > filters.date_to}
			<p class="error tiny">tanggal awal melewati tanggal akhir; ubah rentangnya</p>
		{/if}
	</div>
</section>

<style>
	.search .panel-body {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 6px 8px 8px;
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.row {
		display: flex;
		gap: 6px;
		align-items: flex-end;
		flex-wrap: wrap;
	}

	.row-q {
		display: block;
	}

	.q {
		width: 100%;
		padding: 4px 8px;
		font-size: var(--fs-md);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 1px;
		flex: 1 1 120px;
		min-width: 0;
	}

	.field.narrow {
		flex: 0 1 84px;
	}

	.field input,
	.field select {
		width: 100%;
	}

	.tiny {
		font-size: var(--fs-xs);
	}
</style>
