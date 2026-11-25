#!/usr/bin/env bash
# Model trial: the first 4 warmup cases of runs/swebench-01, one at a time, with another
# local model; then a side-by-side with what qwen2.5-coder:7b did on the same cases.
#
#   ./model_trial.sh                     qwen2.5-coder:14b, new run in runs/trial-<model>-<date-time>
#   ./model_trial.sh runs/NAME           new run with that name, or resume it
#   ./model_trial.sh --summary runs/NAME only print the comparison of a finished trial
#
# Overrides (environment):
#   MODEL         Ollama model (default qwen2.5-coder:14b)
#   NUM_CTX       review_num_ctx (default: the one in BASE_CONFIG)
#   REPEAT_LIMIT  agent_repeat_limit (default 3; 0 turns the repeated-command stop off)
#   BASE_CONFIG   configuration the trial copies (default configs/swebench.local.json)
#   BASELINE      run to compare with (default runs/swebench-01)
#
# Only the warmup runs: the run stops at the human curation of the warmup seed, which is
# the expected end here. The adapt and eval rows of manifests/trial_warmup4.csv exist only
# because `prepare` requires them; they are never attempted.
set -euo pipefail
cd "$(dirname "$0")"

MODEL=${MODEL:-qwen2.5-coder:14b}
REPEAT_LIMIT=${REPEAT_LIMIT:-3}
BASE_CONFIG=${BASE_CONFIG:-configs/swebench.local.json}
BASELINE=${BASELINE:-runs/swebench-01}
ASSIGNMENTS=manifests/trial_warmup4.csv
PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
API=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("api_base","http://localhost:11434"))' "$BASE_CONFIG")
NUM_CTX=${NUM_CTX:-$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["review_num_ctx"])' "$BASE_CONFIG")}
SAFE=$(printf '%s' "$MODEL" | tr ':/' '--')

summary() {
    "$PYTHON" - "$1" "$BASELINE" <<'EOF'
import json, sys
from pathlib import Path
sys.path.insert(0, "src")
from ef_review.agent import repeating
from ef_review.core import digest

trial, baseline = Path(sys.argv[1]), Path(sys.argv[2])
cases = [c for c in json.loads((trial / "manifest.json").read_text())["cases"] if c["role"] == "warmup"]
model = json.loads((trial / "manifest.json").read_text())["config"]["review_model"]

def row(run, iid):
    folder = run / "warmup" / digest(iid)[:16]
    try:
        review = json.loads((folder / "review.json").read_text())
    except FileNotFoundError:
        return None
    value, seconds = review["value"], review["elapsed_seconds"]
    try:
        reason = json.loads((folder / "evaluation.json").read_text())["value"].get("reason")
    except FileNotFoundError:
        reason = "-"
    cmds = value["agent"]["commands"]
    stop = next((i for i in range(1, len(cmds) + 1) if repeating(cmds[:i], 3)), None)
    return {"exit": value["agent"]["exit"], "steps": value["agent"]["steps"], "seconds": seconds,
            "patch": "yes" if value["patch"] else "no", "result": reason,
            "tokens": value["usage"]["prompt_tokens"], "would_stop": stop,
            "workers": value["usage"].get("parallel_cases")}

print(f"\ntrial: {trial}  ({model})   baseline: {baseline}")
head = f"{'case':22} {'run':8} {'exit':18} {'steps':>5} {'seconds':>7} {'prompt tok':>10} {'patch':>5}  result"
print(head + "\n" + "-" * len(head))
totals = {"trial": [0, 0, 0], "baseline": [0, 0, 0]}
for c in cases:
    for name, run in (("trial", trial), ("baseline", baseline)):
        r = row(run, c["instance_id"])
        if r is None:
            print(f"{c['instance_id']:22} {name:8} (not run)")
            continue
        note = ""
        if name == "baseline":
            note = f"   [{r['workers']} workers" + (f"; repeat stop would end it at step {r['would_stop']}]" if r["would_stop"] else "]")
        print(f"{c['instance_id']:22} {name:8} {r['exit']:18} {r['steps']:>5} {r['seconds']:>7.0f} {r['tokens']:>10} "
              f"{r['patch']:>5}  {r['result']}{note}")
        t = totals[name]
        t[0] += r["seconds"]; t[1] += r["patch"] == "yes"; t[2] += r["result"] == "resolved"
    print()
for name, (seconds, patches, resolved) in totals.items():
    print(f"{name:8}: review time {seconds:6.0f} s, patches {patches}/{len(cases)}, resolved {resolved}/{len(cases)}")
print("The baseline ran 4 cases at a time on a shared GPU, so its per-case seconds are not\n"
      "directly comparable; compare steps, prompt tokens, patches and results first.")
EOF
}

