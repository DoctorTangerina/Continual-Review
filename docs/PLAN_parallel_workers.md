# Plan: parallel workers for `experiment run`

Status: **Part A implemented on 2026-11-16, in a different form** (next section). Part B
(`adopt-code`) and `tools/parallel_trial.py` are not implemented. The plan below was
written on 2026-11-13, while `swebench-03` was running its first condition (`none`,
checkpoint 1), and is kept as written.

## What was implemented (2026-11-16)

`run --workers N`, with the model calls of a local model going to **one** Ollama server
started with `OLLAMA_NUM_PARALLEL=N`, instead of one server per model as planned below.
There is no `--api-base` argument.

| Plan | Implemented |
|---|---|
| One Ollama server per parallel model call, `--api-base` repeated (A1, A2, A5) | One server with N slots. Memory holds the weights once and N context windows |
| The model is unloaded before every call, each server serving one call at a time | The model is unloaded once for a group of calls, which then start together (`backends.SharedLoad`). Per-call unloading cannot work with a shared server: Ollama waits for the calls in progress before it unloads |
| Worker threads take cases from a queue; a slot is taken inside `cached` (A3, A4) | The cases of a stage advance in groups of N, in manifest order, and a group takes one step at a time: N attempts, N harness evaluations, N extractions (`engine.perform`, `engine.together`). The harness does not overlap with model calls |
| `preflight_live` compares version and digest of every server (A6) | `preflight_live` measures that the server computes N calls at the same time (`backends.ollama_parallel_check`), because the API does not report `OLLAMA_NUM_PARALLEL` |
| `queued_seconds` in stage files, `api_base` in usage files (A8) | Not recorded. The `elapsed_seconds` of a stage includes the wait for the rest of its group. `run_started` in `audit.jsonl` records `condition` and `workers` |
| Step 0 trial before any change to the engine | Replaced by short measurements on a private server, made during the implementation (results in [PROTOCOL.md](PROTOCOL.md)). **Criterion 1 of the go / no-go failed for parallel slots**: one of six prompts changed its answer when another call ran at the same time |
| Part B, `adopt-code` | Not implemented: runs prepared before this change are refused by the new code, as the existing rule says |

The reasons for the group design, measured on Ollama 0.40.16:

- An unload requested while a call runs does not interrupt it; the model is unloaded
  when that call ends.
- A call that arrives after another has finished is given the slot that call used
  (chosen by common prompt prefix), even when another slot is empty. Only calls that
  start together on a freshly loaded model each get an empty slot.

What the measurements say about the two designs (7B model, CPU, two calls at a time,
short prompts; details in [PROTOCOL.md](PROTOCOL.md)):

| | One server, `OLLAMA_NUM_PARALLEL=2` (implemented) | Two servers, one slot each (planned below) |
|---|---|---|
| Answer to a pinned prompt, compared with the call alone | Changed for one of six prompts: 3 different answers in 4 groups | The same in 6 of 6 groups (one prompt tested) |
| Time for two calls | 44 s one after the other, 30 to 32 s together. Prompt reading was not faster in total; generation and the single model load were | 17.5 s per call alone, 32 to 39 s per call together: no gain |

By the plan's own rule ("If 1 fails, do not use parallel model calls with the local model
in experiment runs"), `--workers` above 1 with a local model is not suitable for runs
whose conditions are compared case by case. It remains usable with hosted providers and
for runs where that variation is accepted and reported. `run` prints a note in this
case, and each local `*.usage.json` records `parallel_cases`.

## Summary

Today `run` processes one case at a time: model call, harness, extractor call, then the
next case. This plan adds an argument to `run`:

```powershell
python experiment.py run --run-dir runs/swebench-03 --condition static --live --workers 2 `
    --api-base http://localhost:11434 --api-base http://localhost:11435
