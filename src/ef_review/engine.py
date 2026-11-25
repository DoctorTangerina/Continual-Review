"""Resumable sequential experiment; human curation is an explicit state boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
import hashlib
from pathlib import Path
import sys
import threading
import time
import math
from typing import Callable, Iterator

from . import backends, swebench
from .core import (CONDITIONS, ExperimentError, ModelOutputError, append_json, digest, file_digest,
                   local_model, manifest_check, package_validate, read_json, retrieve, write_json)

MAX_WORKERS = 16
# budget.json and audit.jsonl are the only files that cases running at the same time all
# write; every other file of a case is inside its own event folder.
SHARED_FILES = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def code_identity() -> dict:
    return {p.name: file_digest(p) for p in Path(__file__).parent.glob("*.py")}


def initialize(root: Path, manifest: dict) -> None:
    if root.exists():
        raise ExperimentError("Run directory already exists; choose a new name")
    root.mkdir(parents=True)
    manifest["code_sha256"] = digest(code_identity())
    manifest["prompts_sha256"] = digest(backends.prompts(manifest["config"]))
    manifest["manifest_sha256"] = digest({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    write_json(root / "manifest.json", manifest)
    write_json(root / "budget.json", {"reserved_calls": 0, "max_calls": manifest["config"]["max_calls"]})


def load_run(root: Path) -> dict:
    m = read_json(root / "manifest.json")
    manifest_check(m)
    if m["code_sha256"] != digest(code_identity()):
        raise ExperimentError("Experiment source changed after prepare; use a new run directory")
    if m["prompts_sha256"] != digest(backends.prompts(m["config"])):
        # The agent's templates come from the installed mini-SWE-agent, not from this source.
        raise ExperimentError("Prompt texts changed after prepare; use a new run directory")
    budget = read_json(root / "budget.json")
    if budget.get("max_calls") != m["config"]["max_calls"] or type(budget.get("reserved_calls")) is not int or not 0 <= budget["reserved_calls"] <= budget["max_calls"]:
        raise ExperimentError("Call-budget record differs from the frozen configuration")
    return m


def audit(root: Path, entry: dict) -> None:
    with SHARED_FILES:
        append_json(root / "audit.jsonl", entry)


def cached(root: Path, event: Path, stage: str, inputs: dict, operation: Callable,
           *, retry_failed: bool, charge: bool = True) -> dict:
    event.mkdir(parents=True, exist_ok=True)
    path, failed = event / f"{stage}.json", event / f"{stage}.failed.json"
    input_hash = digest(inputs)
    if path.exists():
        saved = read_json(path)
        if saved["input_sha256"] != input_hash:
            raise ExperimentError(f"Cached stage inputs changed: {event.name}/{stage}")
        return saved["value"]
    if failed.exists() and not retry_failed:
        raise ExperimentError(f"Stage previously failed: {event.name}/{stage}; inspect audit and use --retry-failed explicitly")
    if failed.exists():
        # Retrying may overwrite model raw text. Preserve failed-attempt evidence before
        # invoking a backend again (swebench.evaluate archives its own harness logs).
        history_root = event / "attempt_history"
        number = len(list(history_root.glob(f"{stage}-*"))) + 1
        history = history_root / f"{stage}-{number}"
        history.mkdir(parents=True)
        for artifact in event.iterdir():
            if artifact.is_file() and artifact.name.startswith(stage + "."):
                (history / artifact.name).write_bytes(artifact.read_bytes())
    if charge:
        with SHARED_FILES:
            budget = read_json(root / "budget.json")
            if budget["reserved_calls"] >= budget["max_calls"]:
                raise ExperimentError("Call limit reached; prepare a separately budgeted run to increase it")
            budget["reserved_calls"] += 1
            write_json(root / "budget.json", budget)
    audit(root, {"at": now(), "event": str(event.relative_to(root)),
                 "stage": stage, "input_sha256": input_hash, "action": "attempt", "charged": charge})
    start = time.monotonic()
    try:
        value = operation()
    except Exception as exc:
        # Logs remain local; raw provider exception text is intentionally omitted.
        error = {"at": now(), "type": type(exc).__name__, "input_sha256": input_hash,
                 "event": str(event.relative_to(root)), "stage": stage, "action": "failed"}
        write_json(failed, error)
        audit(root, error)
        raise ExperimentError(f"Stage failed ({type(exc).__name__}): {event.name}/{stage}; outputs and audit retained") from exc
    write_json(path, {"input_sha256": input_hash, "elapsed_seconds": time.monotonic() - start, "value": value})
    audit(root, {"at": now(), "event": str(event.relative_to(root)), "stage": stage, "action": "completed"})
    return value


def review_stage(root: Path, manifest: dict, condition: str, case: dict, memory: list,
                 event: Path, retry: bool) -> dict:
    cfg = manifest["config"]
    selected = retrieve(memory, case["public"], cfg) if condition != "none" else []
    sandbox = swebench.sandbox_spec(case) if cfg.get("solver") == "agent" else None
    reviewed = cached(root, event, "review", {"task": case["public"], "memory": selected},
                      lambda: backends.review(cfg, case["public"], selected, event / "review.raw.txt", sandbox),
                      retry_failed=retry)
    write_json(event / "retrieval.json", {"package_ids": [p["package_id"] for p in selected],
               "memory_sha256": digest(memory), "retrieved_chars": len(backends.canonical(selected))})
    return reviewed


def evaluation_stage(root: Path, manifest: dict, case: dict, reviewed: dict, event: Path, retry: bool) -> dict:
    cfg = manifest["config"]
    repair = cfg.get("repair_enabled", True)
    needs_repair = backends.evaluation_invoked(cfg, reviewed)
    # The patch is hashed as well: with the agent, findings only names the files it changed.
    scored = cached(root, event, "evaluation", {"findings": reviewed["findings"], "targets": [t["target_id"] for t in case["targets"]],
                                                "patch_sha256": hashlib.sha256(reviewed.get("patch", "").encode("utf-8")).hexdigest()},
                    lambda: backends.evaluate(cfg, case, reviewed, event), retry_failed=retry, charge=needs_repair)
    target_ids = [t["target_id"] for t in case["targets"]]
    returned = scored.get("targets", [])
    if scored.get("status") != ("ok" if repair else "not_run") or sorted(t["target_id"] for t in returned) != sorted(target_ids):
        raise ExperimentError("Evaluation result does not cover the pinned target population")
    return scored


def package_stage(root: Path, manifest: dict, case: dict, reviewed: dict, scored: dict,
                  event: Path, retry: bool) -> dict | None:
    cfg = manifest["config"]
    if case["role"] == "eval":
        raise ExperimentError("Attempt to update memory from reserved evaluation")
    def propose():
        output = backends.extract(cfg, case, reviewed, scored, event / "package.raw.txt")
        if output["package"] is not None:
            try:
                output["package"] = package_validate(output["package"])
            except ModelOutputError as exc:
                output = output | {"package": None, "output_error": exc.code}
        # A malformed answer completes the stage with no package: it is recorded as the
        # outcome of this case's one extraction and is not re-attempted.
        return output
    proposed = cached(root, event, "package", {"review": reviewed, "feedback": scored, "case": case},
                      propose, retry_failed=retry)
    if proposed["package"] is None:
        return None
    clean = package_validate(proposed["package"])
    return clean | {"package_id": "pkg-" + digest([case["instance_id"], clean])[:20],
                    "source_instance_ids": [case["instance_id"]], "status": "candidate"}


def wait(threads: list[threading.Thread]) -> None:
    """Wait for the threads. The first Ctrl+C keeps waiting for them; the second one leaves."""
    interrupted = False
    while True:
        try:
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(0.5)  # with a limit, so that Windows delivers the interrupt
            break
        except KeyboardInterrupt:
            if interrupted:
                raise
            interrupted = True
            running = sum(thread.is_alive() for thread in threads)
            print(f"Interrupted: no new case starts; waiting for the {running} in progress to finish and be "
                  "saved. Ctrl+C again leaves now (their calls stay reserved, without a result).", file=sys.stderr)
    if interrupted:
        raise KeyboardInterrupt


def together(jobs: list[Callable], shared_load: bool = False) -> list:
    """Run the jobs at the same time, one thread each; results come in the order of `jobs`.

    A single job runs in the calling thread, as in a run without workers. Every job runs to
    its end even when another one fails, so each leaves its own stage files and audit lines.
    The error raised afterwards is that of the first failed job in the order of `jobs`.
    With `shared_load`, the jobs' model calls share one fresh load of the local model.
    """
    if len(jobs) == 1:
        return [jobs[0]()]
    load = backends.SharedLoad(len(jobs)) if shared_load else None
    results, errors = [None] * len(jobs), [None] * len(jobs)

    def run(index: int) -> None:
        try:
            with backends.in_group(len(jobs), load):
                results[index] = jobs[index]()
        except Exception as exc:
            errors[index] = exc
    # Daemon threads: after a second Ctrl+C the process must be able to end with calls in progress.
    threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(len(jobs))]
    for thread in threads:
        thread.start()
    wait(threads)
    failed = [error for error in errors if error is not None]
    if len(failed) > 1 and isinstance(failed[0], ExperimentError):
        raise ExperimentError(f"{failed[0]} ({len(failed) - 1} more of the {len(jobs)} cases "
                              "running together also failed)") from failed[0]
    if failed:
        raise failed[0]
    return results


def perform(root: Path, manifest: dict, condition: str, cases: list[dict], memory: list, area: Path,
            retry: bool, extract_package: bool, workers: int = 1) -> Iterator[tuple[dict, Path, dict, dict, dict | None]]:
    """Run the cases of one stage (the warmup, one adaptation block or one checkpoint).

    Yields (case, event folder, attempt, evaluation, package) in the order of `cases`.
    The cases of a stage all read the same memory and never each other's results, so
    running `workers` of them at a time changes when the calls happen, not what is sent.
    The cases advance in groups of `workers`, in the order of `cases`, and a group takes
    each step together (attempt, evaluation, extraction): the model calls of one step
    share one fresh load of a local model (see backends.SharedLoad), which needs all of
    them to start at once. With one worker this is one case after another.
    """
    cfg = manifest["config"]
    shared_load = cfg["backend"] != "demo" and local_model(cfg) and bool(cfg.get("review_unload_model"))
    for first in range(0, len(cases), workers):
        group = cases[first:first + workers]
        events = [area / digest(case["instance_id"])[:16] for case in group]
        reviewed = together([partial(review_stage, root, manifest, condition, case, memory, event, retry)
                             for case, event in zip(group, events)], shared_load)
        scored = together([partial(evaluation_stage, root, manifest, case, attempt, event, retry)
                           for case, attempt, event in zip(group, reviewed, events)])
        packages = [None] * len(group)
        if extract_package:
            packages = together([partial(package_stage, root, manifest, case, attempt, verdict, event, retry)
                                 for case, attempt, verdict, event in zip(group, reviewed, scored, events)], shared_load)
        yield from zip(group, events, reviewed, scored, packages)


def curation_path(root: Path, area: str) -> Path:
    return root / area / "curation.json"


def await_curation(root: Path, area: str, candidates: list[dict]) -> list[dict]:
    path = curation_path(root, area)
    candidate_hash = digest(candidates)
    receipt = path.with_name("curation_receipt.json")
    if receipt.exists():
        saved = read_json(receipt)
        if saved["candidates_sha256"] != candidate_hash:
            raise ExperimentError("Curation receipt does not match candidates")
        # Editing a previously committed decision cannot silently alter later memory.
        if saved["decisions_sha256"] != digest(read_json(path)):
            raise ExperimentError("Committed curation file changed; start a new run")
        return saved["approved"]
    if not path.exists():
        write_json(path, {"instructions": "For each item: approve, correct or reject; fill reviewer, reason and human_seconds. Do not use reserved evaluation outcomes.",
                   "candidates_sha256": candidate_hash, "items": [{"candidate_id": p["package_id"], "decision": "pending",
                   "reviewer": "", "reason": "", "human_seconds": None, "package": p} for p in candidates]})
    choices = read_json(path)
    if choices.get("candidates_sha256") != candidate_hash:
        raise ExperimentError("Curation file was generated for other candidates")
    items = choices.get("items", [])
    if len(items) != len(candidates) or {i.get("candidate_id") for i in items} != {p["package_id"] for p in candidates}:
        raise ExperimentError("Curation candidates changed or duplicated")
    if any(i.get("decision") == "pending" for i in items):
        raise ExperimentError(f"Human curation pending: {path}")
    original = {p["package_id"]: p for p in candidates}
    approved = []
    for item in items:
        if item.get("decision") not in ("approve", "correct", "reject"):
            raise ExperimentError("Curation decision must be approve, correct or reject")
        if not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip() or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ExperimentError("Curation requires reviewer and reason")
        if type(item.get("human_seconds")) not in (int, float) or not math.isfinite(item["human_seconds"]) or item["human_seconds"] < 0:
            raise ExperimentError("Curation requires measured, nonnegative human_seconds")
        old = original[item["candidate_id"]]
        if item["decision"] == "reject":
            continue
        clean = package_validate(item["package"])
        if item["decision"] == "approve" and clean != package_validate(old):
            raise ExperimentError("Edited packages require decision=correct")
        # Source provenance is assigned by the engine, never by edited model output.
        approved.append(clean | {"package_id": "cur-" + digest([old["package_id"], clean])[:20],
                        "source_instance_ids": old["source_instance_ids"], "status": "approved"})
    write_json(receipt, {"at": now(), "candidates_sha256": candidate_hash,
                        "decisions_sha256": digest(choices), "approved": approved})
    append_json(root / "audit.jsonl", {"at": now(), "action": "curation_committed", "area": area,
                "candidates": len(candidates), "approved": len(approved), "decisions_sha256": digest(choices)})
    return approved


def seed(root: Path, manifest: dict, retry: bool, workers: int = 1) -> None:
    seed_path = root / "seed.json"
    if seed_path.exists():
        # Validate human receipt on every subsequent execution.
        candidates = read_json(root / "warmup" / "candidates.json")
        approved = await_curation(root, "warmup", candidates)
        if read_json(seed_path) != approved:
            raise ExperimentError("Initial memory changed")
        return
    warmup = [case for case in manifest["cases"] if case["role"] == "warmup"]
    # A package is None when the extractor's answer was malformed (see package.json).
    candidates = [package for *_, package in perform(root, manifest, "none", warmup, [], root / "warmup",
                                                     retry, True, workers) if package is not None]
    write_json(root / "warmup" / "candidates.json", candidates)
    approved = await_curation(root, "warmup", candidates)
    if not approved:
        raise ExperimentError("No approved warmup package (all rejected, or no extractor answer was usable); a static "
                              "memory baseline needs an approved seed. Prepare a new run with other warmup cases")
    write_json(seed_path, approved)


def actions(manifest: dict) -> list[dict]:
    result = [{"type": "checkpoint", "block": 0}]
    for block in manifest["blocks"]:
        result.append({"type": "adapt", "block": block})
        result.append({"type": "checkpoint", "block": block})
    return result


def run_condition(root: Path, condition: str, *, live: bool = False, retry: bool = False, workers: int = 1) -> dict:
    """Run or resume one condition. `workers` is how many cases of a stage are in progress at a time.

    It is an argument of the invocation, not part of the manifest: it may differ between
    invocations of one run, and each one records it in audit.jsonl (`run_started`).
    """
    if condition not in CONDITIONS:
        raise ExperimentError("Unknown condition")
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ExperimentError(f"workers must be an integer from 1 to {MAX_WORKERS}")
    manifest = load_run(root)
    if manifest["config"]["backend"] != "demo" and not live:
        raise ExperimentError("Real execution requires --live; it may call the model and the benchmark's evaluation")
    if manifest["config"]["backend"] != "demo":
        backends.preflight_live(manifest["config"], manifest["runtime"], workers)
    audit(root, {"at": now(), "action": "run_started", "condition": condition, "workers": workers})
    if workers > 1 and manifest["config"]["backend"] != "demo" and local_model(manifest["config"]):
        print(f"Note: with {workers} workers the local model computes several calls at once, and its answer to "
              "the same prompt can then change from run to run (docs/PROTOCOL.md).", file=sys.stderr)
    seed(root, manifest, retry, workers)
    base = root / "conditions" / condition
    state_path = base / "state.json"
    initial = [] if condition == "none" else read_json(root / "seed.json")
    state = read_json(state_path) if state_path.exists() else {"cursor": 0, "memory": initial, "complete": False}
    # Validate committed past decisions without recomputing completed model calls.
    for receipt in base.glob("adapt-*/curation_receipt.json"):
        area = str(receipt.parent.relative_to(root))
        await_curation(root, area, read_json(receipt.parent / "candidates.json"))
    schedule = actions(manifest)
    while state["cursor"] < len(schedule):
        action = schedule[state["cursor"]]
        block = action["block"]
        if action["type"] == "adapt":
            if condition in ("automatic", "curated"):
                adapt = [case for case in manifest["cases"] if case["role"] == "adapt" and case["block"] == block]
                # Block-wise consolidation: both methods see the same pre-block snapshot.
                # A malformed extractor answer (package None) adds nothing to memory.
                candidates = [package for *_, package in perform(root, manifest, condition, adapt, state["memory"],
                                                                 base / f"adapt-{block}", retry, True, workers)
                              if package is not None]
                area = str((base / f"adapt-{block}").relative_to(root))
                write_json(root / area / "candidates.json", candidates)
                if condition == "curated":
                    new = await_curation(root, area, candidates)
                else:
                    new = [p | {"status": "approved"} for p in candidates]
                ids = {p["package_id"] for p in state["memory"]}
                state["memory"] += [p for p in new if p["package_id"] not in ids]
        else:
            snap = {"block": block, "memory": state["memory"], "memory_sha256": digest(state["memory"])}
            snapshot_path = base / "snapshots" / f"checkpoint-{block}.json"
            if snapshot_path.exists() and read_json(snapshot_path) != snap:
                raise ExperimentError("Read-only evaluation snapshot changed")
            write_json(snapshot_path, snap)
            # Case outcomes are local private artifacts; they never go into package extraction.
            reserved = [case for case in manifest["cases"] if case["role"] == "eval"]
            for case, event, reviewed, scored, _ in perform(root, manifest, condition, reserved, snap["memory"],
                                                            base / f"checkpoint-{block}", retry, False, workers):
                write_json(event / "case.json", {"instance_id": case["instance_id"], "family": case["family"],
                           "repo": case["public"]["repo"], "checkpoint": block,
                           "eligible_targets": len(case["targets"]), "findings": len(reviewed["findings"]),
                           "passed_targets": (sum(t["passed"] for t in scored["targets"])
                                              if scored["status"] == "ok" else None),
                           "reason": scored["reason"],
                           # How the agent's loop ended and its number of model replies (null for oneshot).
                           "agent_exit": reviewed.get("agent", {}).get("exit"),
                           "steps": reviewed.get("agent", {}).get("steps"),
                           "status": "ok", "synthetic": manifest["synthetic"]})
            if digest(state["memory"]) != snap["memory_sha256"]:
                raise ExperimentError("Evaluation mutated memory")
        state["cursor"] += 1
        state["complete"] = state["cursor"] == len(schedule)
        write_json(state_path, state)
    return {"condition": condition, "complete": True, "synthetic": manifest["synthetic"]}
