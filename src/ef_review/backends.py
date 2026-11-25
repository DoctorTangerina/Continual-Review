"""Model and evaluation adapters; imported dependencies do not make live calls."""
from __future__ import annotations

import contextlib
import functools
import json
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import swebench
from .core import (LOCAL_PROVIDERS, THINKING_SWITCH_PROVIDERS, ExperimentError, ModelOutputError,
                   canonical, local_model, write_json)

# Synthetic demo only: each fixture file holds this line once; "fixing" it is the demo's patch.
DEMO_SEARCH, DEMO_REPLACE = "FIXED = False\n", "FIXED = True\n"


def reasoning_kwargs(cfg: dict, provider: str, json_mode: bool = True) -> dict:
    """Request arguments that set the reviewer's thinking mode explicitly.

    Some models (e.g. Nemotron 3 on NVIDIA NIM) think by default, so "off" must be
    sent, not assumed. For providers without a known switch, refuse to run rather
    than silently use the provider's default.
    """
    enabled = bool(cfg.get("review_reasoning", False))
    if provider in THINKING_SWITCH_PROVIDERS:
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": enabled}}}
    if provider in LOCAL_PROVIDERS:
        if enabled:
            raise ExperimentError("Local Ollama models are used without thinking; set review_reasoning to false")
        if not cfg.get("review_num_ctx"):
            raise ExperimentError("Set review_num_ctx for Ollama models (Ollama truncates long prompts otherwise)")
        # num_ctx sizes the window; JSON mode constrains decoding to a JSON object. An agent
        # step is plain text with a fenced command, so it does not ask for JSON mode.
        return {"num_ctx": cfg["review_num_ctx"]} | ({"response_format": {"type": "json_object"}} if json_mode else {})
    raise ExperimentError(f"No known reasoning switch for provider {provider!r}; "
                          "add one in backends.reasoning_kwargs before using it")


def sampling_kwargs(cfg: dict) -> dict:
    """The temperature and seed to send; a null key is left to the provider's default.

    A key the provider does not accept for this model is refused: LiteLLM could
    otherwise drop it and the run would be recorded as pinned when it was not.
    """
    wanted = {key: cfg.get(f"review_{key}") for key in ("temperature", "seed")}
    wanted = {key: value for key, value in wanted.items() if value is not None}
    if wanted:
        import litellm
        # The lookup needs the name without its provider prefix: with the prefix LiteLLM
        # falls back to the provider's general list and reports every key as accepted.
        name, provider = litellm.get_llm_provider(cfg["review_model"])[:2]
        supported = litellm.get_supported_openai_params(model=name, custom_llm_provider=provider) or []
        for key in wanted:
            if key not in supported:
                raise ExperimentError(f"{cfg['review_model']} does not accept {key}; set review_{key} to null "
                                      "(calls then use the provider's default for it)")
    return wanted


def ollama_identity(cfg: dict) -> dict:
    """Ollama's version and the digest of the pulled review model; neither request generates text."""
    base = (cfg.get("api_base") or "http://localhost:11434").rstrip("/")
    name = cfg["review_model"].split("/", 1)[1]
    try:
        with urllib.request.urlopen(base + "/api/version", timeout=10) as response:
            version = json.load(response)["version"]
        with urllib.request.urlopen(base + "/api/tags", timeout=10) as response:
            digests = {m["name"]: m["digest"] for m in json.load(response).get("models", [])}
    except Exception as exc:  # server not running, wrong api_base, unexpected reply
        raise ExperimentError(f"Ollama server not reachable at {base} ({type(exc).__name__}); "
                              "its version and the model digest are part of the run record") from exc
    found = digests.get(name) or digests.get(f"{name}:latest")
    if not found:
        raise ExperimentError(f"Model {name} is not pulled in the Ollama server at {base}")
    return {"version": version, "model_digest": found}


