"""Standard-library contracts: immutable inputs, source separation and memory."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import importlib.metadata
from pathlib import Path
import re
from typing import Any

CONDITIONS = ("none", "static", "automatic", "curated")
CONFIG_DEFAULTS = {
    "max_calls": 200, "max_output_tokens": 2000,
    "memory_max_chars": 6000, "memory_top_k": 3,
    "review_timeout_seconds": 180,
    "source_split_policy": "project",
    # Reviewer/extractor "thinking" mode. Off unless explicitly enabled; see backends.reasoning_kwargs.
    "review_reasoning": False,
    # Sampling of reviewer/extractor calls. null = the provider's default, which varies
    # from call to call. Pinning alone does not make a run deterministic: see PROTOCOL.md.
    "review_temperature": 0,
    "review_seed": 0,
    # How the reviewer/extractor is called: "litellm" (provider API) or "claude_cli"
    # (Claude Code CLI with the subscription login).
    "review_runner": "litellm",
    # claude_cli only: path to the claude executable; null = PATH, then the VS Code extension.
    "claude_bin": None,
    # Local Ollama models only: context window sent as num_ctx. Ollama otherwise uses a
    # small default and silently drops the start of long prompts.
    "review_num_ctx": None,
    # Local Ollama models only: unload the model before every call, so each prompt is
    # computed from a freshly loaded model. Ollama otherwise reuses the previous request's
    # prompt state, and the answer to the same pinned prompt then depends on what the
    # server processed before (measured: see PROTOCOL.md). Costs one model load per call.
    "review_unload_model": True,
    # false = the SWE-bench harness is not run; targets stay unevaluated (passed = null)
    # and results come from manual annotation only.
    "repair_enabled": True,
    # The model resolves SWE-bench issues and the SWE-bench harness grades the patch
    # (see swebench.py). It is the only benchmark; the key is kept so configurations
    # and manifests state it explicitly.
    "benchmark": "swebench",
    "swebench_dataset": "SWE-bench/SWE-bench_Lite", "swebench_split": "test",
    "swebench_eval_timeout_seconds": 1800,
    # How a task is resolved. "agent": the model works in a loop inside the task's Docker
    # container, one shell command per reply (mini-SWE-agent runs the loop; see agent.py).
    # "oneshot": one call that returns search/replace edits for the oracle files.
    "solver": "agent",
    # Agent only. Most model replies per attempt; limit for one command; most characters
    # of one command's output shown to the model (first and last halves are kept).
    "agent_step_limit": 30,
    "agent_command_timeout_seconds": 120,
    "agent_observation_max_chars": 4000,
    # Agent only: also give the paths (not the text) of the files the reference fix edits.
    "agent_oracle_paths": False,
    # Agent only: end the attempt (RepeatedCommands) when the last commands are one block
    # of 1 to agent.REPEAT_MAX_PERIOD commands said this many times in a row. 0 turns it off.
    "agent_repeat_limit": 3,
}
REVIEW_RUNNERS = ("litellm", "claude_cli")
SOLVERS = ("oneshot", "agent")

# Providers whose chat templates take an enable_thinking switch (verified on
# nvidia/nemotron-3-ultra-550b-a55b: thinking is on by default there).
THINKING_SWITCH_PROVIDERS = ("nvidia_nim", "hosted_vllm")
# Local providers used only with non-thinking models (e.g. qwen2.5-coder); reasoning must be off.
LOCAL_PROVIDERS = ("ollama", "ollama_chat")


class ExperimentError(Exception):
    pass


class ModelOutputError(ExperimentError):
    """The model answered, but not in the required shape. `code` is a short stable label."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def local_model(cfg: dict) -> bool:
    """Whether the reviewer is a local Ollama model; LiteLLM routes these by the model prefix."""
    return (cfg.get("review_runner", "litellm") != "claude_cli"
            and str(cfg.get("review_model") or "").split("/", 1)[0] in LOCAL_PROVIDERS)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def append_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")


def resolve(base: Path, value: str) -> Path:
    return (base / Path(value).expanduser()).resolve()


