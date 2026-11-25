# Plan: pinned sampling (temperature and seed) for model calls

Status: **implemented 2026-11-13** (Part A including A4, and Part B), with
`review_temperature = 0` and `review_seed = 0` as the defaults. Written 2026-11-12, after
runs `swebench-01` and `swebench-02`. The text below is the plan as it was written.

Verification step 3 did **not** show reproducibility: runs `pinned-check-a` and
`pinned-check-b` gave different answers to the same warmup prompt at temperature 0 and
seed 0. The answer depends on the server's prompt-cache state. This is recorded as a
limit in [PROTOCOL.md](PROTOCOL.md) ("Escolhas implementadas"), and the claim in the
Summary that the same input then always gives the same output does not hold with
pinned sampling alone. Ollama has no switch for that reuse, so a third key was added,
`review_unload_model` (default `true`): the model is unloaded before every call. With
it, runs `pinned-check-c` and `pinned-check-d` have identical raw outputs.

## Summary

Today every reviewer and extractor call samples at the provider's default temperature
with no seed, so the same input gives a different answer on every run. This plan adds
two configuration keys, `review_temperature` and `review_seed`, sends them on every
call, and records them.

Pinning has one consequence that makes a second change mandatory: once the same input
always gives the same output, `--retry-failed` can no longer get past a malformed
answer. A case whose answer is `{}` would block the run forever. So the plan has two
parts, and **Part B must land together with Part A**:

- **Part A** — pin and record temperature and seed.
- **Part B** — a malformed model answer becomes a recorded failed attempt for that
  case instead of stopping the run.

Both parts change files under `src/ef_review/`, so existing run folders cannot be
resumed afterwards (`load_run` refuses when the code hash differs). The change takes
effect in a newly prepared run.

## Evidence

| Observation | Where |
|---|---|
| Same input (hash `469f2a19…`, `pydata__xarray-3364`) gave a 417-token patch in one run and `{}` in the next | `runs/swebench-01` and `runs/swebench-02`, `warmup/3ae050f310f0bca1/review.raw.txt` |
| Same input (`pallets__flask-5063`) gave an applicable patch in one run and a non-applicable one in the next | same runs, `warmup/9b5e660aa3d9eb76/review.json` |
| No `temperature` or `seed` is sent | `call_model` in [backends.py](../src/ef_review/backends.py) |
| The model has no parameters of its own, so Ollama's default temperature applies | `ollama show qwen2.5-coder:14b --parameters` prints nothing |
| LiteLLM 1.108.4 forwards both parameters to Ollama | `litellm/llms/ollama/chat/transformation.py`, `map_openai_params` |

