"""Offline tests for the SWE-bench adapter: no network, model, Docker or SWE-bench install."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))
from ef_review import backends, engine, swebench
from ef_review.core import (CONDITIONS, CONFIG_DEFAULTS, ExperimentError, ModelOutputError, build_manifest,
                            digest, read_json, write_json)
from ef_review.reporting import export_report

GOLD = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+y = 1
diff --git a/pkg/old.py b/pkg/moved.py
similarity index 100%
rename from pkg/old.py
rename to pkg/moved.py
"""


def instance(name: str, repo: str) -> dict:
    return {"instance_id": name, "repo": repo, "base_commit": "c" * 40, "problem_statement": f"Bug in {name}",
            "hints_text": "HINT_SECRET", "patch": GOLD.replace("x = 2", "x = 2  # GOLD_SECRET"),
            "test_patch": "TESTPATCH_SECRET", "FAIL_TO_PASS": ["test_secret_name"], "PASS_TO_PASS": [],
            "eval_script": "#!/bin/bash\n", "image": "img", "version": "1", "log_parser": "p", "eval_type": "pass_and_fail"}


class EditTests(unittest.TestCase):
    def test_gold_files_separates_existing_and_created(self):
        self.assertEqual(swebench.gold_files(GOLD), (["pkg/a.py", "pkg/old.py"], ["pkg/new.py"]))

    def test_generated_patch_applies_with_git(self):
        files = [{"path": "a.py", "content": "one\ntwo\x0cstill two\nthree"},      # form feed, no final newline
                 {"path": "crlf.txt", "content": "alpha\r\nbeta\r\n"}]
        edits = [{"file": "a.py", "search": "three", "replace": "3\nfour"},
                 {"file": "crlf.txt", "search": "alpha\nbeta", "replace": "alpha\ngamma"},
                 {"file": "sub/new.py", "search": "", "replace": "created = True\n"}]
        diff, errors = swebench.apply_edits(files, edits)
        self.assertFalse(errors)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for f in files:
                (repo / f["path"]).write_bytes(f["content"].encode())
            (repo / "change.diff").write_bytes(diff.encode())
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "change.diff"], cwd=repo, check=True)
            self.assertEqual((repo / "a.py").read_bytes(), b"one\ntwo\x0cstill two\n3\nfour")
            self.assertEqual((repo / "crlf.txt").read_bytes(), b"alpha\r\ngamma\r\n")
            self.assertEqual((repo / "sub" / "new.py").read_bytes(), b"created = True\n")

    def test_one_unplaceable_edit_voids_the_attempt(self):
        files = [{"path": "a.py", "content": "x = 1\nx = 1\ny = 2\n"}]
        ok = {"file": "a.py", "search": "y = 2", "replace": "y = 3"}
        for bad, error in (({"file": "a.py", "search": "x = 1", "replace": "z"}, "search_not_unique"),
                           ({"file": "a.py", "search": "missing", "replace": "z"}, "search_not_found"),
                           ({"file": "a.py", "search": "", "replace": "z"}, "empty_search"),
                           ({"file": "b.py", "search": "x", "replace": "z"}, "file_not_supplied")):
            diff, errors = swebench.apply_edits(files, [ok, bad])
            self.assertEqual((diff, errors), ("", [{"edit": 1, "error": error}]))

    def test_edit_paths_must_stay_inside_the_repository(self):
        for path in ("/etc/passwd", "../x.py", "C:/x.py"):
            with self.assertRaises(ExperimentError):
                swebench.edits_validate({"edits": [{"file": path, "search": "", "replace": ""}]})

    def test_malformed_answers_carry_a_stable_code(self):
        edit = {"file": "a.py", "search": "x", "replace": "y"}
        for data, code in (({}, "missing_edits"), ({"edits": "none"}, "missing_edits"),
                           ({"edits": ["a.py"]}, "invalid_edit"),
                           ({"edits": [edit | {"file": ""}]}, "invalid_edit"),
                           ({"edits": [{"search": "x", "replace": "y"}]}, "invalid_edit"),
                           ({"edits": [edit | {"file": "../a.py"}]}, "invalid_edit"),
                           ({"edits": [edit | {"replace": None}]}, "invalid_edit")):
            with self.assertRaises(ModelOutputError) as raised:
                swebench.edits_validate(data)
            self.assertEqual(raised.exception.code, code)
        for text, code in (("some prose", "invalid_json"), ('{"edits": [', "invalid_json"), ("[]", "not_an_object")):
            with self.assertRaises(ModelOutputError) as raised:
                backends.parse_object(text)
            self.assertEqual(raised.exception.code, code)


class VerdictTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event = Path(tmp.name) / "event"
        self.event.mkdir()
        self.cfg = {"swebench_python": ["python"], "swebench_eval_timeout_seconds": 10}
        raw = instance("proj__proj-1", "proj/proj")
        self.case = {"instance_id": raw["instance_id"], "instance": raw, "targets": swebench.targets(raw)}

    def grade(self, report=None, log=""):
        def harness(command, cwd, **kwargs):
            run_id = command[command.index("--run_id") + 1]
            folder = Path(cwd) / "logs" / "evaluation" / run_id / swebench.MODEL_NAME / "proj__proj-1"
            folder.mkdir(parents=True)
            (folder / "run_instance.log").write_text(log)
            if report is not None:
                (folder / "report.json").write_text(json.dumps({"proj__proj-1": report}))
            self.assertEqual(json.loads((Path(cwd) / "dataset.json").read_text())[0], self.case["instance"])
        with patch("ef_review.swebench.subprocess.run", side_effect=harness) as run:
            result = swebench.evaluate(self.cfg, self.case, {"patch": "diff", "edit_errors": []}, self.event)
        self.assertEqual(run.call_count, 1)
        return result["targets"][0]["passed"], result["reason"]

    def test_only_a_resolved_report_is_positive(self):
        self.assertEqual(self.grade({"resolved": True}), (True, "resolved"))
        self.assertEqual(self.grade({"resolved": False}), (False, "unresolved"))
        self.assertEqual(self.grade(log=">>>>> Patch Apply Failed:\nerror"), (False, "patch_apply_failed"))
        self.assertEqual(self.grade(log="Test timed out after 10 seconds."), (False, "tests_timed_out"))
        # every regrade archived the previous attempt's logs instead of reusing them
        self.assertEqual(len(list((self.event / "attempt_history").glob("swebench_eval-*"))), 3)

    def test_technical_failures_are_not_verdicts(self):
        with self.assertRaisesRegex(ExperimentError, "infrastructure"):
            self.grade({"resolved": False, "infra_failure": True})
        with self.assertRaisesRegex(ExperimentError, "no verdict"):
            self.grade(log="Error response from daemon")

    def test_no_patch_is_negative_without_running_the_harness(self):
        with patch("ef_review.swebench.subprocess.run") as run:
            empty = swebench.evaluate(self.cfg, self.case, {"patch": "", "edit_errors": []}, self.event)
            void = swebench.evaluate(self.cfg, self.case, {"patch": "", "edit_errors": [{"edit": 0}]}, self.event)
            malformed = swebench.evaluate(self.cfg, self.case, {"patch": "", "edit_errors": [],
                                                                "output_error": "invalid_json"}, self.event)
        run.assert_not_called()
        self.assertEqual([(r["reason"], r["targets"][0]["passed"], r["repair_invoked"]) for r in (empty, void, malformed)],
                         [("no_patch_proposed", False, False), ("edits_not_applicable", False, False),
                          ("invalid_model_output", False, False)])
        # a malformed answer starts no harness run, so it is not charged to the call budget
        self.assertFalse(backends.evaluation_invoked({"backend": "litellm"}, {"patch": "", "output_error": "invalid_json"}))