def load_dotenv(path: Path) -> list[str]:
    """Export KEY=value lines from a local .env file into os.environ.

    Variables already set in the environment win. Values are never logged or
    written to run files; only the variable names are returned.
    """
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def config_load(path: Path) -> dict:
    path = path.resolve()
    cfg = CONFIG_DEFAULTS | read_json(path)
    if cfg.get("backend") not in ("demo", "litellm"):
        raise ExperimentError("backend must be demo or litellm")
    for key in ("max_calls", "max_output_tokens", "memory_max_chars", "memory_top_k",
                "review_timeout_seconds"):
        if type(cfg.get(key)) is not int or cfg[key] < 1:
            raise ExperimentError(f"{key} must be a positive integer")
    if type(cfg["review_reasoning"]) is not bool:
        raise ExperimentError("review_reasoning must be true or false")
    if type(cfg["review_unload_model"]) is not bool:
        raise ExperimentError("review_unload_model must be true or false")
    if type(cfg["repair_enabled"]) is not bool:
        raise ExperimentError("repair_enabled must be true or false")
    if cfg["review_runner"] not in REVIEW_RUNNERS:
        raise ExperimentError("review_runner must be litellm or claude_cli")
    if cfg["review_runner"] == "claude_cli" and not cfg["review_reasoning"]:
        # Verified with Claude Code 2.1.322 + claude-haiku-5-5: neither MAX_THINKING_TOKENS=0,
        # alwaysThinkingEnabled=false nor --effort low stops thinking, so "off" cannot be honored.
        raise ExperimentError("claude_cli cannot disable thinking; set review_reasoning to true "
                              "(thinking tokens are recorded in each *.usage.json)")
    temperature, seed = cfg["review_temperature"], cfg["review_seed"]
    if temperature is not None and (type(temperature) not in (int, float) or not 0 <= temperature <= 2):
        raise ExperimentError("review_temperature must be null or a number from 0 to 2")
    if seed is not None and (type(seed) is not int or seed < 0):
        raise ExperimentError("review_seed must be null or a non-negative integer")
    if cfg["review_runner"] == "claude_cli" and (temperature is not None or seed is not None):
        # The CLI has no sampling controls; refuse rather than record values that were not applied.
        raise ExperimentError("claude_cli cannot set temperature or seed; "
                              "set review_temperature and review_seed to null")
    if cfg["review_num_ctx"] is not None and (type(cfg["review_num_ctx"]) is not int
                                              or cfg["review_num_ctx"] <= cfg["max_output_tokens"]):
        raise ExperimentError("review_num_ctx must be null or an integer larger than max_output_tokens")
    if cfg.get("claude_bin"):
        cfg["claude_bin"] = str(resolve(path.parent, cfg["claude_bin"]))
    if cfg["source_split_policy"] not in ("project", "pr"):
        raise ExperimentError("source_split_policy must be project or pr")
    if cfg["benchmark"] != "swebench":
        # c-CRAB (diff review graded by a corrector) was removed from the project.
        raise ExperimentError("benchmark must be swebench, the only supported benchmark")
    if cfg["solver"] not in SOLVERS:
        raise ExperimentError("solver must be oneshot or agent")
    for key in ("agent_step_limit", "agent_command_timeout_seconds"):
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ExperimentError(f"{key} must be a positive integer")
    if type(cfg["agent_observation_max_chars"]) is not int or cfg["agent_observation_max_chars"] < 200:
        raise ExperimentError("agent_observation_max_chars must be an integer of at least 200")
    if type(cfg["agent_repeat_limit"]) is not int or cfg["agent_repeat_limit"] == 1 or cfg["agent_repeat_limit"] < 0:
        raise ExperimentError("agent_repeat_limit must be 0 (off) or an integer of at least 2")
    if type(cfg["agent_oracle_paths"]) is not bool:
        raise ExperimentError("agent_oracle_paths must be true or false")
    if cfg["solver"] == "agent":
        # "agent" is the default, so a configuration that cannot run it must name "oneshot".
        if cfg["backend"] == "demo":
            raise ExperimentError('The synthetic demo has no agent; set "solver": "oneshot"')
        if cfg["review_runner"] == "claude_cli":
            raise ExperimentError('claude_cli is a single call with tools disabled; set "solver": "oneshot" '
                                  "or review_runner to litellm")
        if cfg["review_num_ctx"] is None:
            raise ExperimentError("solver agent needs review_num_ctx (the context window, with any provider): "
                                  "the loop ends an attempt before a prompt would exceed it")
    if cfg["backend"] == "demo":
        # Synthetic SWE-bench-shaped tasks whose oracle files are pre-cached; no harness.
        cfg["dataset_path"] = str(resolve(path.parent, cfg["demo_dataset"]))
        cfg["swebench_cache"] = str(resolve(path.parent, cfg["demo_cache"]))
    else:
        from . import swebench
        swebench.config_resolve(cfg, path.parent)
    return cfg


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def upstream_identity(cfg: dict) -> dict:
    if cfg["backend"] == "demo":
        return {"kind": "synthetic-demo", "commit": None}
    from . import swebench
    return swebench.identity(cfg)