def ollama_unload(cfg: dict, wait_seconds: float = 60) -> None:
    """Unload the review model and wait until the server no longer lists it as loaded.

    The next call then loads it again with no prompt state left over. Ollama offers no
    switch for that reuse, and with it the answer to a pinned prompt depends on the
    requests that came before. Raises if the model is still loaded: a call must not
    run in an unknown state.
    """
    base = (cfg.get("api_base") or "http://localhost:11434").rstrip("/")
    name = cfg["review_model"].split("/", 1)[1]
    try:
        request = urllib.request.Request(base + "/api/generate", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"model": name, "keep_alive": 0}).encode("utf-8"))
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
        deadline = time.monotonic() + wait_seconds
        while True:
            with urllib.request.urlopen(base + "/api/ps", timeout=10) as response:
                loaded = {m["name"] for m in json.load(response).get("models", [])}
            if not loaded & {name, f"{name}:latest"}:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    except Exception as exc:  # server not running, wrong api_base, unexpected reply
        raise ExperimentError(f"Could not unload the model in Ollama at {base} ({type(exc).__name__})") from exc
    raise ExperimentError(f"Ollama still has {name} loaded {wait_seconds:.0f} s after the unload request")


class SharedLoad:
    """One fresh load of the local model, shared by the calls that `members` threads make together.

    An Ollama server started with OLLAMA_NUM_PARALLEL=N computes N calls at the same time,
    each in its own slot. A slot keeps the prompt state of the call it served, and a later
    call can be given that slot, so the rule "every call starts from a freshly loaded model"
    holds only if the model is unloaded once and the calls then start together, while every
    slot is still empty. An unload requested while another call is running does not do that:
    the server waits for the running call first.

    Each thread runs its work inside `in_group`. Its first call that asks for a fresh model
    (`fresh_model`) waits until every other member has also asked or has finished without
    asking (stage already saved, or failed before its call); the model is then unloaded
    once and the waiting calls all proceed.

    This keeps every slot empty at the start, but it does not make the answers those of a
    run without workers: measured on Ollama 0.40.16, the answer to a pinned prompt can
    change with what the server computes at the same time (see PROTOCOL.md).
    """

    def __init__(self, members: int):
        self.absent = members    # members that have neither asked for the model nor finished
        self.unloaded = None     # True once unloaded; the exception if the unload failed
        self.changed = threading.Condition()

    def arrived(self) -> None:
        with self.changed:
            self.absent -= 1
            self.changed.notify_all()

    def start(self, unload) -> None:
        if _thread.asked:
            # The others are already computing; a second unload would stop their calls.
            raise ExperimentError("A second fresh model load was requested within one group of parallel calls")
        _thread.asked = True
        self.arrived()
        with self.changed:
            while self.absent:
                self.changed.wait()
            first = self.unloaded is None
            if first:
                # The lock is held meanwhile: the other members wait for this unload.
                try:
                    unload()
                    self.unloaded = True
                except Exception as exc:
                    self.unloaded = exc
                    raise
        if self.unloaded is not True:
            raise ExperimentError("The model shared by this group of calls could not be unloaded") from self.unloaded


# Set by `in_group` for the thread's calls: .group, how many cases run at the same time;
# .load, their SharedLoad if they have one; .asked, whether this thread has asked for it.
_thread = threading.local()


@contextlib.contextmanager
def in_group(size: int, load: SharedLoad | None = None):
    """The calls made by the current thread inside this block belong to `size` cases that run together."""
    _thread.group, _thread.load, _thread.asked = size, load, False
    try:
        yield
    finally:
        asked = _thread.asked
        _thread.group, _thread.load = 1, None
        if load is not None and not asked:
            load.arrived()


def fresh_model(cfg: dict) -> None:
    """Before a call that must start from a freshly loaded model: unload it, alone or with the thread's group."""
    load = getattr(_thread, "load", None)
    if load is None:
        ollama_unload(cfg)
    else:
        load.start(lambda: ollama_unload(cfg))