if [ "${1:-}" = "--summary" ]; then
    [ -n "${2:-}" ] || { echo "trial: usage: $0 --summary runs/NAME" >&2; exit 2; }
    summary "$2"
    exit 0
fi
RUN_DIR=${1:-runs/trial-$SAFE-$(date +%m%d-%H%M%S)}

# --- checks -------------------------------------------------------------------------
docker info >/dev/null 2>&1 || { echo "trial: cannot reach the docker daemon" >&2; exit 1; }
if pgrep -f "python[^ ]* experiment.py run" >/dev/null; then
    echo "trial: another 'experiment.py run' is in progress; it would share the GPU and distort the timings:" >&2
    pgrep -af "python[^ ]* experiment.py run" >&2
    exit 1
fi
curl -sf "$API/api/tags" >/dev/null || { echo "trial: no Ollama server at $API" >&2; exit 1; }
if ! curl -sf "$API/api/tags" | "$PYTHON" -c 'import json,sys; m=sys.argv[1]; names={x["name"] for x in json.load(sys.stdin)["models"]}; sys.exit(0 if m in names or m+":latest" in names else 1)' "$MODEL"; then
    echo "trial: $MODEL is not pulled yet (ollama pull $MODEL)" >&2
    exit 1
fi

# The server reserves one context window per OLLAMA_NUM_PARALLEL slot. Load the model at the
# trial's context and check it fits the GPU entirely: a partial CPU offload would make the
# timings meaningless. A server started for --workers 4 typically fails here with 14B.
echo "trial: loading $MODEL at num_ctx $NUM_CTX to check it fits the GPU"
curl -sf "$API/api/generate" -d "{\"model\":\"$MODEL\",\"prompt\":\"\",\"keep_alive\":\"60s\",\"options\":{\"num_ctx\":$NUM_CTX}}" >/dev/null
placement=$(ollama ps | awk -v m="$MODEL" '$1==m {for (i=1;i<=NF;i++) if ($i ~ /GPU|CPU/) {print $(i-1), $i; exit}}')
curl -sf "$API/api/generate" -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" >/dev/null || true
echo "trial: placement: ${placement:-unknown}"
if [ "$placement" != "100% GPU" ]; then
    echo "trial: $MODEL does not fit the GPU at num_ctx $NUM_CTX with this server." >&2
    echo "       Restart Ollama with one slot, e.g.: sudo systemctl edit ollama  (OLLAMA_NUM_PARALLEL=1)," >&2
    echo "       or lower NUM_CTX (and expect more ContextExhausted endings)." >&2
    exit 1
fi

# --- configuration and run ----------------------------------------------------------
CONFIG=configs/trial-$SAFE.local.json
"$PYTHON" - "$BASE_CONFIG" "$CONFIG" "$MODEL" "$NUM_CTX" "$REPEAT_LIMIT" <<'EOF'
import json, sys
base, out, model, num_ctx, repeat = sys.argv[1:]
cfg = json.load(open(base))
cfg |= {"review_model": f"ollama_chat/{model}", "review_num_ctx": int(num_ctx), "agent_repeat_limit": int(repeat)}
json.dump(cfg, open(out, "w"), indent=2)
EOF

if [ -d "$RUN_DIR" ]; then
    echo "trial: resuming $RUN_DIR"
else
    echo "trial: preparing $RUN_DIR ($CONFIG)"
    "$PYTHON" experiment.py --config "$CONFIG" prepare --assignments "$ASSIGNMENTS" --run-dir "$RUN_DIR"
fi

echo "trial: running the 4 warmup cases with $MODEL, one at a time (started $(date +%T))"
log=$(mktemp)
trap 'rm -f "$log"' EXIT
status=0
"$PYTHON" experiment.py run --run-dir "$RUN_DIR" --condition none --live --workers 1 2>&1 | tee "$log" || status=$?
echo "trial: finished $(date +%T), ${SECONDS}s"

if grep -q "Human curation pending" "$log"; then
    status=0   # the warmup is done; curation is not part of the trial
fi
[ "$status" -eq 0 ] && summary "$RUN_DIR"
exit "$status"
