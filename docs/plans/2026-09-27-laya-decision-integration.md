# Laya decision integration + benchmark — implementation plan

> Multi-step work. Verification convention for this repo is `scripts/smoke.sh` plus focused
> measurement scripts, not pytest: the repo has no test framework and adding one would establish a
> second convention. Every task below ends in an observable command.

**Goal:** Make `laya-research` actually run the Laya model (Convai Innovations, Apache 2.0) against
the existing 7,056,000-row grocery datasheet, surface Laya's typed decisions with probabilities in
the live UI, and publish a measured benchmark of the model against the repository's rule engine.

**Architecture:** A new `laya` container runs `laya-serve`, which exposes `POST /v1/systemone`. That
is the same request/response shape TypeSafe Jev serves, so the API talks to a decision model over
HTTP and never imports torch. The API composes three typed questions from each `(product, day)`
rollup state, one per Laya primitive, and returns Laya's answers next to the rule engine's verdict.
The benchmark harness replays the same states through both decision layers and scores them.

**Tech stack:** Laya 0.3.20 (`torch`, `transformers`, `safetensors`), FastAPI, Postgres 17,
SvelteKit 5, Caddy, Docker Compose.

---

## Host reality (measured during planning, before any model work)

| | value | consequence |
|---|---|---|
| CPU | Intel i5-10310U, **4 physical cores**, 8 threads, 1.7 GHz base, 15 W | vendor's CPU figures come from a 4-core EPYC 9R14; expect this to be slower |
| GPU | none | CPU only, `LAYA_DEVICE=cpu`, no `tilelang` fast path |
| RAM | 15 GiB total, ~8 GiB available | **do not preload multiple checkpoints**: 5 resident checkpoints peaked at 9.3 GiB for the vendor |
| disk | 94% used, 31 GiB free after pruning 11.3 GiB of build cache | one checkpoint ~800 MB, cache in a named volume |
| other load | 4 containers from two unrelated projects, load average ~1.7 | do not saturate all 8 threads |

### Vendor numbers this plan is calibrated against

From `BENCHMARKS.md` in the upstream repo, which is unusually candid:

- **CPU, 4-core EPYC**: `english` 580 ms for 1 question, ~600 ms per question up to 10. `multilingual` is
  **~3x faster at 185 ms/question**. Batching saves little on CPU, unlike on GPU.
- **Thread pinning is a 12x factor.** torch's defaults (10 intra-op, 5 inter-op) gave **9,396 ms** p50;
  pinning intra-op to physical cores and inter-op to 1 gave **783 ms**, with no code change.
- **Base checkpoints are near chance zero-shot.** `laya` 0.361 and `laya-multilingual` 0.352 on the
  typed-decisions benchmark, both **below the majority-class baseline of 0.461**. The 0.766 belongs to
  the fine-tuned checkpoint. All capability on that benchmark comes from fine-tuning.
- **`score` is the weakest primitive** (SST-5 0.372). Our `severity` question is a `score`, so expect it
  to be the worst of the three and report it as such.
- **Keep `choice` under ~20 options.** Ours has 10, which is safe.
- **Both checkpoints ship over-confident**; temperature fitting on your own data is the documented fix.

### Consequences for the benchmark design

At ~600 ms per question on a comparable CPU and 3 questions per state, one call is ~1.8 s on the EPYC
and plausibly 3-5 s here. So:

- default sample **200 states**, not 2400, with `--limit` to raise it;
- all three questions in **one** call per state, never three calls;
- run the benchmark in the background and report wall time;
- if `laya-serve` does not pin inter-op threads, replace it with a ~40-line wrapper that sets
  `torch.set_num_threads(<physical cores>)` and `torch.set_num_interop_threads(1)` before serving,
  still exposing `POST /v1/systemone`. The probe in Task 2 decides which.

---

## Why the questions are shaped this way

Laya exposes exactly three primitives. Using all three is the point of the exercise, and each one
gets a deterministic label from the existing rule engine so accuracy is computable:

