<script lang="ts">
	import { fmtAgo, fmtCompact, fmtInt, fmtDuration } from '$lib/format';
	import type { DatasetFacts } from '$lib/types';
	import type { StreamStatus } from '$lib/stream';

	interface Props {
		dataset: DatasetFacts | null;
		status: StreamStatus;
		ticks: number;
		lastTick: number | null;
		day: string | null;
		rollupLagSeconds: number | null;
		/** Epoch ms of the last received frame; drives the "last frame" clock. */
		lastFrameAt: number | null;
		/** Reconnect counter, shown while the socket is not live. */
		reconnects: number;
		now: number;
		loading?: boolean;
		error?: string | null;
	}

	let {
		dataset,
		status,
		ticks,
		lastTick,
		day,
		rollupLagSeconds,
		lastFrameAt,
		reconnects,
		now,
		loading = false,
		error = null
	}: Props = $props();

	const statusLabel = $derived(
		status === 'live' ? 'live' : status === 'reconnecting' ? 'reconnecting' : status === 'connecting' ? 'connecting' : 'offline'
	);
</script>

<header class="statusbar">
	<span class="brand">
		<strong>laya</strong><span class="faint">/market datasheet</span>
	</span>

	<span class="sep" aria-hidden="true"></span>

	<span class="stat" title="rows in market_facts">
		<span class="label">facts</span>
		<span class="mono val">{dataset ? fmtInt(dataset.facts) : '—'}</span>
	</span>
	<span class="stat" title="products x stores">
		<span class="label">catalog</span>
		<span class="mono val">
			{dataset ? `${fmtInt(dataset.products)}p / ${fmtInt(dataset.stores)}s` : '—'}
		</span>
	</span>
	<span class="stat" title="day range of the datasheet">
		<span class="label">days</span>
		<span class="mono val">
			{dataset ? `${dataset.day_min} → ${dataset.day_max}` : '—'}
		</span>
	</span>
	<span class="stat" title="seed used by the generator">
		<span class="label">seed</span>
		<span class="mono val">{dataset ? dataset.seed : '—'}</span>
	</span>

	<span class="sep" aria-hidden="true"></span>

	<span class="stat" title="simulator target day">
		<span class="label">tick day</span>
		<span class="mono val">{day ?? '—'}</span>
	</span>
	<span class="stat" title="tick frames received this session">
		<span class="label">ticks</span>
		<span class="mono val">{fmtInt(ticks)}</span>
	</span>
	<span class="stat" title="tick counter reported by the simulator">
		<span class="label">sim tick</span>
		<span class="mono val">{lastTick === null ? '—' : fmtInt(lastTick)}</span>
	</span>
	<span class="stat" title="age of the last websocket frame">
		<span class="label">last frame</span>
		<span class="mono val">{lastFrameAt === null ? '—' : fmtAgo(new Date(lastFrameAt).toISOString(), now)}</span>
	</span>
	<span class="stat" title="age of the newest materialized-view rollup">
		<span class="label">rollup lag</span>
		<span class="mono val" class:stale={(rollupLagSeconds ?? 0) > 60}>
			{fmtDuration(rollupLagSeconds)}
		</span>
	</span>

	<span class="spacer"></span>

	{#if loading}
		<span class="faint mono tiny">loading…</span>
	{/if}
	{#if error}
		<span class="error" title={error}>meta error: {error}</span>
	{/if}

	<span class="ws ws-{status}" title={`reconnects: ${reconnects}`}>
		<span class="dot" aria-hidden="true"></span>
		<span class="mono">{statusLabel}</span>
		{#if status !== 'live' && reconnects > 0}
			<span class="faint mono tiny">x{fmtCompact(reconnects)}</span>
		{/if}
	</span>
</header>

<style>
	.statusbar {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 4px 10px;
		background: var(--bg-panel);
		border-bottom: 1px solid var(--border);
		font-size: var(--fs-sm);
		position: sticky;
		top: 0;
		z-index: 20;
	}

	.brand {
		font-size: var(--fs-lg);
		letter-spacing: 0.02em;
	}

	.sep {
		width: 1px;
		align-self: stretch;
		background: var(--border);
	}

	.stat {
		display: inline-flex;
		align-items: baseline;
		gap: 4px;
	}

	.val {
		font-size: var(--fs-sm);
	}

	.stale {
		color: var(--warn);
	}

	.spacer {
		flex: 1 1 auto;
	}

	.ws {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 1px 7px;
		border: 1px solid var(--border);
		border-radius: 999px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: var(--fs-xs);
	}

	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--text-faint);
	}

	.ws-live {
		border-color: var(--ok);
		color: var(--ok);
	}

	.ws-live .dot {
		background: var(--ok);
	}

	.ws-connecting,
	.ws-reconnecting {
		border-color: var(--warn);
		color: var(--warn);
	}

	.ws-connecting .dot,
	.ws-reconnecting .dot {
		background: var(--warn);
		animation: pulse 1.1s ease-in-out infinite;
	}

	.ws-offline {
		border-color: var(--critical);
		color: var(--critical);
	}

	.ws-offline .dot {
		background: var(--critical);
	}

	.tiny {
		font-size: var(--fs-xs);
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.25;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.ws-connecting .dot,
		.ws-reconnecting .dot {
			animation: none;
		}
	}
</style>