A direct check on 2026-11-12 (Ollama 0.40.16, llamacpp runner, CPU,
`qwen2.5-coder:14b` Q4_K_M, called through LiteLLM with the run's `num_ctx` and JSON mode):

| Setting | Result |
|---|---|
| defaults, same prompt twice | two different answers |
| `temperature=0, seed=0`, same prompt three times, an unrelated prompt in between | three identical answers |
| `temperature=0.8, seed=7`, same prompt twice | two identical answers |

That check used short prompts (under 200 tokens). It does not cover long prompts,
where Ollama reuses cached prompt state, nor GPU execution, other thread counts, other
Ollama versions or hosted providers. See "Verification" below.

## Decision to confirm before implementing

Confirmed 2026-11-13: temperature `0`.

**Which temperature.** The plan recommends `0` with a fixed seed.

- With `0`, two conditions that send the same prompt for a case get the same answer, so
  a difference between `none` and a memory condition on one case comes from the
  retrieved experiences, not from sampling. That is the comparison the study makes.
- The cost: each case is a single deterministic draw, so the run gives no estimate of
  sampling variance, and a degenerate answer for a case is permanent (hence Part B).
- The alternative is to keep sampling (for example `0.8`) with a fixed seed, and later
  repeat the run with other seeds to measure variance. The two keys support that
  without further code changes: one prepared run per seed.

The rest of the plan does not depend on this choice; only the default value does.

## Part A — pin and record sampling

### A1. Configuration ([core.py](../src/ef_review/core.py))

Add to `CONFIG_DEFAULTS`:

```python
# Sampling of reviewer/extractor calls. null = provider default (not reproducible).
"review_temperature": 0,
"review_seed": 0,
```

Validate in `config_load`:

- `review_temperature`: `null`, or a number from 0 to 2 (reject `bool`).
- `review_seed`: `null`, or a non-negative integer (reject `bool`).
- `review_runner == "claude_cli"`: both must be `null`. The CLI has no such controls,
  and the existing rule for `review_reasoning` is to refuse rather than silently ignore
  a setting. Error message should say to set both to `null`.

The `demo` backend ignores both keys (its solver is already deterministic).

### A2. Sending the values ([backends.py](../src/ef_review/backends.py), `call_model`)

- Add `temperature` and `seed` to `kwargs` when they are not `null`. The call is shared,
  so this covers the reviewer and the extractor.
- Before the call, check `litellm.get_supported_openai_params(model=..., custom_llm_provider=provider)`.
  If a non-null key is not supported for that model, raise `ExperimentError` naming the
  key and telling the user to set it to `null`. Never let LiteLLM drop it silently
  (NVIDIA NIM accepts `seed` only for some models).
- Add to the usage metadata written to `*.raw.usage.json`: `temperature`, `seed` (the
  values requested, `null` when not sent) and `finish_reason`
  (`response.choices[0].finish_reason`), which tells a cut-off answer from a short one.
- `call_claude_cli`: add `"temperature": None, "seed": None` to its metadata so the
  field set is the same for both runners.

### A3. `doctor` ([cli.py](../src/ef_review/cli.py))

`local_model_check` and the hosted branch of `review_credentials` report the two values.
`doctor` should show `"sampling_pinned": false` when either is `null`.

### A4. Record what actually ran (recommended, can be a follow-up)

Pinned sampling is only reproducible for the same model build. The manifest records
the LiteLLM version but nothing about Ollama.

- In `build_manifest`, for local providers, add to `runtime`:
  `"ollama": {"version": <GET /api/version>, "model_digest": <digest from GET /api/tags>}`.
  Neither request generates text. If the server is unreachable, fail `prepare` with a
  clear message rather than recording `null`.
- In `preflight_live`, compare the current digest with the manifest's and refuse to run
  on a mismatch, the same way `manifest_check` treats a changed benchmark checkout.

Values on 2026-11-12: Ollama `0.40.16`, `qwen2.5-coder:14b` digest `9ec8897f747e`.

## Part B — a malformed answer is a failed attempt, not a stopped run

### Behaviour today

`parse_object` and `edits_validate` raise `ExperimentError` when the answer is not JSON,
is not an object, or has no valid `edits` list. `cached` turns that into
`review.failed.json` and the run stops. The same happens in the package stage through
`package_validate`.

### Target behaviour

| Situation | Today | Planned |
|---|---|---|
| Reviewer answer malformed (`{}`, not JSON, cut off at `max_output_tokens`, edit without `file`) | run stops | case scored unresolved, reason `invalid_model_output`, harness not started, not charged |
| Extractor answer malformed or missing required fields | run stops | no package for that case; the case adds nothing to memory |
| Prompt does not fit `review_num_ctx` | run stops | unchanged: still stops |
| Timeout, connection error, provider error | run stops | unchanged: still stops |
| Harness infrastructure failure or no verdict | run stops | unchanged: still stops |

Only answers the model actually returned are treated as attempts. Anything that means
the call or the grading did not happen properly stays fatal.

### B1. Error type ([core.py](../src/ef_review/core.py))

```python
class ModelOutputError(ExperimentError):
    """The model answered, but not in the required shape. `code` is a short stable label."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
```

Raise it, instead of the plain `ExperimentError`, from:

- `parse_object` — codes `invalid_json`, `not_an_object`.
- `edits_validate` — codes `missing_edits`, `invalid_edit`.
- `package_validate` — code `invalid_package`.

It subclasses `ExperimentError`, so every caller that does not catch it behaves exactly
as now. That matters for `package_validate`, which also checks packages edited by a
person during curation: there the error must stay fatal.

### B2. Reviewer ([backends.py](../src/ef_review/backends.py), [swebench.py](../src/ef_review/swebench.py))

- Change `call_model` and `call_claude_cli` to return `{"text": ..., "usage": ...}`
  without parsing. Today a parse failure loses the usage metadata of the call; the raw
  text is already saved before parsing and stays so.
- `review` parses and validates; on `ModelOutputError` it returns
  `{"findings": [], "patch": "", "edit_errors": [], "output_error": exc.code, "usage": ...}`.
- `swebench.evaluate` and the demo branch of `backends.evaluate`: when
  `reviewed.get("output_error")` is set, return the verdict `passed=False`,
  `reason="invalid_model_output"`, `repair_invoked=False`. Check this before the existing
  `edits_not_applicable` / `no_patch_proposed` test. `evaluation_invoked` already
  returns false for an empty patch, so no budget is charged.
- `extraction_payload`: add `"output_error"` to `attempt`, so the extractor is told the
  attempt produced no usable edits.

### B3. Extractor ([engine.py](../src/ef_review/engine.py), `perform`)

- Inside `propose`, catch `ModelOutputError` and return
  `{"package": None, "output_error": exc.code, "usage": ...}`, so the stage is cached as
  completed and is not re-attempted.
- After `cached`, when the package is `None`, `perform` returns `package=None`.
- The two callers that build `candidates` (`seed` and the `adapt` branch of
  `run_condition`) skip `None`. The existing check that at least one warmup package is
  approved still protects the seed.

### B4. Visibility

A failed attempt must be recorded, never silent.

- `case.json` (written in `run_condition`): add `"reason": scored["reason"]`.
- [reporting.py](../src/ef_review/reporting.py): add to each checkpoint row and to
  `summary.json` the number of cases with reason `invalid_model_output`, and add one
  line with those counts to `report.md`. If memory conditions produce more malformed
  answers than `none` (longer prompts), that is a result to report, not noise to hide.
- [tools/run_stats.py](../tools/run_stats.py): the reason already flows into its
  `reasons` counter. Add `output_error` and the count of cases without a package to the
  per-event rows.

## Tests

`tests/test_experiment.py`:

- `config_load` defaults: `review_temperature == 0`, `review_seed == 0`.
- Rejected values: `"0"`, `True`, `-1`, `2.5` for temperature; `1.5`, `-1`, `True` for seed.
- `claude_cli` with a non-null value is refused; with both `null` it loads.
- `call_model` with `litellm.completion` patched: `temperature` and `seed` are in the
  call when set and absent when `null`; an unsupported key raises `ExperimentError`.
- A patched `call_model` returning `{}`: `review` returns `output_error`, an empty
  patch, and usage; nothing raises.
- End-to-end on the demo run with the extractor patched to return a malformed package:
  the run completes, `candidates.json` omits that case, `package.json` records
  `output_error`.
- `package_validate` on a bad human-edited curation package still raises.

`tests/test_swebench.py`:

- `evaluate` with `output_error` set returns `(False, "invalid_model_output")` and does
  not start the harness (extend `test_no_patch_is_negative_without_running_the_harness`).
- `edits_validate` raises `ModelOutputError` with the expected code for each bad shape.

## Documentation changes

The project documentation is in Portuguese; the wording below is what to add.

**[README.md](../README.md), section 2, configuration key list** — add after `review_reasoning`:

> - `review_temperature` e `review_seed`: amostragem das chamadas do modelo/extrator.
>   Padrão `0` e `0`: a mesma entrada produz a mesma saída no mesmo modelo, versão e
>   máquina. `null` usa o padrão do provedor e o run deixa de ser reproduzível. Um
>   provedor que não aceita o parâmetro é recusado; com `claude_cli` ambos precisam ser
>   `null`. Cada `*.usage.json` registra os valores enviados e o `finish_reason`.

**[README.md](../README.md), section 5 "Falhas, retomada e orçamento"** — the sentence
"Não há repetição automática de chamada falha nem descarte silencioso de um caso" stays
true but needs the new rule next to it:

> Uma resposta malformada do modelo (JSON inválido, sem a lista `edits`, cortada no
> limite de saída) não é uma falha do run: o caso é registrado como não resolvido, com
> `reason = invalid_model_output`, sem executar o harness. Com amostragem fixa, repetir
> a chamada daria a mesma resposta. Uma resposta malformada do extrator deixa o caso sem
> pacote. As contagens aparecem em `report.md` e `summary.json`. Erros de transporte,
> timeout, prompt maior que `review_num_ctx` e falhas do harness continuam interrompendo
> o run.

**[docs/PROTOCOL.md](PROTOCOL.md)**:

- "Escolhas implementadas": add a paragraph stating that calls use fixed temperature
  and seed, that this is reproducibility of the sampling settings and not a certified
  deterministic model, and that the Ollama version and model digest are in the manifest.
- "População", list of consequences: replace "O modelo não é determinístico: uma tarefa
  pode falhar mesmo com experiência pertinente" with a statement that each case is one
  deterministic draw, so the run measures no sampling variance, and that a malformed
  answer counts as unresolved and is reported separately.
- Same section, unrelated but now wrong: it says all 300 tasks are in the catalog with
  no exclusion. Since 2026-11-12, 18 tasks are excluded because their prompt does not
  fit the 32,768-token window of the local model (`tools/context_fit.py`,
  `manifests/swebench_catalog.excluded.json`); 282 remain. Fix this in the same edit.

**Configurations** — add both keys explicitly to `configs/swebench.example.json`,
`configs/swebench.local.json` and `configs/swebench.full.json`, so the choice is
visible in each file and frozen in each manifest.

## Verification after implementing

1. `python -m unittest discover -s tests` passes.
2. `python experiment.py --config configs/swebench.local.json doctor` shows the two
   values and `sampling_pinned: true`.
3. Reproducibility on real prompts: prepare two runs from `manifests/swebench_smoke.csv`
   with the same configuration, run the warmup of both, and compare the SHA-256 of each
   `review.raw.txt` and `package.raw.txt` pair. They should match. If a long prompt
   differs between the two runs, record that in PROTOCOL.md as a limit of the claim
   rather than describing the run as deterministic.
4. Malformed answer: confirm with a patched call (as in the tests) that a `{}` answer
   yields `reason = invalid_model_output` in `evaluation.json` and that the run
   continues to the next case.

## Rollout

- `swebench-01` and `swebench-02` stay as they are: evidence of the unpinned behaviour.
  They cannot be resumed once `src/ef_review/` changes.
- Prepare a new run from the updated `manifests/swebench_catalog.csv` (282 cases).
- Suggested order of work: Part B, then Part A, then tests, then documentation and
  configurations, then the verification steps. Part B alone is also safe to ship first;
  Part A alone is not.

## Out of scope

- Repeating a run over several seeds and reporting variance.
- Re-asking the model after a malformed answer (pointless at temperature 0 with a
  fixed seed; it would need a different seed and a protocol decision).
- Constraining the answer with a JSON schema (`format` with a schema in Ollama), which
  could prevent `{}` altogether. Worth a separate look: it changes what the model is
  allowed to emit and so is a protocol change of its own.
