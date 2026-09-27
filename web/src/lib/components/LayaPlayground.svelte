<script lang="ts">
	/**
	 * Free-text Laya playground: an editable state block plus a typed question schema.
	 *
	 * The point is the primitives. Laya answers `choice` (named options), `score`
	 * (an ordinal rubric) and `noul` (a yes/no probability), and the server
	 * normalizes each answer by the question's declared `type` — not by its name.
	 * So the editor lets you name a question anything (`mood`, `risk`, whatever)
	 * and still get a correctly shaped answer back, which is exactly the property
	 * that is easy to get wrong upstream.
	 *
	 * A forward pass is seconds of CPU, so nothing here runs on a keystroke. The
	 * run button is the only trigger, and a newer run always wins over a slower
	 * older one that resolves late.
	 */
	import { layaPlayground, isApiError } from '$lib/api';
	import { fmtPct } from '$lib/format';
	import type { LayaAnswer, LayaPlaygroundResponse, LayaQuestion, LayaQuestionType } from '$lib/types';

	/* --------------------------------------------------------------- state */

	/**
	 * No prop seeds this. The state block is meant to be typed or pasted, and the
	 * example below is a real `render_state` line from the API so the shape matches
	 * what the decision path sends.
	 */
	const EXAMPLE_STATE =
		'Produk dan hari penjualan. Produk: Susu UHT 1 L. Kategori: produk susu. Merek: merek toko. ' +
		'Merek sendiri: ya. Mudah rusak: ya. Terjual hari ini: 412 unit, median pembanding: 505. ' +
		'Harga rata-rata hari ini: 1,29; pembanding: 1,31. Margin hari ini: 18,4%; pembanding: 22,1%. ' +
		'Stok cukup untuk 0,85 hari. Dijual di 41 toko, promosi di 3 toko. ' +
		'Pembanding dihitung dari 28 hari.';

	const TYPES: LayaQuestionType[] = ['choice', 'score', 'noul'];

	/** One editable question. `criteria` is a textarea for both choice and score. */
	interface QuestionDraft {
		id: number;
		name: string;
		type: LayaQuestionType;
		instructions: string;
		/** choice: one `OPTION = description` per line. score: one level per line. */
		criteria: string;
	}

	let nextId = 1;

	function makeDraft(
		name: string,
		type: LayaQuestionType,
		instructions: string,
		criteria: string
	): QuestionDraft {
		return { id: nextId++, name, type, instructions, criteria };
	}

	let stateText = $state(EXAMPLE_STATE);
	let model = $state('');
	let questions = $state<QuestionDraft[]>([
		makeDraft(
			'pattern',
			'choice',
			'Pola keputusan mana yang paling sesuai untuk kondisi produk ini dibandingkan data sebelumnya?',
			'none = tidak ada pola keputusan, kondisi sesuai pembanding\n' +
				'DEMAND_SURGE = penjualan jauh di atas pembanding\n' +
				'DEMAND_COLLAPSE = penjualan jauh di bawah pembanding\n' +
				'STOCKOUT_RISK = stok cukup kurang dari satu setengah hari\n' +
				'PROMO_INEFFECTIVE = sebagian besar toko berpromosi, tetapi penjualan tidak naik'
		),
		makeDraft(
			'severity',
			'score',
			'Seberapa berat kondisi produk ini?',
			'none\ninfo\nwarn\ncritical'
		),
		makeDraft(
			'reorder_now',
			'noul',
			'Apakah stok produk ini perlu segera ditambah agar tidak habis besok?',
			''
		)
	]);

	let result = $state<LayaPlaygroundResponse | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let validation = $state<string | null>(null);
	let elapsed = $state(0);
	let sentState = $state('');
	let sentNames = $state<string[]>([]);
	/** Pasted schema JSON, imported into the form on demand. */
	let jsonImport = $state('');
	let jsonError = $state<string | null>(null);

	let runSeq = 0;
	let abort: AbortController | null = null;
	let ticker: ReturnType<typeof setInterval> | null = null;

	const stateLength = $derived(stateText.length);
	const nonEmptyQuestions = $derived(questions.filter((question) => question.name.trim() !== ''));

	/* --------------------------------------------------------------- schema IO */

	/**
	 * Parses one criteria block into the wire shape.
	 *
	 * `choice` wants a name -> description map, so `OPTION = text` lines are the
	 * natural editor form. `score` wants an ordered list, so plain lines are.
	 */
	function parseCriteria(draft: QuestionDraft): { value: Record<string, string> | string[] | null; error: string | null } {
		const lines = draft.criteria
			.split('\n')
			.map((line) => line.trim())
			.filter((line) => line !== '');
		if (draft.type === 'noul') return { value: null, error: null };
		if (lines.length === 0) {
			return { value: null, error: `"${draft.name}": ${draft.type} memerlukan setidaknya satu baris kriteria` };
		}
		if (draft.type === 'score') return { value: lines, error: null };
		const map: Record<string, string> = {};
		for (const line of lines) {
			const at = line.indexOf('=');
			if (at <= 0) {
				return {
					value: null,
					error: `"${draft.name}": baris pilihan "${line}" harus berbentuk OPSI = keterangan`
				};
			}
			map[line.slice(0, at).trim()] = line.slice(at + 1).trim();
		}
		return { value: map, error: null };
	}

	function buildQuestions(): { value: Record<string, LayaQuestion> | null; error: string | null } {
		if (stateText.trim() === '') return { value: null, error: 'kondisi masih kosong' };
		if (nonEmptyQuestions.length === 0) return { value: null, error: 'tambahkan setidaknya satu pertanyaan bernama' };
		const out: Record<string, LayaQuestion> = {};
		for (const draft of questions) {
			const name = draft.name.trim();
			if (name === '') continue;
			if (Object.hasOwn(out, name)) return { value: null, error: `nama pertanyaan "${name}" digunakan dua kali` };
			const parsed = parseCriteria(draft);
			if (parsed.error !== null) return { value: null, error: parsed.error };
			const instructions = draft.instructions.trim();
			if (draft.type === 'choice') {
				out[name] = {
					type: 'choice',
					criteria: parsed.value as Record<string, string>,
					...(instructions === '' ? {} : { instructions })
				};
			} else if (draft.type === 'score') {
				out[name] = {
					type: 'score',
					criteria: parsed.value as string[],
					...(instructions === '' ? {} : { instructions })
				};
			} else {
				out[name] = {
					type: 'noul',
					...(instructions === '' ? {} : { instructions })
				};
			}
		}
		return { value: out, error: null };
	}

	/** Renders the current schema back into JSON so it can be pasted or inspected. */
	const schemaJson = $derived.by(() => {
		const built = buildQuestions();
		if (built.value === null) return `// ${built.error}`;
		return JSON.stringify(built.value, null, 2);
	});

	/* ------------------------------------------------------------------ actions */

	function addQuestion() {
		questions = [...questions, makeDraft('', 'choice', '', '')];
	}

	/**
	 * Imports a pasted schema into the form.
	 *
	 * The form is the source of truth for what gets sent, so a paste has to survive
	 * the round trip through it: a `choice` map becomes `OPTION = description`
	 * lines, a `score` list becomes one line per level. Anything malformed is
	 * reported and nothing is replaced, so a bad paste cannot destroy the form.
	 */
	function importJson() {
		jsonError = null;
		const text = jsonImport.trim();
		if (text === '') {
			jsonError = 'tempel objek skema terlebih dahulu';
			return;
		}
		let parsed: unknown;
		try {
			parsed = JSON.parse(text);
		} catch (cause) {
			jsonError = `JSON tidak valid: ${cause instanceof Error ? cause.message : String(cause)}`;
			return;
		}
		if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
			jsonError = 'skema harus berupa objek JSON berisi nama pertanyaan';
			return;
		}
		const drafts: QuestionDraft[] = [];
		for (const [name, value] of Object.entries(parsed as Record<string, unknown>)) {
			if (value === null || typeof value !== 'object' || Array.isArray(value)) {
				jsonError = `"${name}": isi harus berupa objek dengan tipe`;
				return;
			}
			const record = value as { type?: unknown; instructions?: unknown; criteria?: unknown };
			const type = record.type;
			if (type !== 'choice' && type !== 'score' && type !== 'noul') {
				jsonError = `"${name}": tipe harus choice, score, atau noul`;
				return;
			}
			const instructions = typeof record.instructions === 'string' ? record.instructions : '';
			let criteria = '';
			if (type === 'choice') {
				if (record.criteria === null || typeof record.criteria !== 'object' || Array.isArray(record.criteria)) {
					jsonError = `"${name}": choice memerlukan objek opsi dan keterangan`;
					return;
				}
				criteria = Object.entries(record.criteria as Record<string, unknown>)
					.map(([option, description]) => `${option} = ${String(description)}`)
					.join('\n');
			} else if (type === 'score') {
				if (!Array.isArray(record.criteria) || record.criteria.length === 0) {
					jsonError = `"${name}": score memerlukan daftar tingkat yang berurutan`;
					return;
				}
				criteria = record.criteria.map((level) => String(level)).join('\n');
			}
			drafts.push(makeDraft(name, type, instructions, criteria));
		}
		if (drafts.length === 0) {
			jsonError = 'skema tidak berisi pertanyaan';
			return;
		}
		questions = drafts;
		validation = null;
		jsonImport = '';
	}

	function removeQuestion(id: number) {
		questions = questions.filter((question) => question.id !== id);
	}

	function resetState() {
		stateText = EXAMPLE_STATE;
	}

	/**
	 * FastAPI answers a rejected body with a list of `{loc, msg}` objects, and the
	 * shared `ApiError` stringifies it — including the offending input, which for a
	 * 5000-character state would flood the panel. Reduce it to the field and the
	 * reason, which is the part that tells the user what to change.
	 */
	function formatDetail(detail: string): string {
		const trimmedDetail = detail.trim();
		if (!trimmedDetail.startsWith('[')) return detail;
		try {
			const parsed: unknown = JSON.parse(trimmedDetail);
			if (!Array.isArray(parsed)) return detail;
			const parts: string[] = [];
			for (const entry of parsed) {
				if (entry === null || typeof entry !== 'object') continue;
				const record = entry as { loc?: unknown; msg?: unknown };
				const loc = Array.isArray(record.loc) ? record.loc.map(String).filter((part) => part !== 'body').join('.') : '';
				const msg = typeof record.msg === 'string' ? record.msg : '';
				if (msg !== '') parts.push(loc === '' ? msg : `${loc}: ${msg}`);
			}
			return parts.length > 0 ? parts.join(' · ') : detail;
		} catch {
			return detail;
		}
	}

	function describeError(cause: unknown): string {
		if (isApiError(cause)) {
			return cause.status === 0
				? `galat jaringan: ${cause.detail}`
				: `${cause.status} ${formatDetail(cause.detail)}`;
		}
		return cause instanceof Error ? cause.message : String(cause);
	}

	async function run() {
		const built = buildQuestions();
		if (built.value === null || built.error !== null) {
			validation = built.error;
			return;
		}
		validation = null;
		abort?.abort();
		if (ticker !== null) clearInterval(ticker);
		const controller = new AbortController();
		abort = controller;
		const token = ++runSeq;
		loading = true;
		error = null;
		result = null;
		elapsed = 0;
		sentState = stateText;
		sentNames = Object.keys(built.value);
		const startedAt = Date.now();
		ticker = setInterval(() => {
			elapsed = Math.round((Date.now() - startedAt) / 1000);
		}, 250);
		try {
			const response = await layaPlayground(
				{
					state: stateText,
					questions: built.value,
					...(model === '' ? {} : { model })
				},
				controller.signal
			);
			if (controller.signal.aborted || token !== runSeq) return;
			result = response;
			loading = false;
		} catch (cause) {
			if (controller.signal.aborted || token !== runSeq) return;
			loading = false;
			error = describeError(cause);
		} finally {
			if (token === runSeq && ticker !== null) {
				clearInterval(ticker);
				ticker = null;
			}
		}
	}

	function abortRun() {
		abort?.abort();
		abort = null;
		runSeq += 1;
		if (ticker !== null) {
			clearInterval(ticker);
			ticker = null;
		}
		loading = false;
		elapsed = 0;
	}

	$effect(() => {
		return () => {
			abort?.abort();
			if (ticker !== null) clearInterval(ticker);
		};
	});

	/* --------------------------------------------------------------- rendering */

	const answers = $derived<Record<string, LayaAnswer>>(result?.laya.answers ?? {});

	const answerEntries = $derived(
		sentNames
			.map((name) => [name, answers[name]] as const)
			.filter((entry): entry is readonly [string, LayaAnswer] => entry[1] !== undefined)
	);

	/** Answers the server returned that we did not ask for — worth surfacing, not hiding. */
	const unexpected = $derived(Object.keys(answers).filter((name) => !sentNames.includes(name)));

	function sortedProbabilities(answer: LayaAnswer | undefined): [string, number][] {
		if (!answer?.probabilities) return [];
		return Object.entries(answer.probabilities).sort((left, right) => right[1] - left[1]);
	}

	function barWidth(probability: number): string {
		return `${Math.max(1, Math.min(100, probability * 100))}%`;
	}

	function typeNote(type: LayaQuestionType): string {
		if (type === 'choice') return 'opsi bernama: distribusi peluang setiap opsi';
		if (type === 'score') return 'rubrik berurutan: tingkat dan posisinya';
		return 'ya/tidak: satu nilai P(benar)';
	}

	/**
	 * `noul` answers with a single probability and no distribution, so an empty
	 * probability list is expected there and worth saying rather than looking like
	 * a missing result.
	 */
	function noDistributionNote(answer: LayaAnswer): string {
		if (typeof answer.answer === 'number') {
			return `peluang tunggal P(benar) = ${answer.answer.toFixed(4)}; noul tidak memiliki distribusi`;
		}
		return 'tidak ada distribusi untuk jawaban ini';
	}