| question | primitive | options / rubric | ground-truth label from |
|---|---|---|---|
| `pattern` | `choice` | the 9 pattern ids + `none` | `api/app/rules.py` |
| `severity` | `score` | `none`, `info`, `warn`, `critical` | rule severity, `none` when silent |
| `reorder_now` | `noul` | P(true) | `STOCKOUT_RISK` at `warn` or `critical` |

**Honest framing, and it must appear in the published report:** the labels come from the rule
engine, so this measures **agreement with a threshold expert**, not truth. The defensible question
is "can a 421M decision model reproduce a threshold expert's calls, and are its probabilities
calibrated when it does". That is answerable with data and worth publishing. "Laya beats my rules"
is not answerable without human labels and will not be claimed.

---

## Task 1: Laya service image

**Files:**
- Create: `laya/Dockerfile` (done)
- Create: `laya/requirements.txt` (done)

**Step 1:** `docker build -t laya-research-laya:dev ./laya`
Expected: builds; `pip install` pulls torch CPU wheel plus transformers/safetensors.

**Step 2:** Confirm the entrypoint exists.

```bash
docker run --rm --entrypoint sh laya-research-laya:dev -c 'command -v laya-serve && laya-serve --help 2>&1 | head -20'
```
Expected: a path plus usage text. If `laya-serve` is missing, `laya[serve]` did not install and the
extras name is wrong.

---

## Task 2: Verify CPU inference is real, and measure it

This gates everything. No GPU on the host, so the model card's 193-464 ms CPU figure has to be
reproduced locally before any integration work is worth doing.

**Files:**
- Create: `scripts/laya_probe.py`

**Step 1:** Write the probe. It loads the router, sends one ticket-shaped state with all three
primitives, prints the raw answers, routing metadata, and the wall time.

**Step 2:** Run it against the image with a checkpoint cache volume.

```bash
docker run --rm -v laya_hf:/models --entrypoint python laya-research-laya:dev /app/scripts/laya_probe.py
```
Expected: three answers, a `routing` block, and a latency number. Record it.

**Step 3:** If the English checkpoint cannot be loaded in the available RAM, fall back to
`laya-multilingual` (322M) or the tipn checkpoint, and record which.

**Recorded:** `____ ms` per forward pass, checkpoint = `____`.

---

## Task 3: Fix the question schema in code

**Files:**
- Create: `api/app/laya.py`
- Modify: `api/app/rules.py` (expose the per-subject state the questions are built from)

**Step 1:** Add `state_for(cur, product_id, day)` returning the rollup state dict already assembled
for the rules: today's units / avg_price / margin / inventory / store_count / promo_stores, plus the
14-day medians and observation count, plus product name, category, brand, private-label and
perishable flags.

**Step 2:** Add `build_questions(state)` returning the three typed questions with `instructions` and
`criteria` exactly as the Laya API expects. Keep the option dictionary in one place so the benchmark
can score against it.

**Step 3:** Add `render_state(state)` producing the compact text Laya reads. One line, stable key
order, no JSON punctuation noise. The English checkpoint reads 512 tokens, so the state must be
small.

**Step 4:** Verify the rendered state round-trips and the criteria dictionary matches the nine rule
ids.

```bash
python3 -c "from app.laya import PATTERN_OPTIONS; print(len(PATTERN_OPTIONS), PATTERN_OPTIONS)"
```
Expected: `10 ['none', 'DEMAND_SURGE', ...]` — nine patterns plus `none`.

---

## Task 4: Laya HTTP client and `/api/decide`

**Files:**
- Create: `api/app/laya.py` (client half)
- Modify: `api/app/main.py` (routes)
- Modify: `api/app/schemas.py` (response models)
- Modify: `docker-compose.yml` (the `laya` service)

**Step 1:** Add the service to compose. Named volume for `HF_HOME=/models`, `LAYAY_DEVICE=cpu`,
`LAYA_PRELOAD=1`, healthcheck on `GET /v1/systemone` shape, and **no published port**: the server
binds `0.0.0.0` with no auth unless `LAYA_API_KEY` is set, so publishing it would put an
unauthenticated model endpoint on the host.

