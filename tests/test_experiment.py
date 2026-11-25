"""Offline tests for scientific data flow and restart safety, on the synthetic demo.

The demo uses SWE-bench-shaped tasks with pre-cached oracle files, a deterministic fake
solver and a synthetic verdict instead of the harness (see tests/test_swebench.py).
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))
from ef_review import backends, engine, swebench
from ef_review.core import (CONDITIONS, CONFIG_DEFAULTS, ExperimentError, ModelOutputError, RunLock, build_manifest, catalog,
                            config_load, digest, package_validate, read_json, retrieve, write_json)
from ef_review.reporting import export_report

SECRETS = ("PRIVATE_", "ANSWER_KEY")


def demo_config(folder: str, **overrides) -> Path:
    """A copy of the demo configuration with some keys replaced, loadable from another folder."""
    cfg = read_json(BASE / "configs" / "demo.json") | overrides
    for key in ("demo_dataset", "demo_cache"):
        cfg[key] = str((BASE / "configs" / cfg[key]).resolve())
    path = Path(folder) / "cfg.json"
    write_json(path, cfg)
    return path


def fake_litellm(text: str = "{}", finish_reason: str = "stop", supported=("temperature", "seed")):
    """Stands in for the litellm package, so these tests need neither it nor a provider.

    `calls` collects the arguments of each completion; `lookups` the model/provider
    pairs whose supported parameters were asked for.
    """
    module = types.ModuleType("litellm")
    module.calls, module.lookups = [], []

    def completion(**kwargs):
        module.calls.append(kwargs)
        message = types.SimpleNamespace(content=text, reasoning_content=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)],
                                     usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=2),
                                     model="coder:1b")

    def supported_params(model, custom_llm_provider=None):
        module.lookups.append((model, custom_llm_provider))
        return list(supported)

    module.completion = completion
    module.get_supported_openai_params = supported_params
    module.get_llm_provider = lambda model: (model.split("/", 1)[1], model.split("/", 1)[0], None, None)
    return module


class RunCase(unittest.TestCase):
    """A prepared run of the synthetic demo in a temporary folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.cfg = config_load(BASE / "configs" / "demo.json")
        self.assignments = BASE / "examples" / "demo_assignments.csv"
        self.root = self.work / "run"

    def prepare(self, cfg=None):
        manifest = build_manifest(cfg or self.cfg, self.assignments)
        engine.initialize(self.root, manifest)
        return read_json(self.root / "manifest.json")

    def approve_pending(self, decision="approve"):
        count = 0
        for path in self.root.glob("**/curation.json"):
            choices = read_json(path)
            for item in choices["items"]:
                if item["decision"] == "pending":
                    count += 1
                    item.update(decision=decision, reviewer="OFFLINE_TEST_ONLY", reason="Synthetic test", human_seconds=1)
            write_json(path, choices)
        return count

    def complete(self, condition, **options):
        for _ in range(10):
            try:
                return engine.run_condition(self.root, condition, **options)
            except ExperimentError as exc:
                if "Human curation pending:" not in str(exc):
                    raise
                self.assertGreater(self.approve_pending(), 0)
        self.fail("Did not complete")