</script>

<section class="panel playground">
	<header>
		<h2>Eksperimen Laya</h2>
		<div class="hdr-right mono tiny">
			<span class="faint">kondisi bebas · pertanyaan bertipe · jawaban sesuai tipe</span>
		</div>
	</header>

	<div class="panel-body">
		<div class="block">
			<div class="block-head">
				<label class="label" for="pg-state">kondisi</label>
				<span class="mono tiny faint">{stateLength} karakter · dibatasi oleh server</span>
				<button type="button" class="tiny" onclick={resetState}>pulihkan</button>
			</div>
			<textarea
				id="pg-state"
				class="state-input mono"
				rows="6"
				spellcheck="false"
				bind:value={stateText}
				placeholder="Kondisi produk pada hari ini…"
			></textarea>
		</div>

		<div class="block">
			<div class="block-head">
				<span class="label">pertanyaan</span>
				<label class="model">
					<span class="label" title="nama model yang tidak dikenal akan ditolak oleh API">
						checkpoint
					</span>
					<input
						type="text"
						class="model-input"
						bind:value={model}
						placeholder="pilihan bawaan"
						autocomplete="off"
					/>
				</label>
				<button type="button" class="tiny" onclick={addQuestion}>+ pertanyaan</button>
			</div>

			<ul class="qlist">
				{#each questions as draft (draft.id)}
					<li class="qrow">
						<div class="qrow-top">
							<label class="field">
								<span class="label">nama</span>
								<input
									type="text"
									bind:value={draft.name}
									placeholder="misalnya risiko"
									autocomplete="off"
								/>
							</label>
							<label class="field narrow">
								<span class="label">tipe</span>
								<select bind:value={draft.type}>
									{#each TYPES as type (type)}
										<option value={type}>{type}</option>
									{/each}
								</select>
							</label>
							<button
								type="button"
								class="tiny remove"
								disabled={questions.length <= 1}
								title="hapus pertanyaan ini"
								onclick={() => removeQuestion(draft.id)}>✕</button>
						</div>

						<label class="field">
							<span class="label">petunjuk (opsional)</span>
							<input type="text" bind:value={draft.instructions} autocomplete="off" />
						</label>

						{#if draft.type !== 'noul'}
							<label class="field">
								<span class="label">
									kriteria {draft.type === 'choice' ? '(OPSI = keterangan, satu per baris)' : '(satu tingkat per baris, berurutan)'}
								</span>
								<textarea
									class="mono criteria"
									rows={draft.type === 'choice' ? 5 : 4}
									spellcheck="false"
									bind:value={draft.criteria}
								></textarea>
							</label>
						{:else}
							<p class="type-note faint tiny">{typeNote(draft.type)}</p>
						{/if}
					</li>
				{/each}
			</ul>
		</div>

		<div class="run-row">
			<button type="button" class="run" disabled={loading} onclick={run}>
				{loading ? `berjalan… ${elapsed} dtk` : 'Jalankan Laya'}
			</button>
			{#if loading}
				<button type="button" class="tiny" onclick={abortRun}>batalkan</button>
			{/if}
			<span class="faint tiny">
				satu proses CPU untuk semua pertanyaan; dapat memakan waktu beberapa detik
			</span>
		</div>

		{#if validation}
			<p class="error" role="alert">{validation}</p>
		{/if}

		<details class="schema">
			<summary class="mono tiny faint">isi permintaan (skema bertipe yang dikirim)</summary>
			<pre class="mono tiny">{schemaJson}</pre>
		</details>

		<details class="import">
			<summary class="mono tiny faint">tempel skema</summary>
			<div class="import-body">
				<label class="sr-only" for="pg-json">JSON skema pertanyaan</label>
				<textarea
					id="pg-json"
					class="mono json"
					rows="5"
					spellcheck="false"
					placeholder={'{"mood": {"type": "choice", "criteria": {"calm": "nothing unusual"}}}'}
					bind:value={jsonImport}
				></textarea>
				<div class="import-row">
					<button type="button" class="tiny" onclick={importJson}>muat ke formulir</button>
					<span class="faint tiny">
						mengganti pertanyaan di atas; jawaban mengikuti tipe yang dipilih
					</span>
				</div>
				{#if jsonError}
					<p class="error" role="alert">{jsonError}</p>
				{/if}
			</div>
		</details>

		<div class="results" aria-live="polite">
			{#if loading}
				<p class="state pending mono tiny">
					menjalankan model di CPU… {elapsed} dtk
					<span class="faint">(tanpa GPU; tunggu sampai model selesai)</span>
				</p>
			{:else if error}
				<p class="state bad mono tiny">{error}</p>
			{:else if result && !result.laya.available}
				<p class="state bad mono tiny">
					model tidak tersedia: {result.laya.detail ?? 'tanpa keterangan'}
				</p>
			{:else if result}
				<p class="state mono tiny">
					<span class="faint">model</span> {result.laya.routing?.model ?? 'tidak diketahui'}
					{#if result.laya.latency_ms !== null}· {Math.round(result.laya.latency_ms)} ms{/if}
					{#if result.laya.routing?.reason}· {result.laya.routing.reason}{/if}
				</p>

				{#each answerEntries as [name, answer] (name)}
					<div class="answer">
						<div class="answer-head">
							<span class="q">{name}</span>
							<span class="chosen mono">{String(answer.answer ?? '—')}</span>
							<span class="conf mono tiny faint">
								p={answer.confidence === null ? '—' : answer.confidence.toFixed(3)}
							</span>
						</div>
						{#if answer.score_position !== null}
							<span class="mono tiny faint">
								posisi rubrik {answer.score_position.toFixed(3)}{#if answer.level_index !== null} · indeks tingkat {answer.level_index}{/if}
							</span>
						{/if}
						<ul class="probs">
							{#each sortedProbabilities(answer) as [option, probability] (option)}
								<li class:chosen={option === String(answer.answer)}>
									<span class="opt mono tiny">
										{option}
										{#if answer.legend?.[option] && answer.legend[option] !== option}
											<span class="faint">· {answer.legend[option]}</span>
										{/if}
									</span>
									<span class="track"><span class="fill" style:width={barWidth(probability)}></span></span>
									<span class="val mono tiny">{fmtPct(probability)}</span>
								</li>
							{/each}
						</ul>
						{#if sortedProbabilities(answer).length === 0}
							<p class="faint tiny">{noDistributionNote(answer)}</p>
						{/if}
					</div>
				{/each}

				{#if unexpected.length > 0}
					<p class="warn tiny">
						server juga menjawab {unexpected.join(', ')}; tidak diminta
					</p>
				{/if}

				{#if answerEntries.length === 0}
					<p class="faint tiny">model tidak memberikan jawaban untuk pertanyaan yang dikirim</p>
				{/if}

				<details class="state-text">
					<summary class="mono tiny faint">kondisi yang dikirim ke model</summary>
					<p class="mono tiny">{sentState}</p>
				</details>

				<p class="faint tiny">
					Skor model ini belum dikalibrasi untuk data pasar; gunakan sebagai saran.
				</p>
			{:else}
				<p class="state faint tiny">
					Belum dijalankan. Ubah kondisi dan pertanyaan, lalu tekan Jalankan Laya.
				</p>
			{/if}
		</div>
	</div>
</section>

<style>
	.playground {
		border-color: var(--border-strong);
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.panel-body {
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	.block {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.block-head {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.model {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		margin-left: auto;
	}

	.model-input {
		width: 130px;
	}

	.state-input {
		width: 100%;
		resize: vertical;
		line-height: 1.45;
		font-size: var(--fs-sm);
	}

	.qlist {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.qrow {
		display: flex;
		flex-direction: column;
		gap: 3px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 5px 6px;
		background: var(--bg-row-alt);
	}

	.qrow-top {
		display: flex;
		gap: 6px;
		align-items: flex-end;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 1px;
		flex: 1 1 auto;
		min-width: 0;
	}

	.field.narrow {
		flex: 0 0 110px;
	}

	.field input,
	.field select {
		width: 100%;
	}

	.criteria {
		width: 100%;
		resize: vertical;
		font-size: var(--fs-xs);
		line-height: 1.4;
	}

	.remove {
		flex: 0 0 auto;
	}

	.type-note {
		margin: 0;
	}

	.run-row {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		border-top: 1px solid var(--border);
		padding-top: 6px;
	}

	.run {
		border-color: var(--accent);
		color: var(--accent);
		font-weight: 600;
	}

	.schema summary,
	.import summary {
		cursor: pointer;
	}

	.schema pre,
	.import-body {
		margin: 4px 0 0;
		padding: 6px;
		background: var(--bg-sunken);
		border-radius: var(--radius);
		max-height: 26vh;
		overflow: auto;
		font-size: var(--fs-xs);
		line-height: 1.4;
	}

	.import-body {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.json {
		width: 100%;
		resize: vertical;
		font-size: var(--fs-xs);
		line-height: 1.4;
	}

	.import-row {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.results {
		border-top: 1px dashed var(--border-strong);
		padding-top: 6px;
	}

	.state {
		margin: 0;
		line-height: 1.45;
	}

	.state.pending {
		color: var(--text-dim);
	}

	.state.bad {
		color: var(--warn);
	}

	.answer {
		margin: 6px 0 0;
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

	.probs {
		list-style: none;
		margin: 3px 0 0;
		padding: 0;
	}

	.probs li {
		display: grid;
		grid-template-columns: minmax(90px, 30%) 1fr auto;
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

	.warn {
		color: var(--warn);
		margin: 5px 0 0;
	}

	.state-text {
		margin-top: 5px;
	}

	.state-text summary {
		cursor: pointer;
	}

	.state-text p {
		margin: 4px 0 0;
		color: var(--text-dim);
		line-height: 1.5;
	}

	.tiny {
		font-size: var(--fs-xs);
	}
</style>