**Step 2:** Add `LAYA_URL` (default `http://laya:8000`) and `LAYA_TIMEOUT` to api settings.

**Step 3:** Implement `decide(state)` posting to `/v1/systemone` with the three questions, returning
answers, probabilities, routing metadata and latency. Handle Laya being down by returning a
`{"available": false, "detail": ...}` payload rather than raising, so the dashboard keeps working.

**Step 4:** Add routes.

- `GET /api/laya/health` → model availability, loaded checkpoint, measured latency
- `GET /api/decide/{product_id}?day=` → Laya's three answers **plus** the rule engine's verdict for
  the same state, side by side, plus `agreement` booleans

**Step 5:** Verify.

```bash
curl -fsS 'localhost:8090/api/laya/health' | python3 -m json.tool
curl -fsS 'localhost:8090/api/decide/2177' | python3 -m json.tool
```
Expected: a model block with a latency, then typed answers with probability distributions for each
primitive, next to the rule verdict.

---

## Task 5: Surface Laya in the dashboard

**Files:**
- Create: `web/src/lib/components/LayaVerdict.svelte`
- Modify: `web/src/lib/api.ts`, `web/src/lib/types.ts`
- Modify: `web/src/lib/components/ProductDetail.svelte`

**Step 1:** `LayaVerdict` renders, for one product: the three questions, Laya's answer for each, and
the probability distribution as horizontal bars. Show the routing metadata and the latency. Show
whether it agreed with the rule engine, marked plainly, without spin.

**Step 2:** Wire it into `ProductDetail` so clicking a row in the result table shows it. Fetch on
open, with a loading state, because CPU inference takes hundreds of milliseconds.

**Step 3:** `npx svelte-check` and `npm run build` must both pass.

---

## Task 6: Benchmark harness

**Files:**
- Create: `scripts/bench_laya.py`
- Create: `docs/BENCHMARK.md` (generated output, committed)

**Step 1:** Export labels. Call `POST /api/patterns/scan {"persist": false}` for the target day and
build `{(product_id, day) -> {pattern, severity, reorder}}`.

**Step 2:** Build states for the sample. Default: the newest dataset day, all products carried that
day. `--limit N` for a stratified subset. Print `N` and the class balance before spending CPU, and
abort if a class is empty.

**Step 3:** For each state, call Laya with all three questions in one request, recording answers,
probabilities and latency.

**Step 4:** Score.

- `choice`: accuracy, macro-F1, per-class precision/recall, confusion matrix
- `score`: accuracy, mean absolute error on the 0-3 ordinal, Spearman correlation
- `noul`: Brier score, log loss, AUC, and a 10-bucket reliability table with ECE

**Step 5:** Report the baseline to beat. Always include the trivial baselines (majority class for
`choice`, `none` for `severity`, and the all-negative rate for `noul`) so an accuracy number cannot
be read as skill.

**Step 6:** Run for both checkpoints and write `docs/BENCHMARK.md` with the measured numbers and the
latency percentiles. Report whatever it says.

```bash
python3 scripts/bench_laya.py --checkpoint english --limit 500
python3 scripts/bench_laya.py --checkpoint typed-decisions --limit 500
```
Expected: a report with accuracy, calibration and latency, plus explicit baselines.

**Step 7:** Commit the report and the raw JSON alongside it.

---

## Task 7: Documentation and acceptance

**Files:**
- Modify: `docs/CONTRACT.md` (the `laya` service, its endpoints, the question schema)
- Modify: `README.md` (architecture diagram, the Laya section, measured numbers, limits)
- Modify: `.env.example` (LAYA_URL, LAYA_CHECKPOINT, bench sample size)
- Modify: `scripts/smoke.sh` (Laya checks)
- Modify: `Makefile` (`laya-probe`, `bench` targets)