def ollama_timed_call(cfg: dict) -> tuple[float, float]:
    """One short call that belongs to no case: when it ended, and the seconds the server computed it."""
    base = (cfg.get("api_base") or "http://localhost:11434").rstrip("/")
    body = {"model": cfg["review_model"].split("/", 1)[1], "stream": False,
            "prompt": "Count from one to thirty in words.",
            # The run's window, so the server loads the model with the memory the run will need.
            "options": {"num_ctx": cfg["review_num_ctx"], "temperature": 0, "seed": 0, "num_predict": 24}}
    request = urllib.request.Request(base + "/api/generate", headers={"Content-Type": "application/json"},
                                     data=json.dumps(body).encode("utf-8"))
    with urllib.request.urlopen(request, timeout=cfg["review_timeout_seconds"]) as response:
        answer = json.load(response)
    return time.monotonic(), (answer["prompt_eval_duration"] + answer["eval_duration"]) / 1e9


def ollama_parallel_check(cfg: dict, workers: int) -> None:
    """Refuse a server that does not compute `workers` calls at the same time.

    Ollama's API does not report OLLAMA_NUM_PARALLEL. A server with fewer slots accepts
    the calls anyway and runs the extra ones afterwards, in a slot another call has used.
    So the server is measured: `workers` short calls are sent together to a freshly loaded
    model. Each answer says how long the server computed it, which gives the period in
    which it was computed; with enough slots these periods overlap almost entirely, with
    too few at least one starts when another ends.
    """
    if not cfg.get("review_num_ctx"):
        raise ExperimentError("Set review_num_ctx for Ollama models (Ollama truncates long prompts otherwise)")
    ollama_unload(cfg)
    periods, errors = [], []

    def call():
        try:
            end, computed = ollama_timed_call(cfg)
            periods.append((end - computed, end))
        except Exception as exc:  # server stopped, model failed to load, unexpected reply
            errors.append(exc)
    threads = [threading.Thread(target=call, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise ExperimentError(f"The Ollama server did not answer {workers} calls sent together "
                              f"({type(errors[0]).__name__}); with {workers} slots the model needs memory for "
                              f"{workers} context windows") from errors[0]
    together = min(end for _, end in periods) - max(start for start, _ in periods)
    if together < 0.5 * min(end - start for start, end in periods):
        raise ExperimentError(f"The Ollama server did not compute {workers} calls at the same time; start it "
                              f"with OLLAMA_NUM_PARALLEL={workers} (or more), or use --workers 1")


def find_claude_bin(cfg: dict) -> str | None:
    """The Claude Code executable: explicit config, then PATH, then the newest VS Code extension copy."""
    if cfg.get("claude_bin"):
        return cfg["claude_bin"] if Path(cfg["claude_bin"]).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    exe = "claude.exe" if os.name == "nt" else "claude"
    bundled = sorted(Path.home().glob(f".vscode/extensions/anthropic.claude-code-*/resources/native-binary/{exe}"),
                     key=lambda p: p.stat().st_mtime)
    return str(bundled[-1]) if bundled else None


@functools.lru_cache(maxsize=None)
def claude_version(binary: str) -> str | None:
    try:
        return subprocess.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8",
                              timeout=30, check=False).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def prompts(cfg: dict) -> list[str]:
    """The prompt texts this run uses: the solver's, then the experience extractor's."""
    if cfg.get("solver") == "agent":
        from . import agent
        # Read from the installed mini-SWE-agent, so another version of it changes this list.
        return list(agent.templates(cfg).values()) + [swebench.PACKAGE_SYSTEM]
    return [swebench.SOLVE_SYSTEM, swebench.PACKAGE_SYSTEM]


def evaluation_invoked(cfg: dict, reviewed: dict) -> bool:
    """Whether evaluating this output starts the SWE-bench harness (and so uses the call budget)."""
    if cfg["backend"] == "demo" or not cfg.get("repair_enabled", True):
        return False
    return bool(reviewed.get("patch"))


def preflight_live(cfg: dict, runtime: dict | None = None, workers: int = 1) -> None:
    """Checks before any call of a case. `runtime` is the manifest's record of what `prepare` saw.

    With `workers` above 1 and a local model, the server is measured with short calls that
    belong to no case and use no call of the budget (see `ollama_parallel_check`).
    """
    if not cfg.get("review_model") or cfg["review_model"].startswith("SET_"):
        raise ExperimentError("Configure an explicit review_model before running")
    if cfg.get("review_runner", "litellm") == "claude_cli":
        if not find_claude_bin(cfg):
            raise ExperimentError("Claude Code CLI not found; install it or set claude_bin")
    elif importlib.util.find_spec("litellm") is None:
        raise ExperimentError("LiteLLM is missing in the experiment environment; install the live extra")
    if cfg.get("solver") == "agent":
        from . import agent
        from .core import package_version
        wanted = (runtime or {}).get("mini_swe_agent")
        if runtime is not None and package_version("mini-swe-agent") != wanted:
            raise ExperimentError(f"mini-swe-agent changed after preparation (run prepared with {wanted}); "
                                  "install that version or prepare a new run")
        agent.docker_check()
    recorded = (runtime or {}).get("ollama")
    if recorded and ollama_identity(cfg)["model_digest"] != recorded["model_digest"]:
        # Same rule as a changed benchmark checkout: the frozen run no longer describes what would execute.
        raise ExperimentError("The Ollama model changed after preparation (digest differs); prepare a new run")
    if cfg.get("repair_enabled", True) and not swebench.probe(cfg)["ok"]:
        raise ExperimentError("SWE-bench harness is not usable from swebench_python; run doctor")
    if workers > 1 and local_model(cfg):
        # Last, because it loads the model: the cheaper checks above refuse first.
        ollama_parallel_check(cfg, workers)


def parse_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelOutputError("invalid_json", "Model did not return valid JSON; raw output retained locally") from exc
    if not isinstance(data, dict):
        raise ModelOutputError("not_an_object", "Expected a JSON object")
    return data


def call_claude_cli(cfg: dict, system: str, payload: dict, raw_path: Path) -> dict:
    """One text-in/text-out call through `claude -p`, authenticated by the subscription login.

    Isolation: no tools, no MCP servers, no user/project/local settings, no slash
    commands or skills, our system prompt replaces the default one, and the process
    runs in an empty temporary directory so no CLAUDE.md is discovered. --bare is not
    used because it only accepts ANTHROPIC_API_KEY, never the subscription login.
    max_output_tokens is not applied: the CLI exposes no per-call output cap.
    """
    model = cfg.get("review_model")
    if not model or model.startswith("SET_"):
        raise ExperimentError("Set review_model in local configuration")
    binary = find_claude_bin(cfg)
    if not binary:
        raise ExperimentError("Claude Code CLI not found; install it or set claude_bin")
    command = [binary, "-p", "--output-format", "json", "--model", model,
               "--system-prompt", system, "--tools", "", "--strict-mcp-config",
               "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence"]
    # Credentials must come from the subscription login, never from an API key.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ef-review-cli-") as cwd:
        completed = subprocess.run(command, input=canonical(payload), capture_output=True, text=True,
                                   encoding="utf-8", cwd=cwd, env=env,
                                   timeout=cfg["review_timeout_seconds"], check=False)
    try:
        outer = json.loads(completed.stdout)
    except json.JSONDecodeError:
        outer = None
    if completed.returncode or not isinstance(outer, dict) or outer.get("is_error"):
        raw_path.write_text(completed.stdout or "", encoding="utf-8")
        raise ExperimentError(f"Claude CLI call failed (exit {completed.returncode}); raw output retained locally")
    text = outer.get("result") or ""
    raw_path.write_text(text, encoding="utf-8")
    usage = outer.get("usage") or {}
    metadata = {"model": model, "runner": "claude_cli", "claude_version": claude_version(binary),
                "elapsed_seconds": time.monotonic() - start,
                "prompt_tokens": sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                                 "cache_read_input_tokens")),
                "completion_tokens": usage.get("output_tokens"),
                "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
                "reasoning_requested": True, "max_output_tokens_applied": False,
                # The CLI has no sampling controls; the keys keep both runners' records alike.
                "temperature": None, "seed": None, "finish_reason": outer.get("stop_reason"),
                "model_usage": outer.get("modelUsage"),
                "cost_usd": outer.get("total_cost_usd")}
    write_json(raw_path.with_suffix(".usage.json"), metadata)
    return {"text": text, "usage": metadata}


