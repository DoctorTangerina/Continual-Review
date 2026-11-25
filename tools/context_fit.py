"""Find catalog cases whose review prompt does not fit the local model's context window.

Builds the exact review prompt of every case in a catalog CSV (system prompt plus
the canonical JSON payload, as ef_review.backends.review sends it), counts its tokens
with the model's own tokenizer and lists the cases that cannot fit into
<catalog>.excluded.json, next to the entries already there.

A case fits when

    prompt_tokens + memory_reserve + max_output_tokens <= review_num_ctx

prompt_tokens is the prompt with an empty memory. memory_reserve covers the
experiences the memory conditions add: at most memory_max_chars characters, taken
at a pessimistic 3 characters per token (real packages measure about 5).

Ollama does not reject a prompt above num_ctx: it keeps about half the window and
drops the rest, instructions included, so the model answers something unrelated.

The token count is exact for qwen2.5-coder: on the five prompts Ollama logged for
runs/swebench-01 it differs by a constant 13 tokens, the chat template. For another
model pass its Hugging Face tokenizer and re-measure --template-tokens.

Usage:
    python tools/context_fit.py --config configs/swebench.local.json \
        --catalog manifests/swebench_catalog.csv            # report only
    python tools/context_fit.py ... --write                 # update the excluded file
    python tools/context_fit.py ... --update                # also drop them from the catalog

--update sets role=exclude on those rows of the catalog CSV, which is what makes
`prepare` skip them (the excluded file alone is only a record). The role each case
had is kept in the excluded file, so the change can be undone by hand.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ef_review import swebench  # noqa: E402
from ef_review.core import canonical, config_load, jsonl, read_json, write_json  # noqa: E402

KIND = "context_window"  # marks the entries this tool owns, so a rerun replaces them
MEMORY_CHARS_PER_TOKEN = 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--tokenizer", default="Qwen/Qwen2.5-Coder-14B-Instruct",
                        help="Hugging Face repo of the review model's tokenizer")
    parser.add_argument("--template-tokens", type=int, default=13,
                        help="tokens the chat template adds around the system and user messages")
    parser.add_argument("--write", action="store_true", help="update <catalog>.excluded.json")
    parser.add_argument("--update", action="store_true",
                        help="also set role=exclude on those rows of the catalog CSV (implies --write)")
    args = parser.parse_args(argv)
    args.write = args.write or args.update

    from tokenizers import Tokenizer
    cfg = config_load(args.config)
    if not cfg.get("review_num_ctx"):
        parser.error("review_num_ctx is not set; this check only applies to local models")
    tokenizer = Tokenizer.from_pretrained(args.tokenizer)

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    memory_reserve = math.ceil(cfg["memory_max_chars"] / MEMORY_CHARS_PER_TOKEN)
    limit = cfg["review_num_ctx"] - cfg["max_output_tokens"] - memory_reserve
    fixed = args.template_tokens + count(swebench.SOLVE_SYSTEM)

    raw = args.catalog.read_bytes()
    bom, crlf = raw.startswith(b"\xef\xbb\xbf"), b"\r\n" in raw
    with args.catalog.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows, fields = list(reader), reader.fieldnames
    out = args.catalog.with_suffix(".excluded.json")
    previous = read_json(out) if out.is_file() else []
    kept = [e for e in previous if e.get("kind") != KIND]
    # A row this tool already excluded no longer shows the role it was assigned.
    assigned = {e["instance_id"]: e.get("role", "") for e in previous if e.get("kind") == KIND}
    dataset = {c["instance_id"]: c for c in jsonl(Path(cfg["dataset_path"]))}
    too_large = []
    for row in rows:
        iid = row["instance_id"]
        task = swebench.public_case(cfg, dataset[iid])
        tokens = fixed + count(canonical({"task": task, "experiences": []}))
        if tokens > limit:
            role = row.get("role", "").strip()
            if role in ("", "exclude"):
                role = assigned.get(iid, role)
            too_large.append({
                "instance_id": iid,
                "reason": (f"Review prompt does not fit review_num_ctx: {tokens} prompt tokens + "
                           f"{memory_reserve} memory reserve + {cfg['max_output_tokens']} output "
                           f"> {cfg['review_num_ctx']} ({cfg['review_model']})"),
                "kind": KIND, "prompt_tokens": tokens, "role": role})

    if args.write:
        write_json(out, kept + too_large)
    changed = []
    if args.update:
        drop = {e["instance_id"] for e in too_large}
        for row in rows:
            if row["instance_id"] in drop and row.get("role", "").strip() != "exclude":
                row["role"] = "exclude"
                changed.append(row["instance_id"])
        if changed:
            # Same encoding and line endings as found, so only the role cells differ.
            with args.catalog.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\r\n" if crlf else "\n")
                writer.writeheader()
                writer.writerows(rows)
    by_role: dict[str, int] = {}
    for entry in too_large:
        by_role[entry["role"] or "unassigned"] = by_role.get(entry["role"] or "unassigned", 0) + 1
    print(json.dumps({"cases": len(rows), "prompt_token_limit": limit, "too_large": len(too_large),
                      "too_large_by_role": by_role, "other_exclusions_kept": len(kept),
                      "written": str(out) if args.write else None,
                      "catalog_rows_set_to_exclude": len(changed) if args.update else None,
                      "instances": {e["instance_id"]: e["prompt_tokens"] for e in too_large}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
