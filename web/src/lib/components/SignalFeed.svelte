<script lang="ts">
	import { fmtAgo, fmtEvidence, fmtInt, fmtRatio } from '$lib/format';
	import type { PatternDef, Severity, Signal, SignalCounts } from '$lib/types';

	interface Props {
		/** Signals seeded from `GET /api/signals`, newest first, already de-duplicated. */
		signals: Signal[];
		patterns: PatternDef[];
		activePatterns: string[];
		activeSeverities: Severity[];
		counts: SignalCounts;
		total: number;
		loading: boolean;
		error: string | null;
		now: number;
		onTogglePattern?: (pattern: string) => void;
		onToggleSeverity?: (severity: Severity) => void;
		onMarkSeen?: (signalIds: number[]) => void;
		onRefresh?: () => void;
		seenCount?: number;
	}

	let {
		signals,
		patterns,
		activePatterns,
		activeSeverities,
		counts,
		total,
		loading,
		error,
		now,
		onTogglePattern,
		onToggleSeverity,
		onMarkSeen,
		onRefresh,
		seenCount = 0
	}: Props = $props();

	const SEVERITIES: Severity[] = ['critical', 'warn', 'info'];

	function labelFor(id: string) {
		const match = patterns.find((pattern) => pattern.id === id);
		return match ? match.label : id;
	}

	let expanded = $state<number | null>(null);

	function severityClass(severity: Severity) {
		return severity === 'critical' ? 'sev-critical' : severity === 'warn' ? 'sev-warn' : 'sev-info';
	}

	function toggleExpand(signalId: number) {
		expanded = expanded === signalId ? null : signalId;
	}

	/** Evidence is untyped JSON; show the stable contract keys first. */
	const EVIDENCE_ORDER = ['metric', 'value', 'baseline', 'lift', 'window_days', 'observations'];

	function evidenceEntries(evidence: Record<string, unknown>) {
		const keys = Object.keys(evidence);
		const ordered = [
			...EVIDENCE_ORDER.filter((key) => keys.includes(key)),
			...keys.filter((key) => !EVIDENCE_ORDER.includes(key)).sort()
		];
		return ordered.map((key) => ({ key, value: evidence[key] }));
	}
</script>