class FlowTests(unittest.TestCase):
    """The four conditions over a frozen three-task manifest, with model and harness replaced."""
    SOLVER = "oneshot"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)
        tasks = [instance("warm__warm-1", "warm/warm"), instance("adapt__adapt-1", "adapt/adapt"),
                 instance("held__held-1", "held/held")]
        dataset = work / "tasks.jsonl"
        dataset.write_text("\n".join(json.dumps(t) for t in tasks), encoding="utf-8")
        for t in tasks:  # pre-seeded oracle files: fetch_context must not touch the network
            write_json(work / "cache" / "context" / f"{t['instance_id']}.json", {"base_commit": t["base_commit"],
                       "files": [{"path": "pkg/a.py", "content": f"x = 1  # {t['instance_id']}\n"}]})
        self.assignments = assignments = work / "assignments.csv"
        assignments.write_text("instance_id,role,block,family\nwarm__warm-1,warmup,,f\n"
                               "adapt__adapt-1,adapt,1,f\nheld__held-1,eval,,f\n", encoding="utf-8")
        self.cfg = CONFIG_DEFAULTS | {"backend": "litellm", "benchmark": "swebench", "review_model": "fake/model",
                                      "solver": self.SOLVER,
                                      "dataset_path": str(dataset), "swebench_cache": str(work / "cache"),
                                      "swebench_python": ["python"]}
        self.root = work / "run"
        self.payloads = []
        self.malformed = {}  # (system prompt, repo) -> raw answer returned instead of the well-formed one
        for target, value in (("ef_review.swebench.identity", {"kind": "swebench", "commit": "h", "tasks_commit": "t"}),
                              ("ef_review.backends.preflight_live", None)):
            started = patch(target, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        engine.initialize(self.root, build_manifest(self.cfg, assignments))

    def model(self, cfg, system, payload, raw_path):
        self.payloads.append((system, payload))
        if system == swebench.PACKAGE_SYSTEM:
            data = {"episode": "e", "rule": "check x", "scope": "s", "exceptions": "none", "goal": "g",
                    "question": "q", "metric": "m", "tags": ["bug"]}
        else:  # resolves a task only when an experience was retrieved
            data = {"edits": [{"file": "pkg/a.py", "search": "x = 1", "replace": "x = 2"}] if payload["experiences"] else []}
        # call_model returns the answer unparsed
        text = self.malformed.get((system, payload["task"]["repo"]), json.dumps(data))
        return {"text": text, "usage": {"model": "fake"}}

    def run_all(self):
        """Run the four conditions, approving every curation item, with the model and harness replaced."""
        with patch("ef_review.backends.call_model", side_effect=self.model), \
             patch("ef_review.swebench.subprocess.run", side_effect=self.harness) as harness_process:
            for condition in CONDITIONS:
                for _ in range(5):
                    try:
                        engine.run_condition(self.root, condition, live=True)
                        break
                    except ExperimentError as exc:
                        self.assertIn("Human curation pending", str(exc))
                        for path in self.root.glob("**/curation.json"):
                            choices = read_json(path)
                            for item in choices["items"]:
                                item.update(decision="approve", reviewer="OFFLINE_TEST_ONLY", reason="test", human_seconds=1)
                            write_json(path, choices)
                else:
                    self.fail(f"{condition} did not complete")
        return harness_process

    def event(self, area: str, instance_id: str) -> Path:
        return self.root / area / digest(instance_id)[:16]

    def harness(self, command, cwd, **kwargs):
        """Stands in for the SWE-bench process: reports every submitted patch as resolved."""
        iid = command[command.index("--instance_ids") + 1]
        folder = Path(cwd) / "logs" / "evaluation" / command[command.index("--run_id") + 1] / swebench.MODEL_NAME / iid
        folder.mkdir(parents=True)
        self.assertIn("+x = 2", json.loads((Path(cwd) / "preds.jsonl").read_text())["model_patch"])
        (folder / "report.json").write_text(json.dumps({iid: {"resolved": True}}))

    def test_conditions_complete_and_answer_keys_stay_private(self):
        harness_process = self.run_all()
        solver = [p for s, p in self.payloads if s == swebench.SOLVE_SYSTEM]
        extractor = [p for s, p in self.payloads if s == swebench.PACKAGE_SYSTEM]
        for secret in ("GOLD_SECRET", "HINT_SECRET", "TESTPATCH_SECRET", "test_secret_name"):
            self.assertNotIn(secret, json.dumps(solver))
        self.assertNotIn("held__held-1", json.dumps(extractor))       # reserved task never feeds memory
        self.assertIn("GOLD_SECRET", json.dumps(extractor))            # adaptation feedback is the reference fix
        export_report(self.root)
        rows = {(r["condition"], r["checkpoint"]): r for r in read_json(self.root / "reports" / "summary.json")["checkpoints"]}
        self.assertEqual(rows[("none", 1)]["passed_targets"], 0)       # no memory: no edits, harness never asked
        self.assertEqual(rows[("static", 0)]["passed_targets"], 1)
        with_edits = sum(bool(p["experiences"]) for p in solver)
        self.assertEqual(harness_process.call_count, with_edits)       # one harness run per proposed patch
        self.assertEqual(read_json(self.root / "budget.json")["reserved_calls"],
                         len(solver) + len(extractor) + with_edits)
        self.assertIn("SWE-bench harness", (self.root / "reports" / "report.md").read_text())
        summary = read_json(self.root / "reports" / "summary.json")
        self.assertEqual(summary["invalid_model_output"]["reviewer_answers_whole_run"], 0)
        self.assertEqual({r["invalid_model_output"] for r in summary["checkpoints"]}, {0})

    def test_malformed_solver_answer_is_an_unresolved_case_and_the_run_continues(self):
        # The reserved task's answer is "{}" in every condition, as when pinned sampling repeats it.
        self.malformed[(swebench.SOLVE_SYSTEM, "held/held")] = "{}"
        harness_process = self.run_all()
        for condition in CONDITIONS:
            for checkpoint in (0, 1):
                folder = self.event(f"conditions/{condition}/checkpoint-{checkpoint}", "held__held-1")
                review = read_json(folder / "review.json")["value"]
                self.assertEqual((review["output_error"], review["patch"], review["findings"]), ("missing_edits", "", []))
                self.assertEqual(review["usage"], {"model": "fake"})      # the call's usage is kept
                scored = read_json(folder / "evaluation.json")["value"]
                self.assertEqual((scored["reason"], scored["targets"][0]["passed"], scored["repair_invoked"]),
                                 ("invalid_model_output", False, False))
                self.assertEqual(read_json(folder / "case.json")["reason"], "invalid_model_output")
                self.assertFalse((folder / "review.failed.json").exists())
                self.assertFalse((folder / "swebench_eval").exists())      # harness never started
        # only the adaptation case of the two incremental conditions reached the harness
        self.assertEqual(harness_process.call_count, 2)
        charged = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(read_json(self.root / "budget.json")["reserved_calls"],
                         sum(1 for entry in charged if entry.get("action") == "attempt" and entry["charged"]))
        self.assertFalse(any(entry.get("action") == "failed" for entry in charged))
        export_report(self.root)
        summary = read_json(self.root / "reports" / "summary.json")
        self.assertEqual({(r["invalid_model_output"], r["passed_targets"]) for r in summary["checkpoints"]}, {(1, 0)})
        self.assertEqual(summary["invalid_model_output"]["reserved_evaluation_by_checkpoint"]["static"], {"0": 1, "1": 1})
        self.assertEqual(summary["invalid_model_output"]["reviewer_answers_whole_run"], 8)
        self.assertIn("invalid_model_output", (self.root / "reports" / "report.md").read_text())

    def test_malformed_extractor_answer_leaves_the_case_without_a_package(self):
        self.malformed[(swebench.PACKAGE_SYSTEM, "adapt/adapt")] = '{"episode": "cut off at the output li'
        self.run_all()
        for condition in ("automatic", "curated"):
            area = f"conditions/{condition}/adapt-1"
            package = read_json(self.event(area, "adapt__adapt-1") / "package.json")["value"]
            self.assertEqual((package["package"], package["output_error"], package["usage"]),
                             (None, "invalid_json", {"model": "fake"}))
            self.assertEqual(read_json(self.root / area / "candidates.json"), [])
            # the memory after the block is still the approved warmup seed
            self.assertEqual(read_json(self.root / "conditions" / condition / "state.json")["memory"],
                             read_json(self.root / "seed.json"))
        self.assertEqual(len(read_json(self.root / "warmup" / "candidates.json")), 1)
        export_report(self.root)
        counts = read_json(self.root / "reports" / "summary.json")["invalid_model_output"]
        self.assertEqual((counts["extractor_answers_whole_run"], counts["reviewer_answers_whole_run"]), (2, 0))

    def test_extractor_is_told_the_attempt_was_malformed(self):
        self.malformed[(swebench.SOLVE_SYSTEM, "warm/warm")] = "not json"
        self.run_all()
        attempts = [p["attempt"] for s, p in self.payloads if s == swebench.PACKAGE_SYSTEM]
        warm = [p["attempt"] for s, p in self.payloads
                if s == swebench.PACKAGE_SYSTEM and p["task"]["repo"] == "warm/warm"]
        self.assertEqual(warm, [{"edits": [], "edit_errors": [], "output_error": "invalid_json"}])
        self.assertTrue(all("output_error" in attempt for attempt in attempts))
        self.assertEqual([a["output_error"] for a in attempts if a not in warm], [None, None])

    def test_manifest_records_the_ollama_build_and_a_changed_model_is_refused(self):
        cfg = self.cfg | {"review_model": "ollama_chat/coder:1b", "review_num_ctx": 32768}
        seen = {"version": "0.40.16", "model_digest": "a" * 64}
        with patch.object(backends, "ollama_identity", return_value=seen):
            manifest = build_manifest(cfg, self.assignments)
        self.assertEqual(manifest["runtime"]["ollama"], seen)
        self.assertIsNone(read_json(self.root / "manifest.json")["runtime"]["ollama"])   # not a local model
        with patch.object(backends, "ollama_identity", side_effect=ExperimentError("Ollama server not reachable")):
            with self.assertRaisesRegex(ExperimentError, "not reachable"):   # never recorded as null
                build_manifest(cfg, self.assignments)


AGENT_PATCH = "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


class AgentViewTests(unittest.TestCase):
    CFG = {"solver": "agent", "agent_oracle_paths": False, "swebench_cache": "unused"}

    def test_agent_gets_the_issue_only_and_nothing_is_downloaded(self):
        task = instance("a__a-1", "a/a")
        with patch.object(swebench, "fetch_context", side_effect=AssertionError("no oracle download")):
            self.assertEqual(swebench.public_case(self.CFG, task), {"repo": "a/a", "problem_statement": "Bug in a__a-1"})
            with_paths = swebench.public_case(self.CFG | {"agent_oracle_paths": True}, task)
        self.assertEqual(with_paths["files"], [{"path": "pkg/a.py"}, {"path": "pkg/old.py"}])   # paths, never text
        self.assertEqual(swebench.sandbox_spec({"instance": task}), {"image": "img", "base_commit": "c" * 40})

    def test_catalog_for_the_agent_measures_no_oracle_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "tasks.jsonl"
            dataset.write_text(json.dumps(instance("a__a-1", "a/a")), encoding="utf-8")
            with patch.object(swebench, "fetch_context", side_effect=AssertionError("no oracle download")):
                rows, excluded = swebench.catalog(self.CFG | {"backend": "demo", "dataset_path": str(dataset)})
        self.assertEqual(excluded, [])
        self.assertEqual((rows[0]["instance_id"], rows[0]["context_files"], rows[0]["context_chars"]), ("a__a-1", "", ""))

    def test_extractor_sees_patch_exit_and_commands_within_a_fixed_size(self):
        case = {"public": {"repo": "a/a", "problem_statement": "p"}, "instance": {"patch": "REFERENCE"}}
        scored = {"status": "ok", "reason": "unresolved", "targets": [{"passed": False}]}
        reviewed = {"findings": [{"file": "pkg/a.py"}], "patch": AGENT_PATCH, "edit_errors": [],
                    "agent": {"exit": "Submitted", "steps": 3, "commands": ["ls", "sed -i s/1/2/ pkg/a.py", "submit"]}}
        payload = swebench.extraction_payload(case, reviewed, scored)
        self.assertEqual(payload["attempt"], {"patch": AGENT_PATCH, "exit": "Submitted", "steps": 3,
                                              "commands": ["ls", "sed -i s/1/2/ pkg/a.py", "submit"], "output_error": None})
        self.assertEqual(payload["task"]["files"], [])
        huge = reviewed | {"patch": "+" * 50000, "agent": reviewed["agent"] | {"commands": ["x" * 3000] * 200}}
        attempt = swebench.extraction_payload(case, huge, scored)["attempt"]
        self.assertLess(len(json.dumps(attempt)), swebench.AGENT_PATCH_MAX_CHARS + swebench.AGENT_COMMANDS_MAX_CHARS + 1500)
        self.assertIn("characters removed", attempt["patch"])
        self.assertIn("more commands removed", attempt["commands"][-1])


class AgentFlowTests(unittest.TestCase):
    """The four conditions with the agent solver; the loop itself is replaced (see test_agent.py)."""
    SOLVER = "agent"
    setUp, run_all, event, harness, model = (FlowTests.setUp, FlowTests.run_all, FlowTests.event,
                                             FlowTests.harness, FlowTests.model)

    def attempt(self, cfg, task, memory, sandbox, raw_path):
        """Stands in for agent.run_attempt: leaves a patch only when an experience was retrieved."""
        self.attempts.append({"task": task, "memory": memory, "sandbox": sandbox})
        patch_text = AGENT_PATCH if memory else ""
        return {"findings": [{"file": "pkg/a.py"}] if memory else [], "patch": patch_text, "edit_errors": [],
                "usage": {"model": "fake", "steps": 2 if memory else 30, "prompt_tokens": 500, "completion_tokens": 40},
                "agent": {"exit": "Submitted" if memory else "LimitsExceeded", "steps": 2 if memory else 30,
                          "commands": ["ls", "SECRET_FREE_COMMAND"], "image": {"remotes": 0, "commits_after_base": 0}}}

    def test_conditions_complete_with_the_agent(self):
        self.attempts = []
        manifest = read_json(self.root / "manifest.json")
        self.assertRegex(manifest["runtime"]["mini_swe_agent"], r"^\d+\.\d+")
        self.assertTrue(all("files" not in c["public"] for c in manifest["cases"]))
        with patch("ef_review.agent.run_attempt", side_effect=self.attempt):
            harness_process = self.run_all()
        for secret in ("GOLD_SECRET", "HINT_SECRET", "TESTPATCH_SECRET", "test_secret_name"):
            self.assertNotIn(secret, json.dumps(self.attempts))
        self.assertEqual({json.dumps(a["sandbox"], sort_keys=True) for a in self.attempts},
                         {json.dumps({"image": "img", "base_commit": "c" * 40}, sort_keys=True)})
        extractor = [p for s, p in self.payloads if s == swebench.PACKAGE_SYSTEM]
        self.assertTrue(extractor and all(set(p["attempt"]) == {"patch", "exit", "steps", "commands", "output_error"}
                                          for p in extractor))
        self.assertNotIn("held__held-1", json.dumps(extractor))
        with_patch = sum(bool(a["memory"]) for a in self.attempts)
        self.assertEqual(harness_process.call_count, with_patch)      # an empty diff never starts the harness
        static = read_json(self.event("conditions/static/checkpoint-0", "held__held-1") / "case.json")
        self.assertEqual((static["agent_exit"], static["steps"], static["reason"]), ("Submitted", 2, "resolved"))
        none = read_json(self.event("conditions/none/checkpoint-1", "held__held-1") / "case.json")
        self.assertEqual((none["agent_exit"], none["steps"], none["reason"]), ("LimitsExceeded", 30, "no_patch_proposed"))
        export_report(self.root)
        rows = {(r["condition"], r["checkpoint"]): r for r in read_json(self.root / "reports" / "summary.json")["checkpoints"]}
        self.assertEqual((rows[("none", 0)]["agent_exits"], rows[("none", 0)]["median_steps"]), ("LimitsExceeded:1", 30))
        self.assertEqual((rows[("static", 1)]["agent_exits"], rows[("static", 1)]["solver_prompt_tokens"]), ("Submitted:1", 500))
        self.assertIn("| none | 0 | LimitsExceeded:1 | 30 |", (self.root / "reports" / "report.md").read_text())

    def test_a_different_patch_for_the_same_files_is_not_served_from_the_grading_cache(self):
        self.attempts = []
        with patch("ef_review.agent.run_attempt", side_effect=self.attempt):
            self.run_all()
        event = self.event("conditions/static/checkpoint-0", "held__held-1")
        graded = read_json(event / "evaluation.json")
        review = read_json(event / "review.json")
        review["value"]["patch"] = AGENT_PATCH.replace("x = 2", "x = 3")     # same file list, other content
        write_json(event / "review.json", review)
        write_json(self.root / "conditions" / "static" / "state.json", {"cursor": 0, "memory": read_json(self.root / "seed.json"),
                                                                        "complete": False})
        with self.assertRaisesRegex(ExperimentError, "Cached stage inputs changed"):
            engine.run_condition(self.root, "static", live=True)
        self.assertEqual(read_json(event / "evaluation.json"), graded)

    def test_changed_prompt_templates_are_refused_on_resume(self):
        with patch("ef_review.agent.templates", return_value={"system": "another text"}):
            with self.assertRaisesRegex(ExperimentError, "Prompt texts changed"):
                engine.load_run(self.root)


class PreflightTests(unittest.TestCase):
    CFG = {"review_model": "ollama_chat/coder:1b", "repair_enabled": False}

    def check(self, current: dict, recorded: dict | None):
        with patch.object(backends, "ollama_identity", return_value=current) as identity, \
             patch("ef_review.backends.importlib.util.find_spec", return_value=object()):
            backends.preflight_live(self.CFG, {"ollama": recorded})
        return identity

    def test_changed_ollama_model_digest_is_refused(self):
        recorded = {"version": "0.40.16", "model_digest": "a" * 64}
        self.check({"version": "0.41.0", "model_digest": "a" * 64}, recorded).assert_called_once()
        with self.assertRaisesRegex(ExperimentError, "digest differs"):
            self.check({"version": "0.40.16", "model_digest": "b" * 64}, recorded)
        self.check({"version": "x", "model_digest": "x"}, None).assert_not_called()      # hosted model: no query

    def test_ollama_identity_reads_version_and_digest_without_generating(self):
        replies = {"/api/version": {"version": "0.40.16"},
                   "/api/tags": {"models": [{"name": "coder:1b", "digest": "a" * 64},
                                            {"name": "other:latest", "digest": "b" * 64}]}}
        asked = []

        def urlopen(url, timeout):
            asked.append(url)
            reply = unittest.mock.MagicMock()
            reply.__enter__.return_value.read.return_value = json.dumps(replies[url.split("11434", 1)[1]]).encode()
            return reply

        cfg = {"review_model": "ollama_chat/coder:1b", "api_base": "http://localhost:11434/"}
        with patch("ef_review.backends.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(backends.ollama_identity(cfg), {"version": "0.40.16", "model_digest": "a" * 64})
            self.assertEqual(backends.ollama_identity(cfg | {"review_model": "ollama/other"})["model_digest"], "b" * 64)
            with self.assertRaisesRegex(ExperimentError, "not pulled"):
                backends.ollama_identity(cfg | {"review_model": "ollama_chat/missing:1b"})
        self.assertEqual(set(asked), {"http://localhost:11434/api/version", "http://localhost:11434/api/tags"})
        with patch("ef_review.backends.urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaisesRegex(ExperimentError, "not reachable"):
                backends.ollama_identity(cfg)


if __name__ == "__main__":
    unittest.main()
