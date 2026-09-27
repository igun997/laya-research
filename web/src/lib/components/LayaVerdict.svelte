<script lang="ts">
	/**
	 * Laya's decision for one product-day, next to the rule engine's verdict.
	 *
	 * The point of the panel is the disagreement. Laya and the rules are two
	 * different decision makers reading the same state, and showing them side by
	 * side with the probability distributions visible is more informative than
	 * either one alone. Nothing here is inferred or smoothed over: if the model is
	 * unavailable, or it disagrees, or its confidence is near uniform, the panel
	 * says so.
	 *
	 * A forward pass takes 8 to 12 seconds on CPU, so the pending state is long by
	 * design and carries the reason.
	 */
	import { getDecide } from '$lib/api';
	import { isApiError } from '$lib/api';
	import { fmtPct } from '$lib/format';
	import type { DecideResponse, LayaAnswer } from '$lib/types';

	interface Props {
		productId: number;
		/** Defaults to the newest dataset day when omitted. */
		day?: string;
	}

	let { productId, day }: Props = $props();

	let data = $state<DecideResponse | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let elapsed = $state(0);

	$effect(() => {
		const id = productId;
		const targetDay = day;
		const controller = new AbortController();
		let ticker: number | undefined;

		data = null;
		error = null;
		loading = true;
		elapsed = 0;
		const startedAt = Date.now();
		ticker = setInterval(() => {
			elapsed = Math.round((Date.now() - startedAt) / 1000);
		}, 250);

		void (async () => {
			try {
				const response = await getDecide(id, { day: targetDay }, controller.signal);
				if (controller.signal.aborted) return;
				data = response;
				loading = false;
			} catch (cause) {
				if (controller.signal.aborted) return;
				loading = false;
				error = isApiError(cause) ? cause.detail : String(cause);
			} finally {
				clearInterval(ticker);
			}
		})();

		return () => {
			clearInterval(ticker);
			controller.abort();
		};
	});

	const answers = $derived(data?.laya.answers ?? {});
	const agreement = $derived(data?.agreement ?? null);

	function barWidth(probability: number): string {
		return `${Math.max(1, Math.min(100, probability * 100))}%`;
	}

	function sortedProbabilities(answer: LayaAnswer | undefined): [string, number][] {
		if (!answer?.probabilities) return [];
		return Object.entries(answer.probabilities).sort((a, b) => b[1] - a[1]);
	}

	/** Which of the two is "confident" versus "near uniform", stated plainly. */
	function confidenceNote(answer: LayaAnswer | undefined, optionCount: number): string {
		if (!answer || answer.confidence === null) return 'tanpa skor keyakinan';
		const uniform = 1 / optionCount;
		if (answer.confidence <= uniform * 1.25) return 'hampir merata; belum jelas';
		if (answer.confidence < 0.5) return 'rendah';
		return 'tegas';
	}

	function agreeMark(flag: boolean | null | undefined): string {
		if (flag === null || flag === undefined) return '—';
		return flag ? 'sesuai' : 'berbeda';
	}
</script>