def catalog(cfg: dict) -> tuple[list[dict], list[dict]]:
    from . import swebench
    return swebench.catalog(cfg)


def build_manifest(cfg: dict, assignments: Path) -> dict:
    from . import swebench
    if cfg["backend"] != "demo":
        swebench.ensure_dataset(cfg)
    raw_cases = jsonl(Path(cfg["dataset_path"]))
    dataset = {c["instance_id"]: c for c in raw_cases}
    if len(dataset) != len(raw_cases):
        raise ExperimentError("Duplicate IDs in dataset")
    with assignments.open(encoding="utf-8-sig", newline="") as stream:
        selected = list(csv.DictReader(stream))
    cases, seen, groups, task_roles = [], set(), {}, {}
    for row in selected:
        role = row.get("role", "").strip()
        if not role or role == "exclude":
            continue
        if role not in ("warmup", "adapt", "eval"):
            raise ExperimentError("role must be warmup, adapt, eval, exclude or blank")
        iid = row["instance_id"].strip()
        if not re.fullmatch(r"[A-Za-z0-9_@./-]+", iid) or ".." in iid or iid.startswith("/"):
            raise ExperimentError("Unsafe benchmark instance ID")
        if iid in seen or iid not in dataset:
            raise ExperimentError(f"Duplicate or unknown selected ID: {iid}")
        seen.add(iid)
        raw = dataset[iid]
        if cfg["backend"] == "demo" and not iid.startswith("demo-"):
            raise ExperimentError("Demo backend may only use synthetic demo-* instances")
        family = row.get("family", "").strip()
        if not family:
            raise ExperimentError(f"Set family for {iid}")
        block = 0
        if role == "adapt":
            try:
                block = int(row["block"])
            except (KeyError, ValueError) as exc:
                raise ExperimentError(f"Set positive block for adaptation case {iid}") from exc
            if block < 1:
                raise ExperimentError("Adaptation blocks start at 1")
        group = raw["repo"] if cfg["source_split_policy"] == "project" else f"{raw['repo']}:{raw.get('pull_number', iid)}"
        if group in groups and groups[group] != role:
            raise ExperimentError(f"Source leakage across roles: {group}")
        groups[group] = role
        # The frozen instance is what the harness grades; it never reaches the model.
        pub, targets = swebench.public_case(cfg, raw), swebench.targets(raw)
        task_hash = digest(pub)
        if task_hash in task_roles and task_roles[task_hash] != role:
            raise ExperimentError("Identical tasks across roles")
        task_roles[task_hash] = role
        if len({e["target_id"] for e in targets}) != len(targets):
            raise ExperimentError(f"Duplicate test targets for {iid}")
        cases.append({"instance_id": iid, "role": role, "block": block, "family": family,
                      "public": pub, "targets": targets, "instance": raw, "task_sha256": digest(raw)})
    if not any(c["role"] == "eval" for c in cases) or not any(c["role"] == "adapt" for c in cases):
        raise ExperimentError("Select at least one adaptation case and one reserved evaluation case")
    blocks = sorted({c["block"] for c in cases if c["role"] == "adapt"})
    if blocks != list(range(1, max(blocks) + 1)):
        raise ExperimentError("Use contiguous adaptation blocks 1, 2, ...")
    # Warmup is required for a meaningful static baseline; all memory arms share this initial state.
    if not any(c["role"] == "warmup" for c in cases):
        raise ExperimentError("Select a separate warmup set to seed the static and incremental memory conditions")
    litellm_version, mini_version = package_version("litellm"), None
    if cfg["solver"] == "agent":
        # The loop and its prompt templates come from this package; they are part of the run.
        mini_version = package_version("mini-swe-agent")
        if mini_version is None:
            raise ExperimentError("mini-swe-agent is not installed; solver agent needs it (pip install -e .)")
    claude_cli = None
    if cfg.get("review_runner") == "claude_cli":
        from .backends import claude_version, find_claude_bin
        binary = find_claude_bin(cfg)
        claude_cli = {"path": binary, "version": claude_version(binary) if binary else None}
    ollama = None
    if cfg["backend"] != "demo" and local_model(cfg):
        # Pinned sampling only reproduces on the same model build; fails if the server is down.
        from .backends import ollama_identity
        ollama = ollama_identity(cfg)
    manifest = {"schema_version": 1, "config": cfg, "config_sha256": digest(cfg),
                "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                            "litellm": litellm_version, "mini_swe_agent": mini_version,
                            "claude_cli": claude_cli, "ollama": ollama},
                "dataset_sha256": file_digest(Path(cfg["dataset_path"])),
                "upstream": upstream_identity(cfg), "cases": cases, "blocks": blocks,
                "synthetic": cfg["backend"] == "demo"}
    manifest["manifest_sha256"] = digest(manifest)
    return manifest


