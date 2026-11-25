"""Synthetic demonstration only. The auto-filled 'reviewer' is NOT a human."""
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))
from ef_review.core import CONDITIONS, ExperimentError, RunLock, build_manifest, config_load, read_json, write_json
from ef_review.engine import initialize, run_condition
from ef_review.reporting import export_report


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else BASE / "runs" / "demo"
    cfg = config_load(BASE / "configs" / "demo.json")
    if not root.exists():
        initialize(root, build_manifest(cfg, BASE / "examples" / "demo_assignments.csv"))
    if not read_json(root / "manifest.json")["synthetic"]:
        raise SystemExit("This helper must never be used with real experiment data")
    for condition in CONDITIONS:
        while True:
            try:
                with RunLock(root):
                    run_condition(root, condition)
                break
            except ExperimentError as exc:
                if "Human curation pending:" not in str(exc):
                    raise
                for path in root.glob("**/curation.json"):
                    choices = read_json(path)
                    for item in choices["items"]:
                        if item["decision"] == "pending":
                            item.update(decision="approve", reviewer="SYNTHETIC_DEMO_SCRIPT_NOT_HUMAN",
                                        reason="Synthetic wiring check, not human validation.", human_seconds=0)
                    write_json(path, choices)
    with RunLock(root):
        print(export_report(root))
    print("SYNTHETIC DEMONSTRATION ONLY — no LLM calls, Docker runs or scientific findings.")


if __name__ == "__main__":
    main()