class ExperimentTests(RunCase):
    def test_catalog_and_manifest_keep_target_population(self):
        rows, excluded = catalog(self.cfg)
        self.assertEqual(len(rows), 7)
        self.assertFalse(excluded)
        m = self.prepare()
        self.assertEqual(sum(len(c["targets"]) for c in m["cases"]), 7)
        self.assertTrue(m["synthetic"])
        engine.load_run(self.root)

    def test_answer_key_fields_are_not_in_solver_view(self):
        m = self.prepare()
        for case in m["cases"]:
            self.assertEqual(set(case["public"]), {"repo", "problem_statement", "files"})
            for secret in SECRETS:
                self.assertNotIn(secret, json.dumps(case["public"]))
                self.assertIn(secret, json.dumps(case["instance"]))  # kept only for grading/extraction

    def test_project_leakage_is_rejected(self):
        dataset = [json.loads(s) for s in Path(self.cfg["dataset_path"]).read_text().splitlines()]
        dataset[-1]["repo"] = dataset[1]["repo"]
        path = self.work / "cases.jsonl"
        path.write_text("\n".join(json.dumps(c) for c in dataset))
        with self.assertRaisesRegex(ExperimentError, "Source leakage"):
            build_manifest(self.cfg | {"dataset_path": str(path)}, self.assignments)

    def test_duplicate_selection_is_rejected(self):
        text = self.assignments.read_text()
        path = self.work / "duplicates.csv"
        path.write_text(text + "demo-adapt-alpha@1,adapt,1,alpha\n")
        with self.assertRaisesRegex(ExperimentError, "Duplicate"):
            build_manifest(self.cfg, path)

    def test_missing_warmup_is_rejected(self):
        path = self.work / "no-seed.csv"
        path.write_text("\n".join(s for s in self.assignments.read_text().splitlines() if "warmup" not in s))
        with self.assertRaisesRegex(ExperimentError, "warmup"):
            build_manifest(self.cfg, path)

    def test_dataset_change_is_detected(self):
        p = self.work / "dataset.jsonl"
        p.write_bytes(Path(self.cfg["dataset_path"]).read_bytes())
        self.prepare(self.cfg | {"dataset_path": str(p)})
        p.write_text(p.read_text() + "\n")
        with self.assertRaisesRegex(ExperimentError, "dataset changed"):
            engine.load_run(self.root)

    def test_graded_task_is_frozen_in_manifest(self):
        m = self.prepare()
        for case in m["cases"]:
            self.assertEqual(case["task_sha256"], digest(case["instance"]))
        m["cases"][0]["instance"]["FAIL_TO_PASS"].append("another_test")
        write_json(self.root / "manifest.json", m)
        with self.assertRaisesRegex(ExperimentError, "Manifest changed"):
            engine.load_run(self.root)

    def test_target_identity_depends_only_on_graded_tests(self):
        task = json.loads(Path(self.cfg["dataset_path"]).read_text().splitlines()[0])
        target = swebench.targets(task)[0]["target_id"]
        self.assertEqual(target, swebench.targets(task | {"problem_statement": "reworded"})[0]["target_id"])
        self.assertNotEqual(target, swebench.targets(task | {"PASS_TO_PASS": ["more"]})[0]["target_id"])

    def test_curation_pauses_and_resume_does_not_repeat_calls(self):
        self.prepare()
        with self.assertRaisesRegex(ExperimentError, "Human curation pending"):
            engine.run_condition(self.root, "curated")
        budget = read_json(self.root / "budget.json")
        with self.assertRaisesRegex(ExperimentError, "Human curation pending"):
            engine.run_condition(self.root, "curated")
        self.assertEqual(read_json(self.root / "budget.json"), budget)
        self.complete("curated")
        finished = read_json(self.root / "budget.json")
        engine.run_condition(self.root, "curated")
        self.assertEqual(read_json(self.root / "budget.json"), finished)

    def test_unapproved_or_irrelevant_memory_is_not_retrieved(self):
        p = {"package_id": "x", "status": "candidate", "episode": "episode", "rule": "alpha", "scope": "alpha",
             "exceptions": "none", "goal": "g", "question": "q", "metric": "m", "tags": ["alpha"]}
        task = {"repo": "r", "problem_statement": "alpha", "files": [{"path": "a.py", "content": "x"}]}
        self.assertFalse(retrieve([p], task, self.cfg))
        p["status"] = "approved"
        self.assertTrue(retrieve([p], task, self.cfg))
        self.assertFalse(retrieve([p | {"scope_repo": "another"}], task, self.cfg))
        self.assertFalse(retrieve([p], task, self.cfg | {"memory_max_chars": 2}))

    def test_reserved_cases_never_reach_extractor_and_snapshots_are_read_only(self):
        self.prepare()
        calls = []
        original = backends.extract

        def extract(cfg, case, *args):
            calls.append(case["role"])
            self.assertNotEqual(case["role"], "eval")
            return original(cfg, case, *args)

        with patch.object(backends, "extract", side_effect=extract):
            self.complete("automatic")
        self.assertEqual(calls.count("warmup"), 1)
        self.assertEqual(calls.count("adapt"), 3)
        for path in self.root.glob("conditions/automatic/snapshots/*.json"):
            snapshot = read_json(path)
            self.assertEqual(digest(snapshot["memory"]), snapshot["memory_sha256"])

    def test_static_memory_does_not_grow(self):
        self.prepare()
        self.complete("static")
        initial = read_json(self.root / "seed.json")
        for path in self.root.glob("conditions/static/snapshots/*.json"):
            self.assertEqual(read_json(path)["memory"], initial)

    def test_rejected_packages_do_not_enter_memory(self):
        self.prepare()
        self.complete("none")  # establishes the common seed
        while True:
            try:
                engine.run_condition(self.root, "curated")
                break
            except ExperimentError as exc:
                if "Human curation pending:" not in str(exc):
                    raise
                self.approve_pending("reject")
        self.assertEqual(read_json(self.root / "conditions/curated/state.json")["memory"], read_json(self.root / "seed.json"))

    def test_edited_approved_package_requires_correct_decision(self):
        self.prepare()
        with self.assertRaises(ExperimentError):
            engine.run_condition(self.root, "none")
        self.approve_pending()
        path = self.root / "warmup/curation.json"
        choices = read_json(path)
        choices["items"][0]["package"]["rule"] = "changed"
        write_json(path, choices)
        with self.assertRaisesRegex(ExperimentError, "decision=correct"):
            engine.run_condition(self.root, "none")

    def test_committed_curation_cannot_change_silently(self):
        self.prepare()
        self.complete("none")
        path = self.root / "warmup/curation.json"
        choices = read_json(path)
        choices["items"][0]["reason"] = "revised after evaluation"
        write_json(path, choices)
        with self.assertRaisesRegex(ExperimentError, "Committed curation"):
            engine.run_condition(self.root, "none")

    def test_call_cap_is_shared_and_cannot_be_bypassed_by_resume(self):
        self.prepare(self.cfg | {"max_calls": 1})
        with self.assertRaisesRegex(ExperimentError, "limit reached"):
            engine.run_condition(self.root, "automatic")
        with self.assertRaisesRegex(ExperimentError, "limit reached"):
            engine.run_condition(self.root, "curated")
        self.assertEqual(read_json(self.root / "budget.json")["reserved_calls"], 1)

    def test_failed_calls_need_explicit_retry_and_keep_audit(self):
        self.prepare()
        with patch.object(backends, "review", side_effect=RuntimeError("failed provider")):
            with self.assertRaisesRegex(ExperimentError, "Stage failed"):
                engine.run_condition(self.root, "none")
        with self.assertRaisesRegex(ExperimentError, "previously failed"):
            engine.run_condition(self.root, "none")
        with self.assertRaisesRegex(ExperimentError, "Human curation pending"):
            engine.run_condition(self.root, "none", retry=True)
        self.assertEqual(read_json(self.root / "budget.json")["reserved_calls"], 3)
        self.assertIn('"failed"', (self.root / "audit.jsonl").read_text())

    def test_report_stays_sealed_until_all_conditions_complete(self):
        self.prepare()
        self.complete("none")
        with self.assertRaisesRegex(ExperimentError, "sealed"):
            export_report(self.root)

    def test_explicit_retry_archives_failed_raw_outputs(self):
        self.prepare()
        event = self.root / "custom-event"

        def fail():
            (event / "review.raw.txt").write_text("invalid first reply")
            raise ValueError("parse failed")

        with self.assertRaisesRegex(ExperimentError, "Stage failed"):
            engine.cached(self.root, event, "review", {"x": 1}, fail, retry_failed=False)

        def succeed():
            (event / "review.raw.txt").write_text("valid second reply")
            return {"findings": []}

        engine.cached(self.root, event, "review", {"x": 1}, succeed, retry_failed=True)
        self.assertEqual((event / "attempt_history/review-1/review.raw.txt").read_text(), "invalid first reply")
        self.assertEqual((event / "review.raw.txt").read_text(), "valid second reply")

    def test_empty_review_counts_all_targets_and_precision_is_unknown(self):
        self.prepare()
        for condition in CONDITIONS:
            self.complete(condition)
        export_report(self.root)
        summary = read_json(self.root / "reports/summary.json")
        self.assertTrue(summary["synthetic"])
        for row in summary["checkpoints"]:
            self.assertEqual(row["eligible_targets"], 3)
            self.assertIsNone(row["comment_precision"])
            if row["condition"] == "none":
                self.assertEqual(row["passed_targets"], 0)
        self.assertIn("SYNTHETIC", (self.root / "reports/report.md").read_text())

    def test_double_writer_lock_is_rejected(self):
        self.root.mkdir()
        with RunLock(self.root):
            with self.assertRaisesRegex(ExperimentError, "locked"):
                with RunLock(self.root):
                    pass
        self.assertFalse((self.root / ".run.lock").exists())

    def test_modified_budget_limit_is_rejected(self):
        self.prepare()
        budget = read_json(self.root / "budget.json")
        budget["max_calls"] += 1
        write_json(self.root / "budget.json", budget)
        with self.assertRaisesRegex(ExperimentError, "budget record"):
            engine.load_run(self.root)

    def test_runtime_preflight_refuses_missing_models_without_calls(self):
        with self.assertRaisesRegex(ExperimentError, "explicit"):
            backends.preflight_live({"review_model": None})

    def test_partial_checkpoint_resume_reuses_completed_case_calls(self):
        m = self.prepare()
        self.complete("none")
        original = backends.review
        attempts = 0

        def flaky(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("transient")
            return original(*args, **kwargs)

        with patch.object(backends, "review", side_effect=flaky):
            with self.assertRaisesRegex(ExperimentError, "Stage failed"):
                engine.run_condition(self.root, "static")
        first = self.root / "conditions/static/checkpoint-0" / digest(next(c for c in m["cases"] if c["role"] == "eval")["instance_id"])[:16] / "review.json"
        before = first.read_bytes()
        engine.run_condition(self.root, "static", retry=True)
        self.assertEqual(before, first.read_bytes())

    def test_malformed_extractor_package_is_recorded_and_the_run_completes(self):
        m = self.prepare()
        original = backends.extract

        def extract(cfg, case, *args):
            output = original(cfg, case, *args)
            if case["instance_id"] == "demo-adapt-beta@1":   # the only case of block 2
                output["package"] = {"episode": "an episode and nothing else", "tags": "not-a-list"}
            return output

        with patch.object(backends, "extract", side_effect=extract):
            for condition in CONDITIONS:
                self.complete(condition)
            export_report(self.root)
        folder = digest("demo-adapt-beta@1")[:16]
        for condition in ("automatic", "curated"):
            base = self.root / "conditions" / condition
            package = read_json(base / "adapt-2" / folder / "package.json")["value"]
            self.assertEqual((package["package"], package["output_error"]), (None, "invalid_package"))
            self.assertIn("usage", package)
            self.assertFalse((base / "adapt-2" / folder / "package.failed.json").exists())
            self.assertEqual(read_json(base / "adapt-2" / "candidates.json"), [])
            self.assertEqual(len(read_json(base / "adapt-1" / "candidates.json")), 1)
            self.assertEqual(len(read_json(base / "adapt-3" / "candidates.json")), 1)
            sources = [p["source_instance_ids"] for p in read_json(base / "state.json")["memory"]]
            self.assertEqual(sources, [["demo-warmup-alpha@1"], ["demo-adapt-alpha@1"], ["demo-adapt-gamma@1"]])
        summary = read_json(self.root / "reports/summary.json")
        self.assertEqual(summary["invalid_model_output"]["extractor_answers_whole_run"], 2)
        self.assertEqual(len([c for c in m["cases"] if c["role"] == "adapt"]), 3)

    def test_warmup_with_no_usable_package_cannot_seed_the_memory(self):
        self.prepare()
        unusable = {"package": None, "output_error": "invalid_json", "usage": {"model": "synthetic-demo"}}
        with patch.object(backends, "extract", return_value=unusable):
            with self.assertRaisesRegex(ExperimentError, "No approved warmup package"):
                engine.run_condition(self.root, "none")
        self.assertEqual(read_json(self.root / "warmup/candidates.json"), [])
        self.assertFalse((self.root / "seed.json").exists())

    def test_malformed_package_from_human_curation_stays_fatal(self):
        bad = {"episode": "e", "rule": "", "scope": "s", "exceptions": "x", "goal": "g", "question": "q",
               "metric": "m", "tags": []}
        for package in (bad, bad | {"rule": "r", "tags": "t"}, ["not", "an", "object"]):
            with self.assertRaises(ModelOutputError) as raised:
                package_validate(package)
            self.assertEqual(raised.exception.code, "invalid_package")
        self.prepare()
        with self.assertRaisesRegex(ExperimentError, "Human curation pending"):
            engine.run_condition(self.root, "none")
        self.approve_pending("correct")
        path = self.root / "warmup/curation.json"
        choices = read_json(path)
        choices["items"][0]["package"]["rule"] = "  "
        write_json(path, choices)
        with self.assertRaisesRegex(ExperimentError, "Package requires"):
            engine.run_condition(self.root, "none")
        self.assertFalse((self.root / "seed.json").exists())
        self.assertFalse((self.root / "warmup/curation_receipt.json").exists())

    def test_demo_scores_a_malformed_answer_as_invalid_model_output(self):
        case = {"targets": [{"target_id": "t"}]}
        scored = backends.evaluate(self.cfg, case, {"patch": "", "edit_errors": [], "output_error": "invalid_json"},
                                   self.work)
        self.assertEqual((scored["reason"], scored["targets"], scored["repair_invoked"]),
                         ("invalid_model_output", [{"target_id": "t", "passed": False}], False))

    def test_model_json_is_strict_and_empty_edits_valid(self):
        self.assertEqual(swebench.edits_validate({"edits": []}), [])
        with self.assertRaises(ExperimentError):
            backends.parse_object("some prose")
        with self.assertRaises(ExperimentError):
            swebench.edits_validate({"findings": []})

    def test_demo_never_fetches_missing_context(self):
        task = json.loads(Path(self.cfg["dataset_path"]).read_text().splitlines()[0])
        with patch("ef_review.swebench.urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(ExperimentError, "never uses the network"):
                swebench.fetch_context(self.cfg, task | {"instance_id": "demo-missing@1"})
        urlopen.assert_not_called()

    def test_live_execution_requires_flag_before_any_seed_calls(self):
        m = self.prepare()
        m["config"]["backend"] = "litellm"
        with patch.object(engine, "load_run", return_value=m), patch.object(engine, "seed") as seed:
            with self.assertRaisesRegex(ExperimentError, "--live"):
                engine.run_condition(self.root, "none")
            seed.assert_not_called()


class WorkersTests(RunCase):
    """Several cases of a stage in progress at the same time (`run --workers`)."""
    LOCAL = {"backend": "litellm", "review_model": "ollama_chat/coder:1b", "review_num_ctx": 32768,
             "review_reasoning": False, "max_output_tokens": 2000, "review_timeout_seconds": 60,
             "review_temperature": 0, "review_seed": 0, "review_unload_model": True}

    def one_block(self) -> None:
        """Put the three adaptation cases in one block, so that they form one group."""
        self.assignments = self.work / "assignments.csv"
        text = (BASE / "examples" / "demo_assignments.csv").read_text(encoding="utf-8")
        self.assignments.write_text(text.replace(",adapt,2,", ",adapt,1,").replace(",adapt,3,", ",adapt,1,"),
                                    encoding="utf-8")

    def event(self, manifest: dict, area: str, role: str, index: int) -> Path:
        case = [c for c in manifest["cases"] if c["role"] == role][index]
        return self.root / area / digest(case["instance_id"])[:16]

    def test_three_workers_produce_the_same_run_as_one(self):
        self.one_block()
        kept = ("candidates.json", "curation_receipt.json", "seed.json", "state.json", "case.json", "retrieval.json",
                "review.raw.txt", "package.raw.txt", "summary.json", "family_matrix.json")
        runs = []
        for workers in (1, 3):
            self.root = self.work / f"run-{workers}"
            self.prepare()
            for condition in CONDITIONS:
                self.complete(condition, workers=workers)
            export_report(self.root)
            files = {str(path.relative_to(self.root)): path.read_text(encoding="utf-8")
                     for path in sorted(self.root.rglob("*")) if path.name in kept or path.parent.name == "snapshots"}
            # The receipt carries the time of the decision.
            files = {name: text if "curation_receipt" not in name else json.dumps(
                {k: v for k, v in json.loads(text).items() if k != "at"}) for name, text in files.items()}
            stages = {str(path.relative_to(self.root)): (read_json(path)["input_sha256"], read_json(path)["value"])
                      for path in sorted(self.root.rglob("*.json")) if path.name in ("review.json", "evaluation.json", "package.json")}
            runs.append((files, stages, read_json(self.root / "budget.json")))
        self.assertGreater(len(runs[0][0]), 40)
        self.assertEqual(runs[0], runs[1])

    def test_each_invocation_records_its_workers(self):
        self.prepare()
        self.complete("none", workers=2)
        started = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()
                   if '"run_started"' in line]
        self.assertEqual([(entry["condition"], entry["workers"]) for entry in started], [("none", 2), ("none", 2)])

    def test_workers_outside_the_range_are_refused_before_anything_runs(self):
        self.prepare()
        for workers in (0, 17, 2.0, True):
            with self.assertRaisesRegex(ExperimentError, "workers must be an integer from 1 to 16"):
                engine.run_condition(self.root, "none", workers=workers)
        self.assertFalse((self.root / "audit.jsonl").exists())

    def test_many_threads_charge_the_budget_without_losing_a_call(self):
        self.prepare(self.cfg | {"max_calls": 500})
        jobs = [(lambda i=i: engine.cached(self.root, self.root / "events" / str(i), "review", {"i": i},
                                           lambda: i, retry_failed=False)) for i in range(200)]
        self.assertEqual(engine.together(jobs), list(range(200)))
        self.assertEqual(read_json(self.root / "budget.json")["reserved_calls"], 200)
        lines = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(entry["action"] for entry in lines), ["attempt"] * 200 + ["completed"] * 200)

    def test_call_limit_in_the_middle_of_a_group_is_not_exceeded(self):
        self.prepare(self.cfg | {"max_calls": 3})
        with self.assertRaisesRegex(ExperimentError, "Human curation pending"):
            engine.run_condition(self.root, "none", workers=3)       # the warmup uses 2 of the 3 calls
        self.approve_pending()
        with self.assertRaisesRegex(ExperimentError, r"limit reached.*1 more of the 3 cases"):
            engine.run_condition(self.root, "none", workers=3)
        self.assertEqual(read_json(self.root / "budget.json")["reserved_calls"], 3)

    def test_results_keep_the_order_of_the_cases_when_the_first_is_the_slowest(self):
        self.one_block()
        manifest = self.prepare()
        self.complete("none")
        original = backends.review
        slow = [c for c in manifest["cases"] if c["role"] == "adapt"][0]["public"]

        def review(cfg, task, *args):
            if task == slow:
                time.sleep(0.2)
            return original(cfg, task, *args)

        with patch.object(backends, "review", side_effect=review):
            self.complete("automatic", workers=3)
        candidates = read_json(self.root / "conditions/automatic/adapt-1/candidates.json")
        self.assertEqual([p["source_instance_ids"][0] for p in candidates],
                         [c["instance_id"] for c in manifest["cases"] if c["role"] == "adapt"])

    def test_a_failed_case_lets_its_group_finish_and_stops_the_next_group(self):
        manifest = self.prepare()
        self.complete("none")                                          # seed approved; `static` now has only checkpoints
        reserved = [c["public"] for c in manifest["cases"] if c["role"] == "eval"]
        original, calls = backends.review, []

        def review(cfg, task, *args):
            calls.append(task)
            if task == reserved[0]:
                raise RuntimeError("provider failed")
            time.sleep(0.1)                                            # still running when the other one fails
            return original(cfg, task, *args)

        folders = [self.event(manifest, "conditions/static/checkpoint-0", "eval", i) for i in range(3)]
        with patch.object(backends, "review", side_effect=review):
            with self.assertRaisesRegex(ExperimentError, f"Stage failed .*{folders[0].name}/review"):
                engine.run_condition(self.root, "static", workers=2)
            self.assertTrue((folders[0] / "review.failed.json").exists())
            self.assertTrue((folders[1] / "review.json").exists())     # the case in progress was saved
            self.assertFalse((folders[1] / "evaluation.json").exists())  # the group did not go on to the next step
            self.assertFalse(folders[2].exists())                      # the next group did not start
            self.assertEqual(len(calls), 2)
            reserved[0] = None                                         # the provider works again
            engine.run_condition(self.root, "static", workers=2, retry=True)
        # Only the failed attempt was repeated: 1 retry, the third case, then the 3 other checkpoints.
        self.assertEqual(len(calls), 2 + 1 + 1 + 9)

    def test_error_reports_the_first_failed_case_and_counts_the_others(self):
        def fail(name):
            raise ExperimentError(name)
        with self.assertRaisesRegex(ExperimentError, r"^second \(1 more of the 3 cases running together also failed\)$"):
            engine.together([lambda: 1, lambda: fail("second"), lambda: fail("third")])
        with self.assertRaisesRegex(KeyError, "plain"):                # other errors keep their type
            engine.together([lambda: {}["plain"], lambda: 1])

    def test_first_interrupt_waits_for_the_cases_in_progress(self):
        finished = []

        def interrupted():
            time.sleep(0.1)                                            # the main thread is waiting by now
            signal.raise_signal(signal.SIGINT)                         # what Ctrl+C sends
            time.sleep(0.2)
            finished.append("a")

        def other():
            time.sleep(0.6)
            finished.append("b")

        message = io.StringIO()
        with contextlib.redirect_stderr(message), self.assertRaises(KeyboardInterrupt):
            engine.together([interrupted, other])
        self.assertEqual(finished, ["a", "b"])
        self.assertIn("Interrupted: no new case starts; waiting for the", message.getvalue())

    def test_model_steps_of_a_group_share_one_load_only_for_a_local_model_that_is_unloaded(self):
        manifest = self.prepare()
        reserved = [c for c in manifest["cases"] if c["role"] == "adapt"]
        inside = []

        def stub(name, value):
            def run(*args):
                inside.append((name, getattr(backends._thread, "load", None) is not None))
                return value
            return run

        verdict = lambda cfg, case, *args: {"status": "ok", "reason": "x", "targets": [
            {"target_id": t["target_id"], "passed": False} for t in case["targets"]]}
        for number, (config, shared) in enumerate([(self.LOCAL, True), (self.LOCAL | {"review_unload_model": False}, False),
                                                   (self.LOCAL | {"review_model": "nvidia_nim/some/model"}, False),
                                                   (manifest["config"], False)]):
            inside.clear()
            with patch.object(backends, "review", side_effect=stub("review", {"findings": [], "patch": ""})), \
                 patch.object(backends, "extract", side_effect=stub("extract", {"package": None})), \
                 patch.object(backends, "evaluate", side_effect=lambda *a: (stub("evaluate", None)(), verdict(*a))[1]):
                list(engine.perform(self.root, manifest | {"config": config}, "none", reserved, [],
                                    self.root / f"area-{number}", False, True, workers=3))
            self.assertEqual(sorted(set(inside)), [("evaluate", False), ("extract", shared), ("review", shared)])

    def together_calls(self, jobs, litellm, unload):
        with patch.dict(sys.modules, {"litellm": litellm}), patch.object(backends, "ollama_unload", side_effect=unload):
            return engine.together(jobs, shared_load=True)

    def call(self, name="review"):
        return backends.call_model(self.LOCAL, "system", {"k": name}, self.work / f"{name}-{threading.get_ident()}.raw.txt")

    def test_shared_load_unloads_once_and_only_when_every_member_is_ready(self):
        litellm, order = fake_litellm(), []
        real_completion = litellm.completion
        litellm.completion = lambda **kwargs: (order.append("call"), real_completion(**kwargs))[1]

        def late():
            time.sleep(0.2)
            order.append("late member ready")
            return self.call()

        def saved_stage():                                             # a member that makes no call
            order.append("no call")

        results = self.together_calls([self.call, late, saved_stage, self.call], litellm,
                                      lambda cfg: order.append("unload"))
        self.assertEqual(order, ["no call", "late member ready", "unload", "call", "call", "call"])
        self.assertTrue(all(r["usage"]["model_unloaded_before_call"] for r in results if r))
        # Each usage record says how many cases were in progress with its call.
        self.assertEqual([r["usage"]["parallel_cases"] for r in results if r], [4, 4, 4])
        with patch.dict(sys.modules, {"litellm": litellm}), patch.object(backends, "ollama_unload"):
            self.assertEqual(self.call()["usage"]["parallel_cases"], 1)

    def test_shared_load_does_not_wait_for_a_member_that_failed_before_its_call(self):
        litellm, order = fake_litellm(), []

        def broken():
            raise ExperimentError("container did not start")

        with self.assertRaisesRegex(ExperimentError, "container did not start"):
            self.together_calls([broken, self.call, self.call], litellm, lambda cfg: order.append("unload"))
        self.assertEqual((order, len(litellm.calls)), (["unload"], 2))

    def test_failed_shared_unload_stops_every_call_of_the_group(self):
        litellm = fake_litellm()

        def unload(cfg):
            raise ExperimentError("still loaded")

        with self.assertRaisesRegex(ExperimentError, r"1 more of the 2 cases"):
            self.together_calls([self.call, self.call], litellm, unload)
        self.assertEqual(litellm.calls, [])

    def test_second_fresh_load_inside_a_group_is_refused(self):
        litellm, order = fake_litellm(), []
        with self.assertRaisesRegex(ExperimentError, "second fresh model load"):
            self.together_calls([lambda: (self.call(), self.call()), self.call], litellm,
                                lambda cfg: order.append("unload"))
        self.assertEqual((order, len(litellm.calls)), (["unload"], 2))

    def test_server_that_runs_the_calls_one_after_another_is_refused(self):
        # (end, seconds computed) of each call, as measured on Ollama 0.40.16 with 1 and with 2 slots.
        measured = {"2 calls, 2 slots": ([(9.34, 6.27), (9.34, 6.27)], True),
                    "2 calls, 1 slot": ([(12.14, 3.23), (8.93, 4.05)], False),
                    "3 calls, 1 slot": ([(15.45, 3.16), (12.29, 3.19), (9.09, 4.26)], False),
                    "3 calls, 2 slots": ([(11.18, 6.35), (11.31, 5.31), (14.56, 3.24)], False)}
        for name, (calls, accepted) in measured.items():
            pending, unloads = list(calls), []
            with self.subTest(name), patch.object(backends, "ollama_unload", side_effect=unloads.append), \
                 patch.object(backends, "ollama_timed_call", side_effect=lambda cfg: pending.pop()):
                if accepted:
                    backends.ollama_parallel_check(self.LOCAL, len(calls))
                else:
                    with self.assertRaisesRegex(ExperimentError, f"OLLAMA_NUM_PARALLEL={len(calls)}"):
                        backends.ollama_parallel_check(self.LOCAL, len(calls))
                self.assertEqual((pending, len(unloads)), ([], 1))     # measured on a freshly loaded model
        with patch.object(backends, "ollama_unload"), \
             patch.object(backends, "ollama_timed_call", side_effect=OSError("refused")):
            with self.assertRaisesRegex(ExperimentError, "did not answer 2 calls sent together"):
                backends.ollama_parallel_check(self.LOCAL, 2)

    def test_the_server_is_measured_only_for_a_local_model_with_more_than_one_worker(self):
        local = self.LOCAL | {"solver": "oneshot", "repair_enabled": False}
        hosted = local | {"review_model": "nvidia_nim/some/model"}
        with patch.object(backends.importlib.util, "find_spec", return_value=object()), \
             patch.object(backends, "ollama_parallel_check") as check:
            backends.preflight_live(local)
            backends.preflight_live(local, None, 1)
            backends.preflight_live(hosted, None, 4)
            check.assert_not_called()
            backends.preflight_live(local, None, 4)
            check.assert_called_once_with(local, 4)