def chat(cfg: dict, messages: list[dict], raw_path: Path | None, *, json_mode: bool, unload: bool,
         keep_alive: int | None = None) -> dict:
    """One LiteLLM call over a list of messages; returns the answer unparsed with its usage record.

    `unload` asks for the local model to be unloaded first (ignored for hosted providers).
    `keep_alive` (seconds, local models only) tells Ollama how long to keep the model
    loaded after this call. With `raw_path` the answer and the usage record are written
    next to each other; the agent passes None and keeps both in its trajectory.
    """
    try:
        import litellm
    except ImportError as exc:
        raise ExperimentError('Install live dependencies: python -m pip install -e ".[live]"') from exc
    model = cfg.get("review_model")
    if not model or model.startswith("SET_"):
        raise ExperimentError("Set review_model in local configuration")
    kwargs = {"model": model, "messages": messages,
              "timeout": cfg["review_timeout_seconds"], "num_retries": 0}
    base_model = model.removeprefix("openai/")
    token_key = "max_completion_tokens" if base_model.startswith(("gpt-", "o1", "o3", "o4")) else "max_tokens"
    kwargs[token_key] = cfg["max_output_tokens"]
    if cfg.get("api_base"):
        kwargs["api_base"] = cfg["api_base"]
    provider = litellm.get_llm_provider(model)[1]
    kwargs |= reasoning_kwargs(cfg, provider, json_mode)
    sampling = sampling_kwargs(cfg)
    kwargs |= sampling
    local = provider in LOCAL_PROVIDERS
    if local and keep_alive is not None:
        kwargs["keep_alive"] = keep_alive
    unload = local and unload
    if unload:
        fresh_model(cfg)
    start = time.monotonic()  # includes the model load that the first request now triggers
    response = litellm.completion(**kwargs)
    message = response.choices[0].message
    text = message.content or ""
    usage = response.usage
    reasoning_text = getattr(message, "reasoning_content", None) or ""
    metadata = {"model": response.model, "elapsed_seconds": time.monotonic() - start,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "reasoning_requested": bool(cfg.get("review_reasoning", False)),
                "reasoning_chars": len(reasoning_text),
                # What was requested (null = not sent). "length" marks an answer cut off
                # at max_output_tokens, which a short answer is not.
                "temperature": sampling.get("temperature"), "seed": sampling.get("seed"),
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
                "cost_usd": None}
    if local:
        metadata["num_ctx"] = cfg["review_num_ctx"]
        metadata["model_unloaded_before_call"] = unload
        # Cases in progress at the same time (`run --workers`); above 1 the server may have
        # computed other calls together with this one, which can change the answer.
        metadata["parallel_cases"] = getattr(_thread, "group", 1)
    if raw_path is not None:
        raw_path.write_text(text, encoding="utf-8")
        write_json(raw_path.with_suffix(".usage.json"), metadata)
    # Ollama drops the start of a prompt that does not fit, losing the instructions.
    # A prompt that fills the window with the output budget was therefore truncated.
    if local and (metadata["prompt_tokens"] or 0) + cfg["max_output_tokens"] > cfg["review_num_ctx"]:
        raise ExperimentError(f"Prompt does not fit review_num_ctx ({metadata['prompt_tokens']} prompt tokens); "
                              "the case is too large for this model")
    return {"text": text, "usage": metadata}