```

- `--workers N` — how many cases are in progress at the same time. Default `1`, which is
  today's behaviour.
- `--api-base URL` (repeatable, local Ollama models only) — the Ollama servers to use.
  Each server runs one model call at a time, so the number of servers is the number of
  models running at the same time. Without it, the server in the configuration is used.

Both are arguments of `run`, not configuration keys, so they are not frozen in the
manifest and can change from one invocation to the next.

Cases are only run in parallel inside one stage (the warmup, one adaptation block, one
checkpoint). Inside a stage the protocol already gives every case the same memory, so no
case's prompt depends on another case of the same stage. Parallel execution changes the
order in which the calls happen, not what is sent.

Three points shape the plan:

1. **As the code stands, any change under `src/ef_review/` blocks every prepared run**,
   `swebench-03` included. Being a `run` argument does not avoid this. The plan therefore
   includes an explicit, recorded step for a prepared run to accept new code (Part B).
   This is a change of a protocol rule and needs your confirmation.
2. **The gain on this machine is unknown and may be small.** The model is 83% of the
   time, it runs on the CPU, and one Ollama server answers one call at a time. Running
   two models means two servers sharing the same CPU and memory.
3. **Answers must stay identical.** The run relies on the same prompt giving the same
   answer. Whether that survives two models running at once has to be measured.

Points 2 and 3 are settled by a trial (Step 0) that touches no file of the engine and no
run. The engine is changed only if the trial passes.

## Evidence from the live run

Measured on `runs/swebench-03` at 21:30 UTC on 2026-11-13 (156 calls reserved, 6.55 hours
since the first call), from `audit.jsonl`:

| Stage | Count | Total | Mean | Longest | Share of stage time |
|---|---|---|---|---|---|
| Model call (`review`) | 86 | 4.88 h | 204 s | 814 s | 83% |
| Harness (`evaluation`, when a patch exists) | 55 | 0.80 h | 52 s | 144 s | 14% |
| Extractor call (`package`) | 13 | 0.21 h | 59 s | 199 s | 4% |

Other facts:

| Fact | Where |
|---|---|
| CPU only: i7-13700 (16 cores, 24 threads), 31.7 GB RAM, no NVIDIA GPU | `ollama ps` shows `100% CPU` |
| One loaded model uses 6.9 GB at the 32,768-token window; 12.5 GB of RAM were free with it loaded and the run active | `ollama ps`, Windows memory counters |
| The run design has 1,762 model calls; 99 are done. About 1,235 model calls and 428 extractor calls remain, roughly 85 to 90 hours at the mean times above (my estimate; no adaptation case has run yet) | `manifest.json`: 13 warmup, 214 adapt, 55 eval, 3 blocks |
| The same prompt gave the same answer: 18 of 18 cases of `none` have byte-identical `review.raw.txt` in checkpoint 0 and checkpoint 1 | `conditions/none/checkpoint-{0,1}` |
| `load_run` refuses a run when any `*.py` in `src/ef_review/` differs from `prepare` time; `run`, `status` and `report` all go through it | [engine.py](../src/ef_review/engine.py) `load_run`, [reporting.py](../src/ef_review/reporting.py) |
| The budget is updated by read, add one, write, with no lock between threads | [engine.py](../src/ef_review/engine.py) `cached` |
| The model is unloaded before every call and the call waits until the server no longer lists it | [backends.py](../src/ef_review/backends.py) `ollama_unload` |
| Ollama runs the model with one slot | [PROTOCOL.md](PROTOCOL.md), "Escolhas implementadas" |

What follows from the timing table:

- Overlapping only the harness with the model (one server, several workers) can make the
  run at most 1.16 times faster, because the model calls still run one after another.
- A real gain needs model calls in parallel. With `W` servers, each call slowed by a
  factor `s` because they share the CPU, the model part runs `W / s` times faster. `s` is
  not known. On a CPU, generation is limited by memory speed, so `s` can be close to `W`.
- Memory allows two servers on this machine (2 × 6.9 GB plus Docker and WSL). A third
  does not fit in what was free.
- `review_timeout_seconds` is 1800 in `swebench-03` and frozen. The longest call took
  814 s alone. If two models slow each call by more than 2.2 times, that case times out,
  and a timeout stops the run.

## Decision to confirm before implementing

**How `swebench-03` (and any other prepared run) gets the new code.** The check in
`load_run` exists so that one run is produced by one version of the code. Three options:

| Option | Effect on `swebench-03` | Cost |
|---|---|---|
| **A. Add an explicit `adopt-code` step (Part B).** Recommended if `swebench-03` should use workers | Continues with the new code after one recorded command | The rule "one run, one code version" becomes "one run, a recorded list of code versions". The program cannot prove the new code behaves the same; that rests on the tests and on the checks listed in Part B |
| B. Leave the rule as it is; `swebench-03` finishes with today's code | No change, about 85 to 90 more hours | Workers are only available from the next prepared run. The code must not be edited in this folder until `swebench-03` is reported |
| C. Stop `swebench-03` and prepare a new run with the new code | Discards 6.5 hours of compute and the warmup curation | The rule stays intact |

Part A does not depend on this choice. Without option A, skip Part B.

## Step 0 — trial, before any change to the engine

A script `tools/parallel_trial.py`. `tools/` is not part of the code hash, and the script
only imports `ef_review.backends` unchanged, so it blocks nothing.

- Input: a finished run folder as reference (`swebench-03`, `conditions/none/checkpoint-0`),
  a list of Ollama servers, and the number of cases.
- It picks cases across the range of prompt sizes, including the one that took 814 s. For
  each case it rebuilds the prompt from the manifest (`{"task": case["public"],
  "experiences": []}`) and calls `backends.call_model` with the run's frozen
  configuration, replacing only `api_base`.
- Three rounds: one server on a new port alone; two servers with two threads; three
  servers only if memory allows.
- Output goes to a folder outside any run. It reserves no call in any budget.
- For each round it reports: whether each `review.raw.txt` has the same SHA-256 as the
  reference, the time of each call, and cases completed per hour.

A second server is started in another terminal (confirm the variable name against the
installed Ollama 0.40.16 before relying on it):

```powershell
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama serve
```

Both servers read the same model files, so nothing is downloaded again.

Run the trial when no experiment run is active: it competes for the CPU, which distorts
the timings of both, and it must never use the server a live run is using, because the
unload before each call would interfere with that run's call in progress.

**Go / no-go.** Continue to Part A for local models only if all three hold:

1. Every answer in every round is byte-identical to the reference.
2. Two servers complete at least 1.3 times as many cases per hour as one (suggested
   threshold; below it the gain does not justify the change).
3. The longest call stays under about 1,350 s (75% of the 1,800 s timeout).

If 1 fails, do not use parallel model calls with the local model in experiment runs. If
only 2 or 3 fail, the feature is still useful for hosted providers
(`configs/swebench.nvidia.json`), where nothing is shared between calls on this machine.

## Part A — workers in the engine

### A1. Arguments ([cli.py](../src/ef_review/cli.py))

```python
run.add_argument("--workers", type=int, default=1,
                 help="Cases in progress at the same time; 1 = one after another")
