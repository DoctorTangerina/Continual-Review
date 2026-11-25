"""Compile statistics for one run folder (e.g. runs/swebench-smoke-auto).

Walks every event folder (warmup/<id>, conditions/<cond>/adapt-<n>/<id>,
conditions/<cond>/checkpoint-<n>/<id>) and reads the JSON artefacts it finds
there. Works on partial / in-progress runs, unlike `ef_review.reporting`.

Usage:
    python tools/run_stats.py runs/swebench-smoke-auto
    python tools/run_stats.py runs/swebench-smoke-auto --json stats.json --csv events.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


# --------------------------------------------------------------------------- helpers
def canonical(value) -> str:  # must match ef_review.core.canonical
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def case_dir(instance_id: str) -> str:  # folder name used by ef_review.engine
    return hashlib.sha256(canonical(instance_id).encode("utf-8")).hexdigest()[:16]


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def value(path: Path) -> dict:
    """Most stage files wrap their payload as {"input_sha256", "elapsed_seconds", "value"}."""
    data = load(path)
    if not isinstance(data, dict):
        return {}
    return data.get("value", data) if isinstance(data.get("value"), dict) else data


def elapsed(path: Path) -> float | None:
    data = load(path)
    return data.get("elapsed_seconds") if isinstance(data, dict) else None


def rate(n, d):
    return n / d if d else None


def pct(r) -> str:
    return "  n/a " if r is None else f"{100 * r:5.1f}%"


# --------------------------------------------------------------------------- collection
def collect_event(folder: Path, area: str, condition: str | None, stage: str, checkpoint: int | None,
                  case: dict | None) -> dict:
    ev = value(folder / "evaluation.json")
    review = value(folder / "review.json")
    package = value(folder / "package.json")
    retrieval = load(folder / "retrieval.json") or {}
    case_json = load(folder / "case.json") or {}

    targets = ev.get("targets") or []
    tests = ev.get("tests") or {}
    f2p, p2p = tests.get("FAIL_TO_PASS") or {}, tests.get("PASS_TO_PASS") or {}
    r_usage = review.get("usage") or load(folder / "review.raw.usage.json") or {}
    p_usage = package.get("usage") or load(folder / "package.raw.usage.json") or {}
    agent = review.get("agent") or {}      # solver "agent" only
    image = agent.get("image") or {}

    # Optional raw SWE-bench harness report
    harness = {}
    for rep in folder.glob("swebench_eval/logs/evaluation/*/*/*/report.json"):
        data = load(rep) or {}
        harness = next(iter(data.values()), {}) if data else {}
        break

    instance_id = (case or {}).get("instance_id") or case_json.get("instance_id") or folder.name
    eligible = case_json.get("eligible_targets", len(targets))
    passed = case_json.get("passed_targets")
    if passed is None:
        passed = sum(1 for t in targets if t.get("passed"))

    return {
        "area": area,
        "condition": condition or "",
        "stage": stage,
        "checkpoint": checkpoint,
        "folder": folder.name,
        "instance_id": instance_id,
        "role": (case or {}).get("role", ""),
        "family": (case or {}).get("family") or case_json.get("family", ""),
        "repo": ((case or {}).get("public") or {}).get("repo") or case_json.get("repo", ""),
        "evaluated": bool(ev),
        "eval_status": ev.get("status", ""),
        "reason": ev.get("reason", "missing" if not ev else ""),
        "eligible_targets": eligible,
        "passed_targets": passed,
        "resolved": bool(eligible) and passed == eligible,
        "repair_invoked": ev.get("repair_invoked"),
        "harness_ran": bool(tests),
        "f2p_passed": f2p.get("passed"),
        "f2p_failed": len(f2p.get("failed") or []) if tests else None,
        "p2p_passed": p2p.get("passed"),
        "p2p_failed": len(p2p.get("failed") or []) if tests else None,
        "f2p_failed_tests": ";".join(f2p.get("failed") or []),
        "p2p_failed_tests": ";".join(p2p.get("failed") or []),
        "patch_applied": harness.get("patch_successfully_applied"),
        "infra_failure": harness.get("infra_failure"),
        "findings": len(review.get("findings") or []),
        "patch_nonempty": bool(review.get("patch")),
        "patch_lines_changed": sum(1 for l in (review.get("patch") or "").splitlines()
                                   if l[:1] in "+-" and not l.startswith(("+++", "---"))),
        "edit_errors": len(review.get("edit_errors") or []),
        "edit_error_kinds": ";".join(sorted({e.get("error", "?") for e in review.get("edit_errors") or []})),
        # Malformed answers: the solver's (case scored invalid_model_output) and the extractor's
        # (stage completed with no package, so the case added nothing to memory).
        "output_error": review.get("output_error") or "",
        "package_output_error": package.get("output_error") or "",
        "without_package": bool(package) and package.get("package") is None,
        "review_prompt_tokens": r_usage.get("prompt_tokens"),
        "review_completion_tokens": r_usage.get("completion_tokens"),
        "review_seconds": r_usage.get("elapsed_seconds", elapsed(folder / "review.json")),
        "eval_seconds": elapsed(folder / "evaluation.json"),
        "package_prompt_tokens": p_usage.get("prompt_tokens"),
        "package_completion_tokens": p_usage.get("completion_tokens"),
        "package_seconds": p_usage.get("elapsed_seconds"),
        "retrieved_packages": len(retrieval.get("package_ids") or []),
        "retrieved_chars": retrieval.get("retrieved_chars"),
        "model": r_usage.get("model", ""),
        # Agent solver: how the loop ended, model replies, the largest prompt, and whether the
        # task image holds commits dated after the base commit or a Git remote (both should be 0).
        "agent_exit": agent.get("exit", ""),
        "steps": agent.get("steps"),
        "max_prompt_tokens": r_usage.get("max_prompt_tokens"),
        "command_seconds": r_usage.get("command_seconds"),
        "image_later_commits": image.get("commits_after_base"),
        "image_remotes": image.get("remotes"),
    }


def collect(root: Path, manifest: dict) -> list[dict]:
    by_dir = {case_dir(c["instance_id"]): c for c in manifest.get("cases", [])}
    events = []

    def scan(parent: Path, area, condition, stage, checkpoint):
        if not parent.is_dir():
            return
        for folder in sorted(p for p in parent.iterdir() if p.is_dir()):
            if (folder / "evaluation.json").exists() or (folder / "review.json").exists():
                events.append(collect_event(folder, area, condition, stage, checkpoint, by_dir.get(folder.name)))

    scan(root / "warmup", "warmup", None, "warmup", None)
    for cond_dir in sorted((root / "conditions").glob("*")):
        if not cond_dir.is_dir():
            continue
        for stage_dir in sorted(cond_dir.iterdir(), key=lambda p: (p.name.split("-")[0], _num(p.name))):
            name = stage_dir.name
            if name.startswith("checkpoint-"):
                scan(stage_dir, "checkpoint", cond_dir.name, name, _num(name))
            elif name.startswith("adapt-"):
                scan(stage_dir, "adapt", cond_dir.name, name, _num(name))
    return events


def _num(name: str) -> int:
    try:
        return int(name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return -1


# --------------------------------------------------------------------------- aggregation
def summarize(rows: list[dict]) -> dict:
    eligible = sum(r["eligible_targets"] or 0 for r in rows)
    passed = sum(r["passed_targets"] or 0 for r in rows)
    harness = [r for r in rows if r["harness_ran"]]
    s = lambda k: sum(r[k] or 0 for r in rows)
    return {
        "events": len(rows),
        "eligible_targets": eligible,
        "passed_targets": passed,
        "pass_rate": rate(passed, eligible),
        "resolved_instances": sum(r["resolved"] for r in rows),
        "instance_pass_rate": rate(sum(r["resolved"] for r in rows), len(rows)),
        "harness_runs": len(harness),
        "pass_rate_when_harness_ran": rate(sum(r["passed_targets"] or 0 for r in harness),
                                           sum(r["eligible_targets"] or 0 for r in harness)),
        "patch_proposed_rate": rate(sum(r["patch_nonempty"] for r in rows), len(rows)),
        "f2p_tests_passed": s("f2p_passed"),
        "f2p_tests_failed": s("f2p_failed"),
        "f2p_test_pass_rate": rate(s("f2p_passed"), s("f2p_passed") + s("f2p_failed")),
        "p2p_tests_passed": s("p2p_passed"),
        "p2p_tests_failed": s("p2p_failed"),
        "p2p_test_pass_rate": rate(s("p2p_passed"), s("p2p_passed") + s("p2p_failed")),
        "findings": s("findings"),
        "edit_errors": s("edit_errors"),
        "malformed_answers": sum(bool(r["output_error"]) for r in rows),
        "cases_without_package": sum(r["without_package"] for r in rows),
        "reasons": dict(Counter(r["reason"] or "?" for r in rows).most_common()),
        "review_tokens": {"prompt": s("review_prompt_tokens"), "completion": s("review_completion_tokens")},
        "package_tokens": {"prompt": s("package_prompt_tokens"), "completion": s("package_completion_tokens")},
        "seconds": {"review": round(s("review_seconds"), 1), "eval": round(s("eval_seconds"), 1),
                    "package": round(s("package_seconds"), 1), "commands": round(s("command_seconds"), 1)},
        "agent_exits": dict(Counter(r["agent_exit"] for r in rows if r["agent_exit"]).most_common()),
        "median_steps": statistics.median(r["steps"] for r in rows if r["steps"] is not None)
                        if any(r["steps"] is not None for r in rows) else None,
        "images_with_later_commits_or_remotes": sorted({r["instance_id"] for r in rows
                                                        if r["image_later_commits"] or r["image_remotes"]}),
    }


def group(rows, key) -> dict:
    out = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return {k: summarize(v) for k, v in out.items()}


def audit_stats(root: Path) -> dict:
    path = root / "audit.jsonl"
    if not path.exists():
        return {}
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    attempts = [l for l in lines if l.get("action") == "attempt"]
    completed = [l for l in lines if l.get("action") == "completed"]
    times = sorted(l["at"] for l in lines if "at" in l)
    return {
        "entries": len(lines),
        "attempts": len(attempts),
        "charged_calls": sum(1 for l in attempts if l.get("charged")),
        "completed": len(completed),
        "attempts_by_stage": dict(Counter(l.get("stage") for l in attempts)),
        "unfinished_attempts": len(attempts) - len(completed),
        # One entry per `run` invocation: the number of cases it kept in progress at a time.
        "workers_by_invocation": [l.get("workers") for l in lines if l.get("action") == "run_started"],
        "first": times[0] if times else None,
        "last": times[-1] if times else None,
    }


# --------------------------------------------------------------------------- printing
def table(title: str, groups: dict, label: str):
    print(f"\n== {title}")
    print(f"{label:<34} {'events':>6} {'pass':>5}/{'tgt':<4} {'rate':>6} {'harness':>7} {'F2P':>9} {'P2P':>9} {'patch%':>6}")
    for k, g in groups.items():
        f2p = f"{g['f2p_tests_passed']}/{g['f2p_tests_passed'] + g['f2p_tests_failed']}"
        p2p = f"{g['p2p_tests_passed']}/{g['p2p_tests_passed'] + g['p2p_tests_failed']}"
        print(f"{str(k):<34} {g['events']:>6} {g['passed_targets']:>5}/{g['eligible_targets']:<4} {pct(g['pass_rate']):>6} "
              f"{g['harness_runs']:>7} {f2p:>9} {p2p:>9} {pct(g['patch_proposed_rate']):>6}")


def report(root: Path, manifest: dict, rows: list[dict], stats: dict):
    cfg = manifest.get("config", {})
    print(f"Run: {root.name}   benchmark={cfg.get('benchmark')}   solver={cfg.get('solver', 'oneshot')}   "
          f"model={cfg.get('review_model')}   "
          f"repair={'on' if cfg.get('repair_enabled', True) else 'off'}   synthetic={manifest.get('synthetic')}")
    roles = Counter(c.get("role") for c in manifest.get("cases", []))
    print(f"Manifest cases: {dict(roles)}   blocks={manifest.get('blocks')}")
    for cond, st in stats["condition_state"].items():
        print(f"Condition {cond}: complete={st.get('complete')} cursor={st.get('cursor')} memory={st.get('memory_items')}")
    if stats["budget"]:
        a = stats["audit"]
        print(f"Budget: {a.get('charged_calls', '?')} charged calls used of max {stats['budget'].get('max_calls')} "
              f"(reserved {stats['budget'].get('reserved_calls')})")

    o = stats["overall"]
    print("\n== OVERALL")
    print(f"Events: {o['events']}   Targets passed: {o['passed_targets']}/{o['eligible_targets']}  "
          f"pass rate {pct(o['pass_rate']).strip()}")
    print(f"Harness actually ran: {o['harness_runs']}  (pass rate when it ran: {pct(o['pass_rate_when_harness_ran']).strip()})")
    print(f"Patch proposed: {pct(o['patch_proposed_rate']).strip()}   findings={o['findings']}   edit_errors={o['edit_errors']}")
    print(f"FAIL_TO_PASS tests: {o['f2p_tests_passed']}/{o['f2p_tests_passed'] + o['f2p_tests_failed']} "
          f"({pct(o['f2p_test_pass_rate']).strip()})   PASS_TO_PASS tests: "
          f"{o['p2p_tests_passed']}/{o['p2p_tests_passed'] + o['p2p_tests_failed']} ({pct(o['p2p_test_pass_rate']).strip()})")
    print(f"Malformed solver answers: {o['malformed_answers']}   cases without a package (malformed extractor "
          f"answer): {o['cases_without_package']}")
    print(f"Outcome reasons: {o['reasons']}")
    print(f"Tokens  review {o['review_tokens']}   package {o['package_tokens']}   seconds {o['seconds']}")
    if o["agent_exits"]:
        print(f"Agent exits: {o['agent_exits']}   median steps: {o['median_steps']}   "
              "(review tokens are summed over the steps of each attempt)")
    if o["images_with_later_commits_or_remotes"]:
        print(f"WARNING task images with commits dated after the base commit or with a Git remote: "
              f"{', '.join(o['images_with_later_commits_or_remotes'])}")

    table("By stage", stats["by_stage"], "stage")
    if stats["eval_by_condition_checkpoint"]:
        table("Eval checkpoints (condition / checkpoint)", stats["eval_by_condition_checkpoint"], "condition/checkpoint")
    table("By family", stats["by_family"], "family")
    table("By repo", stats["by_repo"], "repo")

    print("\n== Per test (event)")
    hdr = f"{'stage':<26} {'instance':<28} {'res':>3} {'reason':<26} {'F2P':>5} {'P2P':>5} {'find':>4} {'err':>3} {'tok in/out':>11} {'rev s':>6} {'eval s':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        stage = f"{r['condition']}/{r['stage']}" if r["condition"] else r["stage"]
        f2p = "-" if r["f2p_failed"] is None else f"{r['f2p_passed']}/{r['f2p_passed'] + r['f2p_failed']}"
        p2p = "-" if r["p2p_failed"] is None else f"{r['p2p_passed']}/{r['p2p_passed'] + r['p2p_failed']}"
        tok = f"{r['review_prompt_tokens'] or 0}/{r['review_completion_tokens'] or 0}"
        print(f"{stage:<26} {r['instance_id'][:28]:<28} {'Y' if r['resolved'] else 'n':>3} {r['reason'][:26]:<26} "
              f"{f2p:>5} {p2p:>5} {r['findings']:>4} {r['edit_errors']:>3} {tok:>11} "
              f"{(r['review_seconds'] or 0):>6.1f} {(r['eval_seconds'] or 0):>6.1f}")
        if r["f2p_failed_tests"]:
            print(f"{'':<26}   F2P failing: {r['f2p_failed_tests'].replace(';', ', ')}")
        if r["p2p_failed_tests"]:
            print(f"{'':<26}   P2P failing: {r['p2p_failed_tests'].replace(';', ', ')}")
        if r["edit_error_kinds"]:
            print(f"{'':<26}   edit errors: {r['edit_error_kinds']}")
        if r["agent_exit"]:
            print(f"{'':<26}   agent: {r['agent_exit']}, {r['steps']} steps, largest prompt {r['max_prompt_tokens']} tokens, "
                  f"commands {(r['command_seconds'] or 0):.0f} s")
        if r["output_error"]:
            print(f"{'':<26}   malformed answer: {r['output_error']}")
        if r["without_package"]:
            print(f"{'':<26}   no package: {r['package_output_error'] or '?'}")

    if stats["per_instance"]:
        print("\n== Per instance across stages (resolved? per stage)")
        for iid, seq in stats["per_instance"].items():
            print(f"{iid:<30} " + "  ".join(f"{s}:{'Y' if ok else 'n'}" for s, ok in seq))


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path, help="run folder, e.g. runs/swebench-smoke-auto")
    ap.add_argument("--json", type=Path, help="write full statistics to this JSON file")
    ap.add_argument("--csv", type=Path, help="write one row per event (test) to this CSV file")
    ap.add_argument("--quiet", action="store_true", help="do not print the text report")
    args = ap.parse_args(argv)

    root = args.run.resolve()
    manifest = load(root / "manifest.json")
    if not isinstance(manifest, dict):
        print(f"No readable manifest.json in {root}", file=sys.stderr)
        return 1

    rows = collect(root, manifest)
    if not rows:
        print(f"No event folders with evaluation.json/review.json found in {root}", file=sys.stderr)

    cond_state = {}
    for st in sorted((root / "conditions").glob("*/state.json")):
        d = load(st) or {}
        cond_state[st.parent.name] = {"complete": d.get("complete", False), "cursor": d.get("cursor"),
                                      "memory_items": len(d.get("memory") or [])}

    eval_rows = [r for r in rows if r["area"] == "checkpoint"]
    per_instance = defaultdict(list)
    for r in rows:
        per_instance[r["instance_id"]].append((f"{r['condition']}/{r['stage']}" if r["condition"] else r["stage"],
                                               r["resolved"]))

    stats = {
        "run": str(root),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "config": {k: manifest.get("config", {}).get(k) for k in
                   ("benchmark", "review_model", "repair_enabled", "swebench_dataset", "review_runner", "solver",
                    "agent_step_limit", "agent_command_timeout_seconds", "agent_observation_max_chars")},
        "synthetic": manifest.get("synthetic"),
        "budget": load(root / "budget.json") or {},
        "audit": audit_stats(root),
        "condition_state": cond_state,
        "overall": summarize(rows),
        "by_stage": group(rows, lambda r: f"{r['condition']}/{r['stage']}" if r["condition"] else r["stage"]),
        "by_area": group(rows, lambda r: r["area"]),
        "eval_by_condition_checkpoint": group(eval_rows, lambda r: f"{r['condition']}/cp{r['checkpoint']}"),
        "by_family": group(rows, lambda r: r["family"] or "?"),
        "by_repo": group(rows, lambda r: r["repo"] or "?"),
        "per_instance": dict(per_instance),
        "events": rows,
    }

    if not args.quiet:
        report(root, manifest, rows, stats)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.csv and rows:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
