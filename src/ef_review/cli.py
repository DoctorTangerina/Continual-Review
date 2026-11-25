from __future__ import annotations

import argparse
import csv
import importlib.util
from pathlib import Path
import shutil
import subprocess

from .core import (CONDITIONS, ExperimentError, RunLock, build_manifest, catalog,
                   config_load, load_dotenv, package_version, read_json, upstream_identity, write_json)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .engine import initialize, load_run, run_condition
from .reporting import export_report


def local_model_check(cfg: dict, provider: str) -> dict:
    """Ollama needs no key: check that the server answers and the model is pulled (no generation)."""
    import json
    import urllib.request
    base = (cfg.get("api_base") or "http://localhost:11434").rstrip("/")
    name = cfg["review_model"].split("/", 1)[1]
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=10) as response:
            pulled = {m["name"] for m in json.load(response).get("models", [])}
    except Exception as exc:  # server not running, wrong api_base
        return {"ok": False, "provider": provider, "error": f"Ollama server not reachable at {base}: {type(exc).__name__}"}
    present = name in pulled or f"{name}:latest" in pulled
    ok = present and not cfg.get("review_reasoning") and bool(cfg.get("review_num_ctx"))
    return {"ok": ok, "provider": provider, "api_base": base, "model_pulled": present,
            "num_ctx": cfg.get("review_num_ctx"), "reasoning": cfg.get("review_reasoning", False),
            "temperature": cfg.get("review_temperature"), "seed": cfg.get("review_seed"),
            "unload_model_before_call": cfg.get("review_unload_model")}


def review_credentials(cfg: dict) -> dict:
    """Check, without a model call, that LiteLLM knows the provider and finds its key,
    or, for the claude_cli runner, that the CLI and its subscription login exist."""
    if cfg.get("review_runner", "litellm") == "claude_cli":
        from .backends import claude_version, find_claude_bin
        binary = find_claude_bin(cfg)
        credentials = Path.home() / ".claude" / ".credentials.json"
        return {"ok": bool(binary) and credentials.is_file(), "runner": "claude_cli", "claude_bin": binary,
                "claude_version": claude_version(binary) if binary else None,
                "login_exists": credentials.is_file(), "reasoning": "always on (CLI cannot disable it)"}
    try:
        import litellm
        from .backends import sampling_kwargs
        provider = litellm.get_llm_provider(cfg["review_model"])[1]
        env = litellm.validate_environment(cfg["review_model"])
        sampling_kwargs(cfg)  # a temperature/seed this model does not accept is reported here, before any call
    except Exception as exc:  # unknown provider prefix, missing package, unsupported sampling key, etc.
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    from .core import LOCAL_PROVIDERS, THINKING_SWITCH_PROVIDERS
    if provider in LOCAL_PROVIDERS:
        return local_model_check(cfg, provider)
    switch = provider in THINKING_SWITCH_PROVIDERS
    return {"ok": bool(env.get("keys_in_environment")) and switch, "provider": provider,
            "missing_keys": env.get("missing_keys", []),
            "reasoning": cfg.get("review_reasoning", False) if switch else "no known switch for this provider",
            "temperature": cfg.get("review_temperature"), "seed": cfg.get("review_seed")}


