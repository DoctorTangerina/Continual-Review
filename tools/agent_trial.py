"""Trial of the agent solver on warmup cases: no memory, no grading, nothing written to runs/.

Runs the loop of ef_review.agent once per warmup case of an assignments CSV and keeps
each trajectory. It answers, before a run is prepared, whether the model can drive the
loop at all and what an attempt costs: the share of replies with exactly one command
block, how each attempt ended, whether it left a patch, steps, tokens and seconds.

Only rows with role `warmup` are run. Evaluation cases are never run here: their
outcomes stay reserved until every condition of a run has finished.

The reference fix is read only after an attempt has ended, to report whether the model
looked at the file that fix edits and whether its patch touches it. It never reaches
the model, and the patch is not graded.

Usage:
    python tools/agent_trial.py --config configs/swebench.local.json --output trials/agent-01
    python tools/agent_trial.py ... --assignments manifests/swebench_smoke.csv --instance-ids sympy__sympy-20590
    python tools/agent_trial.py --compare trials/agent-01 trials/agent-02

An attempt that already has attempt.json in the output folder is not run again.
--compare reads two output folders and reports, per case, the first step at which the
model's replies differ and whether the command output before it differed.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ef_review import swebench  # noqa: E402
from ef_review.core import ExperimentError, config_load, jsonl, load_dotenv, read_json, write_json  # noqa: E402


def trajectory(folder: Path) -> list[dict]:
    path = folder / "review.trajectory.json"
    return read_json(path)["messages"] if path.is_file() else []


def describe(folder: Path, instance: dict) -> dict:
    """One row of the summary, from the files an attempt left."""
    attempt, messages = read_json(folder / "attempt.json"), trajectory(folder)
    usage, agent = attempt["usage"], attempt["agent"]
    format_errors = sum(m.get("extra", {}).get("interrupt_type") == "FormatError" for m in messages)
    oracle = swebench.gold_files(instance["patch"])[0]
    return {"instance_id": instance["instance_id"], "exit": agent["exit"], "steps": agent["steps"],
            "replies_with_one_command": agent["steps"] - format_errors, "format_errors": format_errors,
            "patch_nonempty": bool(attempt["patch"]), "output_error": attempt.get("output_error"),
            "patch_files": [f["file"] for f in attempt["findings"]],
            # Analysis only: the reference fix is not shown to the model and nothing is graded.
            "oracle_path_in_commands": any(path in command for path in oracle for command in agent["commands"]),
            "patch_touches_oracle_file": any(f["file"] in oracle for f in attempt["findings"]),
            "prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
            "max_prompt_tokens": usage["max_prompt_tokens"],
            "model_seconds": round(usage["elapsed_seconds"], 1), "command_seconds": round(usage["command_seconds"], 1),
            "image": agent["image"]}


def summarize(rows: list[dict]) -> dict:
    steps = sum(r["steps"] for r in rows)
    exits: dict[str, int] = {}
    for r in rows:
        exits[r["exit"]] = exits.get(r["exit"], 0) + 1
    seconds = [r["model_seconds"] + r["command_seconds"] for r in rows]
    return {"attempts": len(rows), "exits": exits,
            "nonempty_patches": sum(r["patch_nonempty"] for r in rows),
            "replies": steps, "replies_with_one_command": sum(r["replies_with_one_command"] for r in rows),
            "oracle_path_in_commands": sum(r["oracle_path_in_commands"] for r in rows),
            "patch_touches_oracle_file": sum(r["patch_touches_oracle_file"] for r in rows),
            "median_steps": statistics.median(r["steps"] for r in rows) if rows else None,
            "median_seconds_per_attempt": round(statistics.median(seconds), 1) if rows else None,
            "seconds_per_step": round(sum(seconds) / steps, 1) if steps else None,
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "images_with_later_commits_or_remotes": [r["instance_id"] for r in rows
                                                     if r["image"].get("commits_after_base") or r["image"].get("remotes")]}


def run(args) -> int:
    load_dotenv(ROOT / ".env")
    cfg = config_load(args.config)
    if cfg["solver"] != "agent":
        raise ExperimentError('The trial is for "solver": "agent"')
    from ef_review import agent, backends
    backends.preflight_live(cfg | {"repair_enabled": False})  # model configured, Docker reachable; no harness needed
    dataset = {t["instance_id"]: t for t in jsonl(swebench.ensure_dataset(cfg))}
    with args.assignments.open(encoding="utf-8-sig", newline="") as stream:
        warmup = [row["instance_id"].strip() for row in csv.DictReader(stream) if row.get("role", "").strip() == "warmup"]
    wanted = args.instance_ids or warmup
    refused = [iid for iid in wanted if iid not in warmup]
    if refused:
        raise ExperimentError(f"Not warmup cases of {args.assignments.name}: {', '.join(refused)}")
    out = args.output.resolve()
    if (ROOT / "runs") in (out, *out.parents):
        raise ExperimentError("Write the trial outside runs/")
    write_json(out / "config.json", cfg)
    rows = []
    for iid in wanted:
        instance, folder = dataset[iid], out / iid
        if not (folder / "attempt.json").is_file():
            folder.mkdir(parents=True, exist_ok=True)
            print(f"{iid}: running", flush=True)
            case = {"instance": instance}
            value = agent.run_attempt(cfg, swebench.public_case(cfg, instance), [], swebench.sandbox_spec(case),
                                      folder / "review.raw.txt")
            write_json(folder / "attempt.json", value)
        row = describe(folder, instance)
        rows.append(row)
        print(f"{iid}: {row['exit']}, {row['steps']} steps, patch {'yes' if row['patch_nonempty'] else 'no'}, "
              f"{row['model_seconds'] + row['command_seconds']:.0f} s", flush=True)
    summary = {"model": cfg["review_model"], "summary": summarize(rows), "attempts": rows}
    write_json(out / "summary.json", summary)
    print(json.dumps(summary["summary"], indent=2))
    return 0


def compare(first: Path, second: Path) -> int:
    for folder in sorted(p for p in first.iterdir() if p.is_dir() and (second / p.name).is_dir()):
        a, b = trajectory(folder), trajectory(second / folder.name)
        step, verdict = 0, "identical"
        for index, (x, y) in enumerate(zip(a, b)):
            step += x.get("role") == "assistant"
            if x.get("content") != y.get("content"):
                if x.get("role") == "assistant":
                    before = a[index - 1].get("content") == b[index - 1].get("content")
                    verdict = (f"reply {step} differs; the message before it was "
                               f"{'the same' if before else 'different'}")
                else:
                    verdict = f"message {index} ({x.get('role')}) after reply {step} differs: command output varied"
                break
        else:
            if len(a) != len(b):
                verdict = f"same up to message {min(len(a), len(b))}, then one is longer ({len(a)} and {len(b)} messages)"
        print(f"{folder.name}: {verdict}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assignments", type=Path, default=ROOT / "manifests" / "swebench_catalog.csv")
    parser.add_argument("--instance-ids", nargs="*")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("FIRST", "SECOND"))
    args = parser.parse_args(argv)
    try:
        if args.compare:
            return compare(*args.compare)
        if not args.config or not args.output:
            parser.error("--config and --output are required")
        return run(args)
    except ExperimentError as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
