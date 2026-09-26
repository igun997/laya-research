<script lang="ts">
	/**
	 * Sparkline: inline SVG polyline over a plain `number[]`.
	 * No dependencies, no chart library. Handles 0- and 1-point series.
	 */
	interface Props {
		values: number[];
		width?: number;
		height?: number;
		/** Render a soft area fill under the line. */
		area?: boolean;
		/** Fixed y-axis floor/ceiling; when omitted the series min/max are used. */
		min?: number;
		max?: number;
		label?: string;
		format?: (value: number) => string;
		/** Pins the y-axis to zero (correct for units / inventory). */
		zeroBased?: boolean;
	}

	let {
		values,
		width = 240,
		height = 44,
		area = true,
		min,
		max,
		label = '',
		format = (value: number) => value.toFixed(2),
		zeroBased = false
	}: Props = $props();

	const PAD = 2;

	const clean = $derived(values.filter((value) => Number.isFinite(value)));

	const bounds = $derived.by(() => {
		if (clean.length === 0) return { lo: 0, hi: 1 };
		let lo = min ?? Math.min(...clean);
		let hi = max ?? Math.max(...clean);
		if (zeroBased) lo = Math.min(0, lo);
		if (lo === hi) {
			lo -= 1;
			hi += 1;
		}
		return { lo, hi };
	});

	const points = $derived.by(() => {
		if (clean.length === 0) return [] as { x: number; y: number }[];
		const span = bounds.hi - bounds.lo;
		const usableW = Math.max(1, width - PAD * 2);
		const usableH = Math.max(1, height - PAD * 2);
		const step = clean.length > 1 ? usableW / (clean.length - 1) : 0;
		const x0 = clean.length > 1 ? PAD : width / 2;
		return clean.map((value, index) => ({
			x: x0 + step * index,
			y: PAD + usableH - ((value - bounds.lo) / span) * usableH
		}));
	});

	const line = $derived(points.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' '));

	const areaPath = $derived.by(() => {
		if (!area || points.length < 2) return '';
		const first = points[0];
		const last = points[points.length - 1];
		return `M ${first.x.toFixed(2)},${(height - PAD).toFixed(2)} L ${line.replace(/ /g, ' L ')} L ${last.x.toFixed(2)},${(height - PAD).toFixed(2)} Z`;
	});

	const latest = $derived(clean.length > 0 ? clean[clean.length - 1] : null);
</script>

{#if clean.length === 0}
	<div class="spark-empty faint">no series data</div>
{:else}
	<figure class="spark-wrap">
		<svg
			class="sparkline"
			viewBox={`0 0 ${width} ${height}`}
			preserveAspectRatio="none"
			role="img"
			aria-label={`${label || 'series'}: ${clean.length} points, latest ${latest === null ? 'n/a' : format(latest)}`}
		>
			<line class="axis" x1={PAD} y1={height - PAD} x2={width - PAD} y2={height - PAD} />
			{#if areaPath}
				<path class="area" d={areaPath} />
			{/if}
			{#if points.length === 1}
				<circle class="dot" cx={points[0].x} cy={points[0].y} r="2" />
			{:else}
				<polyline class="line" points={line} />
			{/if}
		</svg>
		<figcaption class="spark-cap mono">
			<span class="faint">{format(bounds.lo)}</span>
			{#if latest !== null}<span>{format(latest)}</span>{/if}
			<span class="faint">{format(bounds.hi)}</span>
		</figcaption>
	</figure>
{/if}

<style>
	.spark-wrap {
		margin: 0;
		display: block;
	}

	.sparkline {
		height: 44px;
	}

	.spark-cap {
		display: flex;
		justify-content: space-between;
		gap: 6px;
		font-size: var(--fs-xs);
		line-height: 1.2;
	}

	.spark-empty {
		font-size: var(--fs-sm);
		padding: 8px 0;
	}
</style>