run.add_argument("--api-base", action="append",
                 help="Local Ollama servers to use, one model call at a time on each; repeatable")
```

Validation, before any call:

- `--workers` is an integer from 1 to 16.
- `--api-base` is refused for the demo backend, for `claude_cli` and for hosted
  providers. The URLs must be distinct.

Pass both to `run_condition(root, condition, live=..., retry=..., workers=..., api_bases=...)`.
The defaults keep `examples/run_demo.py` and every existing test unchanged.

### A2. Execution settings ([engine.py](../src/ef_review/engine.py))

A small object built once per `run_condition` call:

- `workers`: the number of worker threads.
- `model_slots`: a queue of slots. A thread takes a slot before a model or extractor call
  and returns it after.
  - Local Ollama model: one slot per server, holding that server's URL. This keeps the
    rule "unload, then call" exclusive on each server.
  - Hosted provider or `claude_cli`: `workers` slots with no URL, so up to `workers`
    calls run at once.
  - Demo backend: no slot is needed.
- `lock`: one `threading.Lock` for the budget and the audit file.

With `--workers 4` and one Ollama server, model calls still run one at a time and only
the harness overlaps with them (the 1.16 limit above). This is allowed and stated in the
help text.

### A3. `cached` ([engine.py](../src/ef_review/engine.py))

- Hold `lock` while reading, increasing and writing `budget.json`, and for each line
  appended to `audit.jsonl`. These are the only files shared between cases; every other
  file is inside the case's own event folder.
- New optional argument `slot`. When given, `cached` takes a slot **after** the cache
  check and the failed-stage check and **before** the budget is charged and the timer
  starts. A cached stage therefore never waits, and `elapsed_seconds` keeps its meaning
  (time of the operation). The wait is written as `queued_seconds` in the stage file.
- The slot's URL is passed to the operation.

### A4. Running the cases of a stage ([engine.py](../src/ef_review/engine.py))

One helper replaces the three `for case in manifest["cases"]` loops (in `seed`, in the
`adapt` branch and in the checkpoint branch of `run_condition`):

```python
def run_cases(cases: list[dict], work: Callable[[dict], T], execution) -> list[T]:
    """Run `work` for each case; results are returned in the order of `cases`."""