<section class="panel feed">
	<header>
		<h2>signals</h2>
		<div class="hdr-right">
			<span class="counts mono tiny">
				<span class="badge sev-critical">crit {fmtInt(counts.critical)}</span>
				<span class="badge sev-warn">warn {fmtInt(counts.warn)}</span>
				<span class="badge sev-info">info {fmtInt(counts.info)}</span>
				{#if seenCount > 0}<span class="faint">seen {fmtInt(seenCount)}</span>{/if}
				<span class="faint">of {fmtInt(total)}</span>
			</span>
			<button type="button" class="tiny" onclick={() => onRefresh?.()} disabled={loading}>refresh</button>
		</div>
	</header>

	<div class="filters">
		<div class="filter-row">
			<span class="label">pattern</span>
			<div class="chips">
				<button
					type="button"
					class="chip"
					aria-pressed={activePatterns.length === 0}
					onclick={() => onTogglePattern?.('')}
				>all</button>
				{#each patterns as pattern (pattern.id)}
					<button
						type="button"
						class="chip"
						aria-pressed={activePatterns.includes(pattern.id)}
						title={pattern.description}
						onclick={() => onTogglePattern?.(pattern.id)}
					>{labelFor(pattern.id)}</button>
				{/each}
			</div>
		</div>
		<div class="filter-row">
			<span class="label">severity</span>
			<div class="chips">
				{#each SEVERITIES as severity (severity)}
					<button
						type="button"
						class="chip"
						aria-pressed={activeSeverities.includes(severity)}
						onclick={() => onToggleSeverity?.(severity)}
					>{severity}</button>
				{/each}
			</div>
		</div>
	</div>

	<div class="panel-body list">
		{#if error}
			<p class="error">{error}</p>
		{/if}

		{#each signals as signal (signal.signal_id)}
			<article class="signal {severityClass(signal.severity)}">
				<div class="head">
					<span class="badge {severityClass(signal.severity)}">{signal.severity}</span>
					<span class="pattern mono">{labelFor(signal.pattern)}</span>
					<span class="subject" title={signal.subject_label}>{signal.subject_label}</span>
					<span class="spacer"></span>
					<span class="faint mono tiny" title={signal.fired_at}>{fmtAgo(signal.fired_at, now)}</span>
					<button
						type="button"
						class="tiny ghost"
						onclick={() => toggleExpand(signal.signal_id)}
						title="evidence"
					>{expanded === signal.signal_id ? '−' : '+'}</button>
					<button type="button" class="tiny ghost" onclick={() => onMarkSeen?.([signal.signal_id])}>
						mark seen
					</button>
				</div>

				<!-- `action` is server-rendered prose with the numbers baked in: rendered verbatim. -->
				<p class="action">{signal.action}</p>

				<div class="meta mono tiny faint">
					<span>day {signal.day}</span>
					<span>score {fmtRatio(signal.score)}</span>
					<span>{signal.subject_type} #{signal.subject_id}</span>
					<span>signal #{signal.signal_id}</span>
				</div>

				{#if expanded === signal.signal_id}
					<dl class="evidence mono tiny">
						{#each evidenceEntries(signal.evidence) as entry (entry.key)}
							<dt>{entry.key}</dt>
							<dd>{fmtEvidence(entry.value)}</dd>
						{/each}
					</dl>
				{/if}
			</article>
		{:else}
			<p class="empty">
				{loading ? 'loading signals…' : 'no signals match the current filter'}
			</p>
		{/each}
	</div>
</section>

<style>
	.feed {
		min-height: 0;
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.counts {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}

	.filters {
		padding: 4px 6px;
		border-bottom: 1px solid var(--border);
		background: var(--bg-panel);
		flex: 0 0 auto;
	}

	.filter-row {
		display: flex;
		align-items: baseline;
		gap: 6px;
		padding: 1px 0;
	}

	.filter-row > .label {
		flex: 0 0 46px;
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 3px;
		min-width: 0;
	}

	.list {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 5px 6px;
	}

	.signal {
		border: 1px solid var(--border);
		border-left-width: 3px;
		border-radius: var(--radius);
		padding: 3px 6px 4px;
		background: var(--bg-panel);
	}

	.signal.sev-critical {
		border-left-color: var(--critical);
		background: var(--critical-soft);
	}

	.signal.sev-warn {
		border-left-color: var(--warn);
		background: var(--warn-soft);
	}

	.signal.sev-info {
		border-left-color: var(--info);
		background: var(--info-soft);
	}

	.head {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.pattern {
		font-size: var(--fs-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.subject {
		font-size: var(--fs-sm);
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 220px;
	}

	.spacer {
		flex: 1 1 auto;
	}

	.action {
		margin: 2px 0 0;
		font-size: var(--fs-sm);
		line-height: 1.35;
	}

	.meta {
		display: flex;
		gap: 10px;
		flex-wrap: wrap;
		margin-top: 2px;
	}

	.evidence {
		display: grid;
		grid-template-columns: minmax(70px, auto) 1fr;
		gap: 0 8px;
		margin: 4px 0 0;
		padding-top: 3px;
		border-top: 1px dashed var(--border-strong);
	}

	.evidence dt {
		color: var(--text-faint);
	}

	.evidence dd {
		margin: 0;
		overflow-wrap: anywhere;
	}

	.ghost {
		background: transparent;
		border-color: transparent;
		color: var(--text-dim);
		padding: 0 5px;
	}

	.ghost:hover {
		border-color: var(--border);
		color: var(--text);
	}

	.tiny {
		font-size: var(--fs-xs);
	}
</style>