class ConfigTests(unittest.TestCase):
    def test_removed_ccrab_benchmark_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            write_json(path, {"backend": "litellm", "benchmark": "ccrab", "ccrab_root": "."})
            with self.assertRaisesRegex(ExperimentError, "swebench"):
                config_load(path)


class SolverConfigTests(unittest.TestCase):
    AGENT = {"backend": "litellm", "solver": "agent", "review_num_ctx": 32768}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def load(self, cfg: dict) -> dict:
        path = Path(self.tmp) / "cfg.json"
        write_json(path, cfg)
        return config_load(path)

    def test_agent_is_the_default_and_every_shipped_configuration_names_its_solver(self):
        self.assertEqual(CONFIG_DEFAULTS["solver"], "agent")
        self.assertEqual((CONFIG_DEFAULTS["agent_step_limit"], CONFIG_DEFAULTS["agent_command_timeout_seconds"],
                          CONFIG_DEFAULTS["agent_observation_max_chars"], CONFIG_DEFAULTS["agent_oracle_paths"]),
                         (30, 120, 4000, False))
        for path in sorted((BASE / "configs").glob("*.json")):
            self.assertIn(read_json(path).get("solver"), ("oneshot", "agent"), msg=path.name)
        self.assertEqual(config_load(BASE / "configs" / "demo.json")["solver"], "oneshot")

    def test_agent_is_refused_where_it_cannot_run(self):
        demo = read_json(demo_config(self.tmp))
        with self.assertRaisesRegex(ExperimentError, "demo has no agent"):
            self.load({k: v for k, v in demo.items() if k != "solver"})      # the default is agent
        with self.assertRaisesRegex(ExperimentError, "review_num_ctx"):
            self.load(self.AGENT | {"review_num_ctx": None})
        with self.assertRaisesRegex(ExperimentError, "claude_cli is a single call"):
            self.load(self.AGENT | {"review_runner": "claude_cli", "review_reasoning": True,
                                    "review_temperature": None, "review_seed": None})

    def test_rejected_values(self):
        demo = read_json(demo_config(self.tmp))
        for key, values in (("solver", ("react", None, True)),
                            ("agent_step_limit", (0, -1, 2.5, True, "30")),
                            ("agent_command_timeout_seconds", (0, 1.5, "120", None)),
                            ("agent_observation_max_chars", (199, 0, "4000", True)),
                            ("agent_oracle_paths", (0, "false", None))):
            for value in values:
                with self.assertRaisesRegex(ExperimentError, key, msg=repr(value)):
                    self.load(demo | {key: value})


class SamplingConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def load(self, **overrides) -> dict:
        return config_load(demo_config(self.tmp, **overrides))

    def test_sampling_is_pinned_by_default(self):
        cfg = config_load(BASE / "configs" / "demo.json")
        self.assertEqual((cfg["review_temperature"], cfg["review_seed"]), (0, 0))
        self.assertIs(type(cfg["review_temperature"]), int)   # not False: 0 must still be sent
        self.assertIs(type(cfg["review_seed"]), int)

    def test_accepted_values(self):
        for temperature in (0, 0.8, 2, None):
            self.assertEqual(self.load(review_temperature=temperature)["review_temperature"], temperature)
        for seed in (0, 7, 2**31, None):
            self.assertEqual(self.load(review_seed=seed)["review_seed"], seed)

    def test_rejected_values(self):
        for temperature in ("0", True, False, -1, 2.5, float("nan"), [0]):
            with self.assertRaisesRegex(ExperimentError, "review_temperature", msg=repr(temperature)):
                self.load(review_temperature=temperature)
        for seed in (1.5, -1, True, False, "7", 0.0):
            with self.assertRaisesRegex(ExperimentError, "review_seed", msg=repr(seed)):
                self.load(review_seed=seed)

    def test_model_unload_is_on_by_default_and_must_be_boolean(self):
        self.assertIs(config_load(BASE / "configs" / "demo.json")["review_unload_model"], True)
        self.assertIs(self.load(review_unload_model=False)["review_unload_model"], False)
        for value in (0, 1, "true", None):
            with self.assertRaisesRegex(ExperimentError, "review_unload_model", msg=repr(value)):
                self.load(review_unload_model=value)

    def test_claude_cli_requires_both_null(self):
        cli = {"review_runner": "claude_cli", "review_reasoning": True}
        for sampling in ({}, {"review_temperature": None}, {"review_seed": None},
                         {"review_temperature": 0.8, "review_seed": None}):
            with self.assertRaisesRegex(ExperimentError, "review_temperature and review_seed to null"):
                self.load(**cli, **sampling)
        cfg = self.load(**cli, review_temperature=None, review_seed=None)
        self.assertEqual((cfg["review_temperature"], cfg["review_seed"]), (None, None))