def docker_ready() -> dict:
    result = {"docker_cli": bool(shutil.which("docker")), "docker_daemon": False}
    if result["docker_cli"]:
        try:
            result["docker_daemon"] = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def doctor(cfg: dict) -> dict:
    if cfg["backend"] == "demo":
        return {"backend": "demo", "dataset_exists": Path(cfg["dataset_path"]).is_file(),
                "context_cache_exists": (Path(cfg["swebench_cache"]) / "context").is_dir(),
                "identity": upstream_identity(cfg)}
    from . import swebench
    # dataset_exported is false until the first catalog/prepare, which creates it.
    return {"backend": cfg["backend"], "benchmark": "swebench", "identity": upstream_identity(cfg),
            "tasks_repo_exists": (Path(cfg["swebench_tasks"]) / "tasks").is_dir(),
            "dataset": f"{cfg['swebench_dataset']} / {cfg['swebench_split']}",
            "dataset_exported": Path(cfg["dataset_path"]).is_file(),
            "review_runner": cfg.get("review_runner", "litellm"),
            "solver": cfg["solver"],
            # Agent only: its limits, and the package that runs the loop. Its containers are
            # started with the docker command of this machine (docker_daemon below).
            "agent": ({"step_limit": cfg["agent_step_limit"],
                       "command_timeout_seconds": cfg["agent_command_timeout_seconds"],
                       "observation_max_chars": cfg["agent_observation_max_chars"],
                       "oracle_paths": cfg["agent_oracle_paths"],
                       "repeat_limit": cfg["agent_repeat_limit"],
                       "mini_swe_agent": package_version("mini-swe-agent")} if cfg["solver"] == "agent" else None),
            "litellm_installed": importlib.util.find_spec("litellm") is not None,
            "models_configured": bool(cfg.get("review_model")) and not cfg["review_model"].startswith("SET_"),
            "review_credentials": review_credentials(cfg),
            # false = a key is null, so calls use the provider's default sampling for it.
            "sampling_pinned": cfg["review_temperature"] is not None and cfg["review_seed"] is not None,
            "evaluation_enabled": cfg["repair_enabled"],
            "swebench_harness": swebench.probe(cfg)} | docker_ready()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sequential EF/SWE-bench experiment. Demo is synthetic; real calls require --live.")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "demo.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local paths/runtime without model calls")
    cat = commands.add_parser("catalog", help="Export eligible instances for manual split assignment")
    cat.add_argument("--output", type=Path, required=True)
    prep = commands.add_parser("prepare", help="Freeze inputs and source provenance; no model calls")
    prep.add_argument("--assignments", type=Path, required=True)
    prep.add_argument("--run-dir", type=Path, required=True)
    run = commands.add_parser("run", help="Run/resume one condition; pauses for curation")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--condition", choices=CONDITIONS, required=True)
    run.add_argument("--live", action="store_true")
    run.add_argument("--retry-failed", action="store_true", help="Explicitly reattempt failed stages, consuming the same shared call budget")
    run.add_argument("--workers", type=int, default=1,
                     help="Cases of a stage in progress at the same time (default 1: one after another). A local "
                          "Ollama server must have been started with OLLAMA_NUM_PARALLEL of at least this number")
    status = commands.add_parser("status", help="Show progress without revealing reserved outcomes")
    status.add_argument("--run-dir", type=Path, required=True)
    report = commands.add_parser("report", help="Reveal reserved outcomes after all conditions complete")
    report.add_argument("--run-dir", type=Path, required=True)
    report.add_argument("--annotations", type=Path)
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")  # e.g. NVIDIA_NIM_API_KEY for the model
    try:
        if args.command in ("doctor", "catalog", "prepare"):
            cfg = config_load(args.config)
            if args.command == "doctor":
                result = doctor(cfg)
            elif args.command == "catalog":
                rows, excluded = catalog(cfg)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                if args.output.exists():
                    raise ExperimentError("Catalog exists; use another output filename")
                with args.output.open("w", encoding="utf-8", newline="") as stream:
                    fields = list(rows[0]) if rows else ["instance_id", "role", "block", "family", "repo", "targets"]
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                write_json(args.output.with_suffix(".excluded.json"), excluded)
                result = {"eligible": len(rows), "excluded": len(excluded), "output": str(args.output)}
            else:
                manifest = build_manifest(cfg, args.assignments)
                initialize(args.run_dir.resolve(), manifest)
                result = {"prepared": str(args.run_dir.resolve()), "cases": len(manifest["cases"]), "synthetic": manifest["synthetic"]}
        else:
            root = args.run_dir.resolve()
            if not root.is_dir():
                raise ExperimentError("Run directory does not exist; run prepare first")
            with RunLock(root):
                if args.command == "run":
                    result = run_condition(root, args.condition, live=args.live, retry=args.retry_failed,
                                           workers=args.workers)
                elif args.command == "report":
                    result = export_report(root, args.annotations)
                else:
                    manifest = load_run(root)
                    states = {}
                    for condition in CONDITIONS:
                        p = root / "conditions" / condition / "state.json"
                        state = read_json(p) if p.exists() else {}
                        states[condition] = {"completed_actions": state.get("cursor", 0), "complete": state.get("complete", False)}
                    result = {"synthetic": manifest["synthetic"], "conditions": states, "budget": read_json(root / "budget.json")}
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ExperimentError, FileNotFoundError, KeyError, ValueError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")