```

- `workers == 1`: a plain loop in the calling thread, in manifest order. No thread is
  created. This is the same path as today.
- `workers > 1`: worker threads take cases from a queue. Results are stored by position,
  so `candidates.json` and the memory keep the manifest order whatever the order of
  completion. This matters because the candidates' hash and the memory hash depend on
  the order.
- The checkpoint branch writes `case.json` inside `work`, since it is a file of the
  case's own folder.

The memory list is shared between threads and only read (`retrieve` builds new lists).

### A5. Which server a call uses ([engine.py](../src/ef_review/engine.py), `perform`)

`perform` passes `cfg | {"api_base": url}` to `backends.review` and `backends.extract`
when the slot has a URL. `call_model`, `ollama_unload` and `ollama_identity` already read
`api_base` from the configuration they receive, so [backends.py](../src/ef_review/backends.py)
needs only one change: write `api_base` into the usage record of local calls.

The harness call takes no slot. At most `workers` harness processes run at once, each in
its own event folder with its own `run_id` and container name, as today.

### A6. Checks before the first call ([backends.py](../src/ef_review/backends.py), `preflight_live`)

For a local model, for every server in the list:

- `ollama_identity` must answer; the model digest must equal the manifest's
  (`runtime.ollama.model_digest`), as today for the configured server.
- All servers must report the same Ollama version.

Refuse the run otherwise, before any call.

### A7. Failures and interruption

- First failure in a stage: no new case is started; cases already in progress finish and
  are saved; then the run stops with the error of the earliest failed case in manifest
  order, and the message says how many other cases failed. Each failure has its own
  `*.failed.json` and audit line, as today. `--retry-failed` is unchanged.
- Call limit reached: the same path. `cached` raises before the call is made.
- Ctrl+C: the main thread waits for the workers with a short timeout in a loop, so the
  interrupt is received on Windows. First Ctrl+C: no new case is started, the program
  prints how many calls are in progress and waits for them. Second Ctrl+C: the program
  leaves; this is the same as killing the process today, except that up to `workers`
  stages can be left with a call reserved and no result, and up to `workers` harness
  containers can remain.
- `RunLock` is unchanged: one process per run. The workers are threads of that process.

### A8. What is recorded

- `audit.jsonl`: one line at the start of every `run` invocation,
  `{"action": "run_started", "condition", "workers", "api_bases", "code_sha256"}`. With
  the timestamps this shows under which settings and code each call was made.
- `*.usage.json` of local calls: `api_base`.
- Stage files: `queued_seconds`.
- [tools/run_stats.py](../tools/run_stats.py): add `queued_seconds` and `api_base` to
  the per-event rows, and the worker settings of each invocation to the report.

## Part B — a prepared run accepts new code (only with option A)

### B1. Command ([cli.py](../src/ef_review/cli.py), [engine.py](../src/ef_review/engine.py))

```powershell
python experiment.py adopt-code --run-dir runs/swebench-03 --operator "<name>" --reason "<why>"
```

Under `RunLock`, it:

1. Checks the manifest as `load_run` does today (manifest hash, dataset, benchmark
   checkouts).
2. Refuses if the prompts changed: `digest(backends.prompts(cfg))` must equal the
   manifest's `prompts_sha256`. Today that value is recorded and never checked.
3. Refuses if the current code hash already equals the accepted one.
4. Appends to `code_adoptions.json` in the run folder:
   `{"at", "operator", "reason", "from_code_sha256", "to_code_sha256", "files": <hash of each source file>}`.
5. Appends a `code_adopted` line with the same hashes to `audit.jsonl`.

`manifest.json` is not edited, so its hash stays valid and the README rule about not
editing the manifest stays true.

### B2. `load_run`

The accepted code hash is the `to_code_sha256` of the last entry of
`code_adoptions.json`, or the manifest's `code_sha256` when the file does not exist. Any
other code is refused with today's message, plus a mention of `adopt-code`.

### B3. Reports ([reporting.py](../src/ef_review/reporting.py))

`summary.json` gets `code_adoptions` (the list). `report.md` gets one line per adoption:
date, operator, reason.

### B4. What supports an adoption, and what does not

- The program does not verify that the new code behaves like the old one. The operator
  states it in `reason`.
- Supports for this specific change: with `--workers 1` the engine follows the same path
  as before (test below); stages that are already saved keep their input hash, and
  `cached` refuses a saved stage whose inputs the new code computes differently; the
  prompts are checked by B1.
- Limit: completed actions are not visited again on resume, so that input check only
  covers the stage in progress.

## Tests

`tests/test_experiment.py`, all on the synthetic demo:

- A full run of the four conditions with `workers=3` produces the same `candidates.json`,
  `seed.json`, snapshots, `state.json`, `case.json` files and report as with `workers=1`,
  and `budget.json` has the same count.
- 200 threads charging the budget through `cached` leave `reserved_calls == 200` and 400
  well-formed audit lines.
- Results keep manifest order when the first case is made the slowest.
- With a patched model call that records entry and exit: no two calls overlap on the
  same server; with two servers two calls do overlap; hosted providers overlap up to
  `workers`.
- A failure in one case: cases in progress are saved, no later case starts, the error
  names the earliest failed case, and a second run with `retry=True` repeats only the
  failed stage.
- The call limit reached in the middle of a stage stops the run without exceeding
  `max_calls`.
- `preflight_live` refuses a server with another digest or another version.
- `--api-base` is refused for demo, hosted and `claude_cli` configurations; `--workers 0`
  is refused.
- A cached stage does not take a slot.
- Part B: `load_run` refuses changed code; after `adopt-code` it accepts it; a second
  change is refused again; changed prompts make `adopt-code` refuse; the adoption is in
  `summary.json`.

## Documentation changes

The project documentation is in Portuguese; the wording below is what to add.

**[README.md](../README.md), section 4, after the four `run` commands:**

> `--workers N` processa até N casos ao mesmo tempo dentro de uma etapa (aquecimento, um
> bloco de adaptação ou um checkpoint). O padrão é 1. Os casos de uma mesma etapa já
> recebem a mesma memória, então o paralelismo muda a ordem das chamadas, não o que é
> enviado. Com modelo local do Ollama, cada servidor atende uma chamada por vez: para
> rodar dois modelos ao mesmo tempo, iniciar um segundo servidor em outra porta e passar
> os dois com `--api-base`. O programa confere versão e digest do modelo em cada
> servidor. Os valores usados ficam em `audit.jsonl` (`run_started`) e não entram no
> manifesto.

**[README.md](../README.md), section 5:**

> Com `--workers` maior que 1, uma falha não interrompe os casos já em andamento: eles
> terminam e são salvos, nenhum caso novo começa, e o processo para com o erro do
> primeiro caso que falhou na ordem do manifesto. O primeiro Ctrl+C espera as chamadas em
> andamento; o segundo abandona o processo, e até N etapas podem ficar com chamada
> reservada e sem resultado.

With option A, also in section 3, replacing "Para outro modelo, outra ordem, outro
orçamento ou código alterado, preparar um run novo":

> Para outro modelo, outra ordem ou outro orçamento, preparar um run novo. Código
> alterado também exige um run novo, exceto quando a mudança não altera o que é enviado
> ao modelo, como as respostas são interpretadas e avaliadas, nem a memória; nesse caso
> `adopt-code` registra no run quem aceitou o código novo, quando e por quê.

**[docs/PROTOCOL.md](PROTOCOL.md), "Escolhas implementadas":** a paragraph with the
result of Step 0 (date, Ollama version, number of servers, number of prompts compared,
identical or not), the statement that times per call measured with more than one worker
are not comparable with times measured with one, and, with option A, the adoption rule.

## Verification after implementing

1. `python -m unittest discover -s tests` passes.
2. `python examples/run_demo.py` on a new folder gives the same report as before the
   change.
3. Real check on a new run prepared from `manifests/swebench_smoke.csv`: run the warmup
   once with `--workers 1` and, in a second run folder, with `--workers 2` and two
   servers. Every `review.raw.txt` and `package.raw.txt` pair has the same SHA-256.
4. During step 3, `docker ps` shows two harness containers at once without errors in
   either `harness.log`.
5. With option A: on `swebench-03`, after `adopt-code`, `status` works, and the next
   `run` reuses every saved stage without a new call for them.

## Rollout

1. Step 0 at a moment when `swebench-03` is stopped (between two conditions). Decide go
   or no-go.
2. Develop Parts A and B in a separate Git worktree or branch, not in this folder. The
   running process is not affected by edits on disk, but the next `run`, `status` or
   `report` in this folder would be refused.
3. Bring the change into this folder only when `swebench-03` is stopped. Then run
   `adopt-code` (option A) and continue with `--workers`.
4. Use one worker setting for a whole condition where possible, and keep the same
   setting for the conditions whose times will be compared.

## Risks

| Risk | Consequence | Handling |
|---|---|---|
| Answers differ when two models run at once | Conditions run under different settings would not be comparable | Step 0, criterion 1; verification step 3 |
| Calls slow down enough to hit the 1,800 s timeout | The stage fails and the run stops; a retry reserves another call | Step 0, criterion 3 |
| Two servers plus Docker exceed the RAM | Paging, very slow calls, timeouts | Two servers at most on this machine; watch memory in Step 0 |
| `none` in `swebench-03` ran with one worker, the rest with two | Time per call is not comparable between conditions; resolved counts are, if answers are identical | State it in PROTOCOL.md; `run_started` lines record the settings |
| Hosted provider rate limits with several workers | A refused call is a failed stage (no automatic retry) | Choose `--workers` within the provider's limit |
| Two harness processes pull the same image layers at once | Not observed yet | Verification step 4 |

## Out of scope

- Running several conditions at once in separate processes. `RunLock` allows one process
  per run, and workers inside a stage give the same benefit.
- Starting and stopping the extra Ollama servers from the program.
- Sending `num_thread` to Ollama to divide the CPU between servers. It would change what
  is sent in each call, so it is a configuration key and a new run, and whether the
  thread count changes the answer is not verified.
- Several calls at once on one Ollama server. It runs one slot, and the unload before
  each call needs the server to itself.
- Reusing answers between checkpoints. In `none` and `static` the memory never changes,
  so each of the 55 evaluation cases is asked the same prompt at all four checkpoints:
  330 calls of the design repeat a prompt already answered (18 of them are done, and all
  18 gave the same answer as the first time). Skipping them would save more time than a second server may
  give, but it changes what a checkpoint measures and is a protocol decision of its own.
