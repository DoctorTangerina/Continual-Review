"""Descriptive outputs; a resolved task does not prove each proposed edit was correct."""
from __future__ import annotations

from collections import Counter
import csv
from pathlib import Path
import statistics

from .core import CONDITIONS, ExperimentError, digest, read_json, write_json
from .engine import load_run


def export_report(root: Path, annotations_path: Path | None = None) -> dict:
    manifest = load_run(root)
    for condition in CONDITIONS:
        state = root / "conditions" / condition / "state.json"
        if not state.exists() or not read_json(state).get("complete"):
            raise ExperimentError("Reserved results remain sealed until all four conditions complete")
    annotations = {}
    if annotations_path:
        for item in read_json(annotations_path):
            key = item["annotation_id"]
            if key in annotations:
                raise ExperimentError("Duplicate annotation ID")
            annotations[key] = item
    rows, template = [], []
    eval_cases = [c for c in manifest["cases"] if c["role"] == "eval"]
    # Runs without the harness have no verdicts: rates are None, never 0.
    repair = manifest["config"].get("repair_enabled", True)

    def rate(passed, total):
        return passed / total if repair else None
    for condition in CONDITIONS:
        for checkpoint in [0] + manifest["blocks"]:
            total = passed = findings_count = malformed = 0
            per_pr = []
            exits, steps, tokens = Counter(), [], Counter()
            detected = valid = invalid = 0
            annotated = True
            for case in eval_cases:
                folder = root / "conditions" / condition / f"checkpoint-{checkpoint}" / digest(case["instance_id"])[:16]
                result = read_json(folder / "case.json")
                review = read_json(folder / "review.json")["value"]
                findings = review["findings"]
                total += result["eligible_targets"]
                passed += result["passed_targets"] or 0
                findings_count += len(findings)
                # Read from the review, so the count also holds when the harness is disabled
                # and the case's reason is repair_disabled.
                malformed += bool(review.get("output_error"))
                if "agent" in review:
                    exits[review["agent"]["exit"]] += 1
                    steps.append(review["agent"]["steps"])
                    tokens["prompt"] += review["usage"]["prompt_tokens"]
                    tokens["completion"] += review["usage"]["completion_tokens"]
                per_pr.append((result["passed_targets"] or 0) / result["eligible_targets"])
                aid = digest([condition, checkpoint, case["instance_id"]])[:24]
                template.append({"annotation_id": aid, "condition": condition, "checkpoint": checkpoint,
                                 "instance_id": case["instance_id"], "findings": findings,
                                 "targets": [{"target_id": t["target_id"], "reference": t["comment_text"]} for t in case["targets"]],
                                 "status": "pending", "reviewer": "", "reason": "",
                                 "detected_target_ids": [], "valid_finding_indices": [], "invalid_finding_indices": []})
                item = annotations.get(aid)
                if not item or item.get("status") != "done":
                    annotated = False
                    continue
                if not item.get("reviewer") or not item.get("reason"):
                    raise ExperimentError("Completed annotation needs reviewer and reason")
                d, v, inv = item["detected_target_ids"], item["valid_finding_indices"], item["invalid_finding_indices"]
                if len(set(d)) != len(d) or not set(d) <= {t["target_id"] for t in case["targets"]}:
                    raise ExperimentError("Annotation has unknown or repeated target IDs")
                if d and not findings:
                    raise ExperimentError("An empty review cannot directly identify a target")
                if any(type(i) is not int for i in v + inv) or len(set(v + inv)) != len(v + inv) or set(v + inv) != set(range(len(findings))):
                    raise ExperimentError("Annotate each finding exactly once as valid or invalid (zero-based indices)")
                detected += len(d)
                valid += len(v)
                invalid += len(inv)
            rows.append({"condition": condition, "checkpoint": checkpoint, "instances": len(eval_cases),
                         "eligible_targets": total, "passed_targets": passed if repair else None,
                         "repair_target_rate": rate(passed, total),
                         "macro_pr_rate": rate(sum(per_pr), len(per_pr)), "findings": findings_count,
                         "invalid_model_output": malformed,
                         # Agent solver only (empty for oneshot): how the loops ended, e.g.
                         # "LimitsExceeded:3;Submitted:2", and the tokens summed over all steps.
                         "agent_exits": ";".join(f"{name}:{count}" for name, count in sorted(exits.items())),
                         "median_steps": statistics.median(steps) if steps else None,
                         "solver_prompt_tokens": tokens["prompt"] if steps else None,
                         "solver_completion_tokens": tokens["completion"] if steps else None,
                         "direct_target_recall": detected / total if annotated else None,
                         "comment_precision": valid / (valid + invalid) if annotated and valid + invalid else None,
                         "invalid_comments": invalid if annotated else None,
                         "annotations_complete": annotated, "synthetic": manifest["synthetic"]})
    unknown_annotations = set(annotations) - {item["annotation_id"] for item in template}
    if unknown_annotations:
        raise ExperimentError("Annotation file belongs to a different run")
    out = root / "reports"
    out.mkdir(exist_ok=True)
    with (out / "checkpoints.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    changes = []
    for condition in CONDITIONS:
        by_cp = {r["checkpoint"]: r for r in rows if r["condition"] == condition}
        changes.append({"condition": condition, "initial_to_final_repair_rate_change":
                        by_cp[manifest["blocks"][-1]]["repair_target_rate"] - by_cp[0]["repair_target_rate"]
                        if repair else None})
    family_rows = []
    for condition in CONDITIONS:
        for cp in [0] + manifest["blocks"]:
            for family in sorted({c["family"] for c in eval_cases}):
                matching = [c for c in eval_cases if c["family"] == family]
                results = [read_json(root / "conditions" / condition / f"checkpoint-{cp}" / digest(c["instance_id"])[:16] / "case.json") for c in matching]
                n = sum(c["eligible_targets"] for c in results)
                p = sum(c["passed_targets"] or 0 for c in results)
                family_rows.append({"condition": condition, "checkpoint": cp, "family": family,
                                    "eligible_targets": n, "passed_targets": p if repair else None,
                                    "repair_target_rate": rate(p, n)})
    write_json(out / "family_matrix.json", family_rows)
    if not (out / "annotations.template.json").exists():
        write_json(out / "annotations.template.json", template)
    curation = []
    for path in root.glob("**/curation_receipt.json"):
        choices = read_json(path.with_name("curation.json"))
        curation.append({"area": str(path.parent.relative_to(root)), "human_seconds": sum(i["human_seconds"] for i in choices["items"]),
                         "candidates": len(choices["items"]), "rejected": sum(i["decision"] == "reject" for i in choices["items"])})
    usage = []
    # Malformed answers over the whole run (warmup, adaptation and evaluation), per stage.
    malformed_calls = {"review": 0, "package": 0}
    for stage in malformed_calls:
        for path in root.glob(f"**/{stage}.json"):
            value = read_json(path)["value"]
            usage.append(value.get("usage", {}))
            malformed_calls[stage] += bool(value.get("output_error"))
    malformed_eval = {condition: {str(r["checkpoint"]): r["invalid_model_output"] for r in rows
                                  if r["condition"] == condition} for condition in CONDITIONS}
    summary = {"synthetic": manifest["synthetic"], "manifest_sha256": manifest["manifest_sha256"],
               "call_budget": read_json(root / "budget.json"), "checkpoints": rows, "changes": changes,
               "invalid_model_output": {"reserved_evaluation_by_checkpoint": malformed_eval,
                                        "reviewer_answers_whole_run": malformed_calls["review"],
                                        "extractor_answers_whole_run": malformed_calls["package"]},
               "curation": curation, "recorded_reviewer_and_extractor_usage": usage,
               "limitations": ["Descriptive results; repeated tasks/checkpoints are not independent samples.",
                               "Harness evaluations use no model; their Docker time and cost are not recorded here.",
                               "No statistical significance, code-regression guarantee or human-effort benefit inferred.",
                               "Direct detection and precision remain unknown without completed manual annotations."]}
    write_json(out / "summary.json", summary)
    label = "SYNTHETIC DEMO — NOT SCIENTIFIC RESULTS" if manifest["synthetic"] else "Descriptive experiment results"
    if repair:
        meaning = ("Each task is one target; it passes only when the synthetic demo verdict marks it resolved."
                   if manifest["synthetic"] else
                   "Each task is one target; it passes only when the SWE-bench harness reports it as resolved.")
        text = [f"# {label}", "", meaning, "",
                "| Condition | Checkpoint | Targets | Passed | Rate |", "|---|---:|---:|---:|---:|"]
        text += [f"| {r['condition']} | {r['checkpoint']} | {r['eligible_targets']} | {r['passed_targets']} | {r['repair_target_rate']:.3f} |" for r in rows]
    else:
        text = [f"# {label} (no harness)", "", "The SWE-bench harness was disabled: no task was graded, so there is no resolved rate.",
                "Detection and edit precision come only from completed manual annotations.", "",
                "| Condition | Checkpoint | Targets | Findings | Direct recall | Precision |", "|---|---:|---:|---:|---:|---:|"]
        fmt = lambda v: "pending" if v is None else f"{v:.3f}"
        text += [f"| {r['condition']} | {r['checkpoint']} | {r['eligible_targets']} | {r['findings']} | "
                 f"{fmt(r['direct_target_recall'])} | {fmt(r['comment_precision'])} |" for r in rows]
    per_condition = "; ".join(f"{condition} " + "/".join(str(n) for n in malformed_eval[condition].values())
                              for condition in CONDITIONS)
    checkpoints = "/".join(str(cp) for cp in [0] + manifest["blocks"])
    text += ["", "Malformed model answers (`invalid_model_output`: no usable edits, never sent to the harness) on the "
             f"reserved cases, checkpoints {checkpoints}: {per_condition}. Over the whole run "
             f"{malformed_calls['review']} solver and {malformed_calls['package']} extractor answers were malformed; "
             "a malformed extractor answer leaves its case without a package."]
    if any(r["agent_exits"] for r in rows):
        text += ["", "Agent solver: how the loops ended on the reserved cases, the median number of model replies and "
                 "the tokens summed over all steps. Experiences use part of the context window, so compare "
                 "`ContextExhausted` between `none` and the memory conditions.", "",
                 "| Condition | Checkpoint | Exits | Median steps | Prompt tokens | Output tokens |", "|---|---:|---|---:|---:|---:|"]
        text += [f"| {r['condition']} | {r['checkpoint']} | {r['agent_exits']} | {r['median_steps']} | "
                 f"{r['solver_prompt_tokens']} | {r['solver_completion_tokens']} |" for r in rows]
    text += ["", "See family_matrix.json for retention/transfer by family and annotations.template.json for direct assessment.",
             "", "No model-quality conclusion may be drawn from the synthetic demonstration."]
    (out / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return {"report": str(out / "report.md"), "synthetic": manifest["synthetic"]}