def call_model(cfg: dict, system: str, payload: dict, raw_path: Path) -> dict:
    """One call; returns the answer unparsed, so a malformed answer keeps its usage record."""
    if cfg.get("review_runner", "litellm") == "claude_cli":
        return call_claude_cli(cfg, system, payload, raw_path)
    return chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": canonical(payload)}],
                raw_path, json_mode=True, unload=bool(cfg.get("review_unload_model")))


def review(cfg: dict, task: dict, memory: list[dict], raw_path: Path, sandbox: dict | None = None) -> dict:
    """One attempt at a task. `sandbox` (agent only) names the task's container image."""
    if cfg.get("solver") == "agent" and cfg["backend"] != "demo":
        from . import agent
        if not sandbox:
            raise ExperimentError("The agent solver needs the task's container image")
        return agent.run_attempt(cfg, task, memory, sandbox, raw_path)
    if cfg["backend"] == "demo":
        # Synthetic plumbing demonstration, deliberately not an estimate of model quality:
        # it edits the fixture only when a retrieved experience names the task's marker.
        match = any(tag in task["problem_statement"] for p in memory for tag in p["tags"])
        data = {"edits": [{"file": task["files"][0]["path"], "search": DEMO_SEARCH,
                           "replace": DEMO_REPLACE}] if match else []}
        raw_path.write_text(canonical(data), encoding="utf-8")
        return swebench.solve(data, task) | {"usage": {"model": "synthetic-demo", "cost_usd": 0}}
    result = call_model(cfg, swebench.SOLVE_SYSTEM, {"task": task, "experiences": memory}, raw_path)
    try:
        solved = swebench.solve(parse_object(result["text"]), task)
    except ModelOutputError as exc:
        # The model answered, so this is a failed attempt of the case, not a failed call;
        # asking again until the shape is right would be choosing among samples.
        solved = {"findings": [], "patch": "", "edit_errors": [], "output_error": exc.code}
    return solved | {"usage": result["usage"]}