**Step 1:** Document the question schema in CONTRACT.md with the option dictionaries frozen, so the
benchmark and the API cannot drift.

**Step 2:** Extend `smoke.sh`: `/api/laya/health` reachable, `/api/decide/{id}` returns all three
primitives with probabilities, and Laya being unreachable degrades gracefully rather than 500s.

**Step 3:** Add a README section stating plainly that the repo now runs Laya, what the benchmark
measured, and that the labels come from the rule engine.

**Step 4:** Full acceptance on the assembled stack.

```bash
scripts/smoke.sh
make bench
```
Expected: all checks pass, benchmark report regenerated from live services.

---

## Risks and pre-agreed fallbacks

| risk | fallback |
|---|---|
| CPU inference too slow at 2400 states | reduce the sample to 500 and report throughput honestly |
| base English checkpoint is weak zero-shot (0.362 on Convai's own benchmark) | report it; also run `typed-decisions`, and state clearly that a substantive comparison requires fine-tuning on this domain |
| host disk (94% used, 31 GB free at planning time) | keep HF cache in a named volume, drop the image after measuring if needed |
| no GPU, no `tilelang` fast path | pin `LAYA_DEVICE=cpu`, never claim GPU-class latency |
| `laya-serve` has no batch endpoint | sequential requests; report per-request latency and total wall time |

---

## What actually happened

Recorded because the surprises are the useful part.

**Resolved as planned.** `laya-serve` was the right service shape: it pins inter-op threads itself,
so the planned fallback wrapper was never needed. Measured p50 for a 3-question call on an i5-10310U:
18,159 ms with torch defaults and all checkpoints preloaded, 8,025 ms pinned, 7,833 ms through
`laya-serve`. Load time dropped from 770 s to 9 s once `Router(preload=True)` was replaced with a
single checkpoint.

**The sample was cut twice over.** 3 questions cost 6.3 s p50 here, not the 1.8 s extrapolated from
the vendor's 4-core EPYC. 150 states was the right size.

**A bug worth naming: `LAYA_MODELS` does not select the answering model.** It only preloads. The
Router stays on `auto` and routes English text to `english` regardless, so a run labelled
`typed-decisions` silently measured `english` and produced a byte-identical confusion matrix. That
identity is what exposed it. Two fixes: compose now sets `LAYA_MODEL` and `LAYA_MODELS` from one
variable, and `bench_laya.py` records `routing.model` per record, fails a pre-flight if the served
checkpoint is not the requested one, and refuses to score a run with mixed checkpoints.

**`--fresh` did not truncate.** The output file is opened in append mode, so ignoring prior records
left them on disk. Harmless for scoring (the report only used the new records) but wrong: it now
deletes the file.

**`sim` had to be stopped for the run.** It mutates the newest dataset day every 4 seconds, so labels
scanned at the start would drift from states decided minutes later, and the target day is exactly the
day being labelled. `make bench` stops and restarts it.

**Stopping `sim` was not enough, and that produced a false claim.** The first valid pair of runs was
followed by an acceptance run, which restarts `sim`; the next benchmark then measured a dataset that
had already moved, so the two reports were no longer comparable. The README briefly claimed "both
checkpoints were served the identical states", which was not true for that pair. Discipline is not a
guard, so `GET /api/dataset/fingerprint` was added: an md5 over every mutated column of every fact row
for the day, which changes on every tick while `sim` is live and is byte-stable while it is stopped.
`bench_laya.py` now records it before and after a run and aborts if it moved, every report carries it,
and `bench_compare.py` refuses to publish a table whose reports disagree. Both checkpoints were then
re-run back to back against one frozen dataset.

Two smaller traps found the same way, both now guarded:

- **`--checkpoint` was decorative.** It only labelled the report: the model that answered was whatever
  the container happened to serve, and it happened to be `english` for every run because of the
  Router's language routing. The harness now records `routing.model` per sample and asserts it.
- **`--fresh` did not truncate** the JSONL, so stale records from an earlier dry run sat in the file.
  It now deletes the file.
