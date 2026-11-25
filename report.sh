#!/usr/bin/env bash
# Generate the report of a finished run (all four conditions complete).
#
#   ./report.sh                         the last run started by run.sh
#   ./report.sh runs/NAME               that run
#   ./report.sh runs/NAME --annotations FILE
#                                       include the manual annotations (README section 6)
#
# If RUN/reports/annotations.json exists it is used without --annotations.
# Outputs in RUN/reports/: report.md (printed at the end), summary.json, checkpoints.csv,
# family_matrix.json and annotations.template.json.
# This reveals the reserved evaluations; experiment.py refuses before every condition is complete.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [ -t 1 ]; then
    RED=$'\033[31m' GREEN=$'\033[32m' BOLD=$'\033[1m' RESET=$'\033[0m'
else
    RED="" GREEN="" BOLD="" RESET=""
fi
usage() { printf '%sreport: %s (see --help)%s\n' "$RED" "$*" "$RESET" >&2; exit 2; }
# error REASON [HINT]: print it in red and stop.
error() {
    printf '\n%s%s✗ report failed%s\n%s  Reason: %s%s\n' "$RED" "$BOLD" "$RESET" "$RED" "$1" "$RESET" >&2
    [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/  /' >&2
    exit 1
}

RUN_DIR=""
ANNOTATIONS=""
while [ $# -gt 0 ]; do
    case $1 in
        --annotations) [ $# -ge 2 ] || usage "--annotations needs a file"; ANNOTATIONS=$2; shift ;;
        -h|--help)     sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        -*)            usage "unknown option $1" ;;
        *)             [ -z "$RUN_DIR" ] || usage "only one run directory"; RUN_DIR=${1%/} ;;
    esac
    shift
done

if [ -z "$RUN_DIR" ]; then
    [ -f runs/.last-run ] || error "no run given and run.sh has not started one" "Usage: ./report.sh runs/NAME"
    RUN_DIR=$(<runs/.last-run)
fi
[ -f "$RUN_DIR/manifest.json" ] || error "$RUN_DIR is not a prepared run (no manifest.json)"

PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3

# Every condition must be complete; say which are not, instead of only the engine's refusal.
missing=$("$PYTHON" -c 'import json,sys,pathlib
for c in ("none", "static", "automatic", "curated"):
    p = pathlib.Path(sys.argv[1], "conditions", c, "state.json")
    if not (p.exists() and json.loads(p.read_text()).get("complete")): print(c)' "$RUN_DIR" | paste -sd, | sed 's/,/, /g')
[ -z "$missing" ] || error "conditions not complete yet: $missing" "Continue the run first: ./run.sh $RUN_DIR"

if [ -z "$ANNOTATIONS" ] && [ -f "$RUN_DIR/reports/annotations.json" ]; then
    ANNOTATIONS=$RUN_DIR/reports/annotations.json
fi
if [ -n "$ANNOTATIONS" ]; then
    [ -f "$ANNOTATIONS" ] || error "annotations file $ANNOTATIONS does not exist"
    printf '%s==> Report of %s, with annotations %s%s\n' "$BOLD" "$RUN_DIR" "$ANNOTATIONS" "$RESET"
    ARGS=(--annotations "$ANNOTATIONS")
else
    printf '%s==> Report of %s%s\n' "$BOLD" "$RUN_DIR" "$RESET"
    ARGS=()
fi

LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT
status=0
"$PYTHON" experiment.py report --run-dir "$RUN_DIR" "${ARGS[@]}" >"$LOG" 2>&1 || status=$?
if [ "$status" -ne 0 ]; then
    cat "$LOG" >&2
    reason=$(grep -v '^[[:space:]]*$' "$LOG" | tail -n 1)
    hint=""
    case $reason in
        *"Run locked"*) hint="Another experiment.py may be using this run (pgrep -af experiment.py). If none is, remove $RUN_DIR/.run.lock" ;;
        *nnotation*)    hint="Check $ANNOTATIONS against $RUN_DIR/reports/annotations.template.json (README section 6)" ;;
        *"changed after prep"*|*"source changed"*|*"texts changed"*)
                        hint="The code or environment changed since this run was prepared" ;;
    esac
    error "${reason:-exited with status $status and no message}" "$hint"
fi

REPORTS=$RUN_DIR/reports
printf '\n'
cat "$REPORTS/report.md"
printf '\n%s%s✓ Report written to %s/%s\n' "$GREEN" "$BOLD" "$REPORTS" "$RESET"
for f in report.md summary.json checkpoints.csv family_matrix.json annotations.template.json; do
    [ -f "$REPORTS/$f" ] && printf '  %s\n' "$REPORTS/$f"
done
if [ -z "$ANNOTATIONS" ]; then
    printf 'To add manual annotations: copy annotations.template.json to %s/annotations.json, fill it, run ./report.sh %s again\n' \
        "$REPORTS" "$RUN_DIR"
fi