class CallModelTests(unittest.TestCase):
    CFG = {"backend": "litellm", "review_model": "ollama_chat/coder:1b", "review_num_ctx": 32768,
           "review_reasoning": False, "max_output_tokens": 2000, "review_timeout_seconds": 60,
           "review_temperature": 0, "review_seed": 0}
    TASK = {"repo": "r", "problem_statement": "p", "files": [{"path": "a.py", "content": "x = 1\n"}]}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw = Path(tmp.name) / "review.raw.txt"

    def call(self, cfg: dict, litellm):
        with patch.dict(sys.modules, {"litellm": litellm}):
            return backends.call_model(cfg, "system", {"k": "v"}, self.raw)

    def test_temperature_and_seed_are_sent_and_recorded(self):
        litellm = fake_litellm(text='{"edits": []}')
        result = self.call(self.CFG | {"review_temperature": 0.8, "review_seed": 7}, litellm)
        sent = litellm.calls[0]
        self.assertEqual((sent["temperature"], sent["seed"]), (0.8, 7))
        self.assertEqual(litellm.lookups, [("coder:1b", "ollama_chat")])   # name without the provider prefix
        self.assertEqual(result["text"], '{"edits": []}')                    # returned unparsed
        usage = read_json(self.raw.with_suffix(".usage.json"))
        self.assertEqual(usage, result["usage"])
        self.assertEqual((usage["temperature"], usage["seed"], usage["finish_reason"]), (0.8, 7, "stop"))

    def test_zero_is_sent_not_dropped(self):
        litellm = fake_litellm()
        result = self.call(self.CFG, litellm)
        self.assertEqual({k: litellm.calls[0].get(k, "absent") for k in ("temperature", "seed")},
                         {"temperature": 0, "seed": 0})
        self.assertEqual((result["usage"]["temperature"], result["usage"]["seed"]), (0, 0))

    def test_null_keys_are_not_sent(self):
        litellm = fake_litellm()
        result = self.call(self.CFG | {"review_temperature": None, "review_seed": None}, litellm)
        self.assertNotIn("temperature", litellm.calls[0])
        self.assertNotIn("seed", litellm.calls[0])
        self.assertEqual(litellm.lookups, [])
        self.assertEqual((result["usage"]["temperature"], result["usage"]["seed"]), (None, None))
        litellm = fake_litellm()
        self.call(self.CFG | {"review_seed": None}, litellm)
        self.assertEqual(litellm.calls[0]["temperature"], 0)
        self.assertNotIn("seed", litellm.calls[0])

    def test_unsupported_key_is_refused_before_the_call(self):
        litellm = fake_litellm(supported=("temperature",))
        with self.assertRaisesRegex(ExperimentError, "does not accept seed; set review_seed to null"):
            self.call(self.CFG, litellm)
        self.assertEqual(litellm.calls, [])
        self.assertFalse(self.raw.exists())
        self.call(self.CFG | {"review_seed": None}, litellm)                # the null key lifts the refusal
        self.assertEqual(len(litellm.calls), 1)
        unknown = fake_litellm(supported=())
        unknown.get_supported_openai_params = lambda model, custom_llm_provider=None: None
        with self.assertRaisesRegex(ExperimentError, "review_temperature to null"):
            self.call(self.CFG, unknown)

    def test_local_model_is_unloaded_before_each_call(self):
        litellm, order = fake_litellm(), []
        real_completion = litellm.completion
        litellm.completion = lambda **kwargs: (order.append("call"), real_completion(**kwargs))[1]
        with patch.object(backends, "ollama_unload", side_effect=lambda cfg: order.append("unload")):
            first = self.call(self.CFG | {"review_unload_model": True}, litellm)
            self.call(self.CFG | {"review_unload_model": True}, litellm)
            self.assertEqual(order, ["unload", "call", "unload", "call"])
            self.assertIs(first["usage"]["model_unloaded_before_call"], True)
            kept = self.call(self.CFG | {"review_unload_model": False}, litellm)
            self.assertEqual(order.count("unload"), 2)
            self.assertIs(kept["usage"]["model_unloaded_before_call"], False)
            hosted = {k: v for k, v in self.CFG.items() if k != "review_num_ctx"} | {
                "review_model": "nvidia_nim/some/model", "review_unload_model": True}
            self.assertNotIn("model_unloaded_before_call", self.call(hosted, litellm)["usage"])
            self.assertEqual(order.count("unload"), 2)                        # nothing to unload at a hosted provider

    def test_failed_unload_stops_the_call(self):
        litellm = fake_litellm()
        with patch.object(backends, "ollama_unload", side_effect=ExperimentError("still loaded")):
            with self.assertRaisesRegex(ExperimentError, "still loaded"):
                self.call(self.CFG | {"review_unload_model": True}, litellm)
        self.assertEqual(litellm.calls, [])

    def test_unload_waits_until_the_server_no_longer_lists_the_model(self):
        cfg = {"review_model": "ollama_chat/coder:1b", "api_base": "http://localhost:11434"}
        listed = [["coder:1b", "other:1b"], ["coder:1b"], ["other:1b"]]     # successive /api/ps answers
        requests = []

        def urlopen(request, timeout):
            reply = unittest.mock.MagicMock()
            if isinstance(request, str):
                requests.append(request)
                names = listed.pop(0) if len(listed) > 1 else listed[0]
                reply.__enter__.return_value.read.return_value = json.dumps(
                    {"models": [{"name": n} for n in names]}).encode()
            else:
                requests.append((request.full_url, json.loads(request.data)))
            return reply

        with patch("ef_review.backends.urllib.request.urlopen", side_effect=urlopen), \
             patch("ef_review.backends.time.sleep") as sleep:
            backends.ollama_unload(cfg)
            self.assertEqual(requests, [("http://localhost:11434/api/generate", {"model": "coder:1b", "keep_alive": 0}),
                                        *["http://localhost:11434/api/ps"] * 3])
            self.assertEqual(sleep.call_count, 2)
            listed[:] = [["coder:1b"]]
            with self.assertRaisesRegex(ExperimentError, "still has coder:1b loaded"):
                backends.ollama_unload(cfg, wait_seconds=0)
        with patch("ef_review.backends.urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaisesRegex(ExperimentError, "Could not unload"):
                backends.ollama_unload(cfg)

    def test_malformed_answer_is_an_output_error_with_usage(self):
        usage = {"model": "fake", "finish_reason": "stop"}
        for text, code in (("{}", "missing_edits"), ("I cannot help", "invalid_json"), ("[1]", "not_an_object"),
                           ('{"edits": [{"search": "x", "replace": "y"}]}', "invalid_edit")):
            with patch.object(backends, "call_model", return_value={"text": text, "usage": usage}):
                reviewed = backends.review(self.CFG, self.TASK, [], self.raw)
            self.assertEqual(reviewed, {"findings": [], "patch": "", "edit_errors": [], "output_error": code,
                                        "usage": usage})
        with patch.object(backends, "call_model", return_value={"text": '{"edits": []}', "usage": usage}):
            self.assertNotIn("output_error", backends.review(self.CFG, self.TASK, [], self.raw))

    def test_answer_cut_off_at_the_output_limit_is_recorded_as_such(self):
        litellm = fake_litellm(text='{"edits": [{"file": "a.py", "search": "x = 1", "repl', finish_reason="length")
        with patch.dict(sys.modules, {"litellm": litellm}):
            reviewed = backends.review(self.CFG, self.TASK, [], self.raw)
        self.assertEqual((reviewed["output_error"], reviewed["usage"]["finish_reason"]), ("invalid_json", "length"))
        self.assertEqual(self.raw.read_text(), '{"edits": [{"file": "a.py", "search": "x = 1", "repl')

    def test_malformed_extractor_answer_keeps_usage(self):
        case = {"role": "adapt", "instance_id": "i", "public": self.TASK, "instance": {"patch": "diff"}}
        scored = {"status": "ok", "reason": "unresolved", "targets": [{"target_id": "t", "passed": False}]}
        with patch.dict(sys.modules, {"litellm": fake_litellm(text="no package")}):
            output = backends.extract(self.CFG, case, {"findings": [], "edit_errors": []}, scored, self.raw)
        self.assertEqual((output["package"], output["output_error"]), (None, "invalid_json"))
        self.assertEqual(output["usage"]["completion_tokens"], 2)


class ReasoningSwitchTests(unittest.TestCase):
    def test_reasoning_off_by_default_and_sent_explicitly(self):
        cfg = config_load(BASE / "configs" / "demo.json")
        self.assertIs(cfg["review_reasoning"], False)
        self.assertEqual(backends.reasoning_kwargs(cfg, "nvidia_nim"),
                         {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})

    def test_reasoning_can_be_enabled(self):
        self.assertEqual(backends.reasoning_kwargs({"review_reasoning": True}, "nvidia_nim"),
                         {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}})

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ExperimentError):
            backends.reasoning_kwargs({"review_reasoning": False}, "some_other_provider")

    def test_non_boolean_setting_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = read_json(BASE / "configs" / "demo.json") | {"review_reasoning": "no"}
            for key in ("demo_dataset", "demo_cache"):
                if key in cfg:
                    cfg[key] = str((BASE / "configs" / cfg[key]).resolve())
            path = Path(tmp) / "cfg.json"
            write_json(path, cfg)
            with self.assertRaises(ExperimentError):
                config_load(path)


if __name__ == "__main__":
    unittest.main()