<section class="laya">
	<header>
		<h4>Keputusan Laya</h4>
		{#if data?.laya.available}
			<span class="meta mono tiny">
				{data.laya.routing?.model ?? 'model'}{#if data.laya.latency_ms !== null} ·
					{Math.round(data.laya.latency_ms)} ms{/if}
			</span>
		{/if}
	</header>

	{#if loading}
		<p class="state pending mono tiny">
			menjalankan model di CPU… {elapsed} dtk
			<span class="faint">(400 juta parameter, tanpa GPU; biasanya 8–12 dtk)</span>
		</p>
	{:else if error}
		<p class="state bad mono tiny">{error}</p>
	{:else if data && !data.laya.available}
		<p class="state bad mono tiny">
			model tidak tersedia: {data.laya.detail ?? 'tanpa keterangan'}
			<span class="faint">keputusan aturan di bawah tetap tersedia</span>
		</p>
	{:else if data && agreement}
		<div class="compare">
			<table>
				<thead>
					<tr>
						<th>pertanyaan</th>
						<th>Laya</th>
						<th>aturan</th>
						<th>perbandingan</th>
					</tr>
				</thead>
				<tbody>
					<tr>
						<td class="q">pola</td>
						<td class="mono">{agreement.laya.pattern ?? '—'}</td>
						<td class="mono">{agreement.rules.pattern}</td>
						<td class:agree={agreement.pattern} class:differs={agreement.pattern === false}>
							{agreeMark(agreement.pattern)}
						</td>
					</tr>
					<tr>
						<td class="q">tingkat</td>
						<td class="mono">{agreement.laya.severity ?? '—'}</td>
						<td class="mono">{agreement.rules.severity}</td>
						<td class:agree={agreement.severity} class:differs={agreement.severity === false}>
							{agreeMark(agreement.severity)}
						</td>
					</tr>
					<tr>
						<td class="q">pesan ulang sekarang</td>
						<td class="mono">
							{#if agreement.laya.reorder_probability !== null}
								P={agreement.laya.reorder_probability.toFixed(3)}
							{:else}
								—
							{/if}
						</td>
						<td class="mono">{agreement.rules.reorder ? 'ya' : 'tidak'}</td>
						<td class:agree={agreement.reorder} class:differs={agreement.reorder === false}>
							{agreeMark(agreement.reorder)}
						</td>
					</tr>
				</tbody>
			</table>
		</div>

		{#each Object.entries(answers) as [name, answer] (name)}
			<div class="answer">
				<div class="answer-head">
					<span class="q">{name.replace(/_/g, ' ')}</span>
					<span class="chosen mono">{String(answer.answer ?? '—')}</span>
					<span class="conf mono tiny faint">
						p={answer.confidence === null ? '—' : answer.confidence.toFixed(3)}
						· {confidenceNote(answer, sortedProbabilities(answer).length || 1)}
					</span>
				</div>
				{#if answer.score_position !== null}
					<span class="mono tiny faint">posisi rubrik {answer.score_position.toFixed(3)} / 3</span>
				{/if}
				<ul class="probs">
					{#each sortedProbabilities(answer) as [option, probability] (option)}
						<li class:chosen={option === String(answer.answer)}>
							<span class="opt mono tiny">{option}</span>
							<span class="track"><span class="fill" style:width={barWidth(probability)}></span></span>
							<span class="val mono tiny">{fmtPct(probability)}</span>
						</li>
					{/each}
				</ul>
			</div>
		{/each}

		<details class="state-text">
			<summary class="mono tiny faint">kondisi yang dikirim ke model</summary>
			<p class="mono tiny">{data.state_text}</p>
		</details>
	{/if}
</section>

<style>
	.laya {
		border-top: 1px solid var(--border);
		padding: 10px 12px 14px;
	}

	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 8px;
	}

	h4 {
		margin: 0;
		font-size: var(--fs-sm);
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}

	.meta {
		color: var(--text-faint);
	}

	.state {
		margin: 0 0 6px;
		line-height: 1.5;
	}

	.state.pending {
		color: var(--text-dim);
	}

	.state.bad {
		color: var(--warn);
	}

	.state .faint {
		color: var(--text-faint);
	}

	table {
		width: 100%;
		border-collapse: collapse;
		margin-bottom: 10px;
	}

	th {
		text-align: left;
		font-size: var(--fs-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-faint);
		font-weight: 500;
		padding: 0 6px 3px 0;
		border-bottom: 1px solid var(--border);
	}

	td {
		padding: 3px 6px 3px 0;
		border-bottom: 1px solid var(--border);
		font-size: var(--fs-xs);
	}

	td.q {
		color: var(--text-dim);
	}

	td.agree {
		color: var(--ok);
	}

	td.differs {
		color: var(--critical);
	}

	.answer {
		margin: 0 0 10px;
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
	}

	.chosen {
		color: var(--accent);
		font-size: var(--fs-sm);
	}

	.conf {
		color: var(--text-faint);
		margin-left: auto;
	}

	.probs {
		list-style: none;
		margin: 4px 0 0;
		padding: 0;
	}

	.probs li {
		display: grid;
		grid-template-columns: minmax(90px, 26%) 1fr auto;
		align-items: center;
		gap: 6px;
		padding: 1px 0;
	}

	.probs li.chosen .opt {
		color: var(--text);
	}

	.opt {
		color: var(--text-faint);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
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

	.state-text {
		margin-top: 6px;
	}

	.state-text summary {
		cursor: pointer;
	}

	.state-text p {
		margin: 4px 0 0;
		color: var(--text-dim);
		line-height: 1.5;
	}
</style>