def extract(cfg: dict, case: dict, reviewed: dict, scored: dict, raw_path: Path) -> dict:
    if case["role"] == "eval":
        raise ExperimentError("Evaluation data cannot be sent to the experience extractor")
    payload = swebench.extraction_payload(case, reviewed, scored)
    if cfg["backend"] == "demo":
        marker = re.search(r"DEMO_[A-Z]+", payload["task"]["problem_statement"])
        if not marker:
            raise ExperimentError(f"Synthetic demo task without a DEMO_ marker: {case['instance_id']}")
        result = {"episode": "Synthetic adaptation episode, not a real issue-resolution result.",
                  "rule": f"Switch on {marker.group(0)} when resolving the synthetic demo.",
                  "scope": "Only the synthetic demo fixture.", "exceptions": "Not applicable to real programs.",
                  "goal": "Exercise memory plumbing", "question": "Was the demo marker retrieved?",
                  "metric": "Synthetic resolved count", "tags": [marker.group(0)]}
        raw_path.write_text(canonical(result), encoding="utf-8")
        return {"package": result, "usage": {"model": "synthetic-demo", "cost_usd": 0}}
    result = call_model(cfg, swebench.PACKAGE_SYSTEM, payload, raw_path)
    try:
        return {"package": parse_object(result["text"]), "usage": result["usage"]}
    except ModelOutputError as exc:
        return {"package": None, "output_error": exc.code, "usage": result["usage"]}


def evaluate(cfg: dict, case: dict, reviewed: dict, event_dir: Path) -> dict:
    if not cfg.get("repair_enabled", True):
        # Nothing is executed, so no target is marked passed or failed.
        return {"status": "not_run", "targets": [{"target_id": t["target_id"], "passed": None} for t in case["targets"]],
                "reason": "repair_disabled", "repair_invoked": False}
    if cfg["backend"] == "demo":
        # Synthetic verdict without the harness: any applicable patch counts as resolved.
        passed = bool(reviewed.get("patch"))
        reason = ("synthetic_resolved" if passed else
                  "invalid_model_output" if reviewed.get("output_error") else
                  "edits_not_applicable" if reviewed.get("edit_errors") else "no_patch_proposed")
        return {"status": "ok", "targets": [{"target_id": t["target_id"], "passed": passed} for t in case["targets"]],
                "reason": reason, "repair_invoked": False, "synthetic": True}
    return swebench.evaluate(cfg, case, reviewed, event_dir)