def manifest_check(manifest: dict) -> None:
    payload = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if digest(payload) != manifest.get("manifest_sha256"):
        raise ExperimentError("Manifest changed after preparation; prepare a new run")
    cfg = manifest["config"]
    if file_digest(Path(cfg["dataset_path"])) != manifest["dataset_sha256"]:
        raise ExperimentError("External dataset changed after preparation")
    if upstream_identity(cfg) != manifest["upstream"]:
        raise ExperimentError("External benchmark checkout changed")
    # The graded instances are frozen inside the manifest itself, covered by its hash.


def package_validate(p: dict) -> dict:
    # Also checks packages a person edited during curation; only the extractor path
    # (engine.perform) catches this error, everywhere else it stays fatal.
    required = ("episode", "rule", "scope", "exceptions", "goal", "question", "metric")
    if not isinstance(p, dict) or any(not isinstance(p.get(k), str) or not p[k].strip() for k in required):
        raise ModelOutputError("invalid_package", "Package requires non-empty episode, rule, scope, exceptions, "
                               "goal, question and metric")
    if not isinstance(p.get("tags"), list) or any(not isinstance(x, str) for x in p["tags"]):
        raise ModelOutputError("invalid_package", "Package tags must be a list of strings")
    return {k: p[k].strip() for k in required} | {"tags": p["tags"]}


def retrieve(memory: list[dict], task: dict, cfg: dict) -> list[dict]:
    # Whole source files would match every package; only their paths join the query.
    view = task | {"files": [f["path"] for f in task["files"]]} if "files" in task else task
    query = set(re.findall(r"\w+", canonical(view).lower()))
    ranked = []
    for p in memory:
        if p.get("status") != "approved":
            continue
        scope_repo = p.get("scope_repo")
        if scope_repo and scope_repo != task["repo"]:
            continue
        words = set(re.findall(r"\w+", canonical({k: p[k] for k in ("rule", "scope", "tags")}).lower()))
        score = len(query & words)
        if score:
            ranked.append((-score, p["package_id"], p))
    selected = []
    for _, _, p in sorted(ranked):
        # Only material needed at inference; no raw reference tests/feedback.
        view = {k: p[k] for k in ("package_id", "episode", "rule", "scope", "exceptions", "goal", "question", "metric", "tags")}
        candidate = selected + [view]
        if len(canonical(candidate)) <= cfg["memory_max_chars"]:
            selected = candidate
        if len(selected) >= cfg["memory_top_k"]:
            break
    return selected


class RunLock:
    """Single writer across conditions, curation and reporting. Fail on concurrent access."""
    def __init__(self, root: Path):
        self.path = root / ".run.lock"

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ExperimentError("Run locked. If a process crashed, confirm it stopped before removing .run.lock") from exc
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        self.path.unlink()
