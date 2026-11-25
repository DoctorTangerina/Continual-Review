#!/usr/bin/env bash
# Prepare a run from a configuration and run its four conditions in order:
# none, static, automatic, curated. Conditions already complete are skipped.
#
#   ./run.sh                       resume the last run started by this script, or start a new one
#   ./run.sh runs/NAME             resume runs/NAME, or prepare it if it does not exist
#   ./run.sh --new                 always prepare a new run in runs/swebench-<date-time>
#   ./run.sh --retry-failed [...]  attempt the failed stages again, from where they failed
#
# Options:
#   --config FILE       configuration for a new run (default configs/swebench.local.json)
#   --assignments FILE  case assignments for a new run (default manifests/swebench_catalog.csv)
#   --workers N         cases of a stage in progress at once (default 1); with a local model
#                       the Ollama server needs OLLAMA_NUM_PARALLEL of at least N
#   --retry-failed      passed on to `experiment.py run`; each retried stage uses a call of the budget
#   --new               prepare a new run even if the last one is unfinished
#
# Stops when the run pauses for human curation (warmup seed, or each curated block):
# fill the curation file it names, then call the script again with the same run.
# A resumed run uses its frozen configuration; --config and --assignments only apply to prepare.
#
# Exit status: 0 complete or paused for curation, 1 error, 2 bad usage, 130 interrupted.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

CONFIG=${CONFIG:-configs/swebench.local.json}
ASSIGNMENTS=${ASSIGNMENTS:-manifests/swebench_catalog.csv}
WORKERS=${WORKERS:-1}
RETRY=()
NEW=0
RUN_DIR=""
LAST=runs/.last-run
CONDITIONS=(none static automatic curated)

if [ -t 1 ]; then
    RED=$'\033[31m' YELLOW=$'\033[33m' GREEN=$'\033[32m' BOLD=$'\033[1m' RESET=$'\033[0m'
else
    RED="" YELLOW="" GREEN="" BOLD="" RESET=""
fi
say()   { printf '%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
usage() { printf '%srun: %s (see --help)%s\n' "$RED" "$*" "$RESET" >&2; exit 2; }

ARGS=("$@")
while [ $# -gt 0 ]; do
    case $1 in
        --config)       [ $# -ge 2 ] || usage "--config needs a file";      CONFIG=$2; shift ;;
        --assignments)  [ $# -ge 2 ] || usage "--assignments needs a file"; ASSIGNMENTS=$2; shift ;;
        --workers)      [ $# -ge 2 ] || usage "--workers needs a number";   WORKERS=$2; shift ;;
        --retry-failed) RETRY=(--retry-failed) ;;
        --new)          NEW=1 ;;
        -h|--help)      sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        -*)             usage "unknown option $1" ;;
        *)              [ -z "$RUN_DIR" ] || usage "only one run directory"; RUN_DIR=${1%/} ;;
    esac
    shift
done
[[ $WORKERS =~ ^[1-9][0-9]*$ ]] || usage "--workers must be a positive integer"
[ "$NEW" = 1 ] && [ -n "$RUN_DIR" ] && usage "--new and a run directory together; give a new directory name instead"

# Task containers need the docker group. A shell opened before the user joined the group
# lacks it: start again under that group (as smoke.sh does).
if ! docker info >/dev/null 2>&1 && [ -z "${RUN_REEXEC:-}" ] && getent group docker | grep -qw "$(id -un)"; then
    export RUN_REEXEC=1 CONFIG ASSIGNMENTS WORKERS
    { printf 'exec %q' "$PWD/run.sh"; printf ' %q' "${ARGS[@]}"; echo; } | newgrp docker
    exit $?
fi

PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3

# condition_done RUN CONDITION: has this condition finished? (reads state.json, no lock)
condition_done() {
    "$PYTHON" -c 'import json,sys,pathlib; p=pathlib.Path(sys.argv[1],"conditions",sys.argv[2],"state.json")
sys.exit(0 if p.exists() and json.loads(p.read_text()).get("complete") else 1)' "$1" "$2"
}
run_done() {
    local c
    for c in "${CONDITIONS[@]}"; do condition_done "$1" "$c" || return 1; done
}

# report STEP REASON [HINT]: print an error in red with its reason, then stop.
report() {
    {
        printf '\n%s%s✗ %s failed%s\n' "$RED" "$BOLD" "$1" "$RESET"
        printf '%s  Reason: %s%s\n' "$RED" "$2" "$RESET"
        [ -n "${3:-}" ] && printf '%s\n' "$3" | sed 's/^/  /'
    } >&2
    exit "${4:-1}"
}

# fail STEP LOG STATUS: find the reason of a failed command in its output and what to do next.
fail() {
    local step=$1 log=$2 status=$3 reason hint=""
    if [ "$INTERRUPTED" = 1 ] || [ "$status" -eq 130 ] || [ "$status" -eq 143 ] \
        || grep -q '^KeyboardInterrupt' "$log"; then
        report "$step" "interrupted (Ctrl+C)" \
            "Stages cut off by the interruption are recorded as failed: resume with ./run.sh $RUN_DIR --retry-failed" 130
    else
        # experiment.py ends with "SomeError: message" (its own errors and Python tracebacks alike).
        reason=$(grep -v '^[[:space:]]*$' "$log" | tail -n 1)
        [ -n "$reason" ] || reason="exited with status $status and no message"
        case $reason in
            *"Stage previously failed"*|*"Stage failed"*)
                # The message names the stage as <event>/<stage>; its error record is <event>/<stage>.failed.json.
                local stage failed
                stage=$(printf '%s' "$reason" | sed -n 's|.*: \([^ ;:]*/[^ ;:]*\);.*|\1|p')
                failed=$(find "$RUN_DIR" -path "*/${stage:-?}.failed.json" -printf '%T@ %p\n' 2>/dev/null \
                         | sort -n | tail -n 1 | cut -d' ' -f2-)
                [ -n "$failed" ] && hint="Details: $failed and $RUN_DIR/audit.jsonl"$'\n'
                hint+="After fixing the cause, try again from this point: ./run.sh $RUN_DIR --retry-failed" ;;
            *"Run locked"*)
                hint="Another experiment.py may be using this run (pgrep -af experiment.py). If none is, remove $RUN_DIR/.run.lock" ;;
            *"Call limit reached"*)
                hint="The run's call budget (max_calls) is used up; retrying will not help. Prepare a new run with a larger budget" ;;
            *"changed after prep"*|*"source changed"*|*"texts changed"*)
                hint="This run no longer matches the code or environment it was prepared with. Start a new one: ./run.sh --new" ;;
            *"Ollama"*|*"ollama"*)
                hint="Is the Ollama server running? (sudo systemctl start ollama, or: OLLAMA_NUM_PARALLEL=$WORKERS ollama serve)" ;;
            *docker*|*Docker*)
                hint="Is the Docker daemon running and are you in the docker group? (docker info)" ;;
        esac
    fi
    report "$step" "$reason" "$hint"
}

# step NAME CMD...: run CMD showing its output; on error, report it and stop.
step() {
    local name=$1 status=0
    shift
    # tee ignores Ctrl+C: after the first one experiment.py still finishes the cases in progress
    # and prints, and a closed pipe would kill it.
    "$@" 2>&1 | (trap '' INT; tee "$LOG") || status=${PIPESTATUS[0]}
    if [ "$status" -ne 0 ] || [ "$INTERRUPTED" = 1 ]; then
        if grep -q "Human curation pending" "$LOG"; then
            local file
            file=$(grep -o 'Human curation pending: .*' "$LOG" | tail -n 1 | cut -d' ' -f4-)
            printf '\n%s%s⏸ Paused for human curation%s\n' "$YELLOW" "$BOLD" "$RESET"
            printf '  Fill in: %s\n' "$file"
            printf '  (decision, reviewer, reason, human_seconds for each candidate; see README section 4)\n'
            printf '  Then continue with: ./run.sh %s\n' "$RUN_DIR"
            exit 0
        fi
        fail "$name" "$LOG" "$status"
    fi
}

LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT
INTERRUPTED=0
trap 'INTERRUPTED=1' INT

# --- which run -----------------------------------------------------------------------
if [ -z "$RUN_DIR" ] && [ "$NEW" = 0 ] && [ -f "$LAST" ]; then
    previous=$(<"$LAST")
    if [ -f "$previous/manifest.json" ] && ! run_done "$previous"; then
        RUN_DIR=$previous
    fi
fi
RUN_DIR=${RUN_DIR:-runs/swebench-$(date +%m%d-%H%M%S)}
mkdir -p runs
printf '%s\n' "$RUN_DIR" > "$LAST"

# --- prepare -------------------------------------------------------------------------
if [ -f "$RUN_DIR/manifest.json" ]; then
    say "Resuming $RUN_DIR"
elif [ -e "$RUN_DIR" ]; then
    report "prepare" "$RUN_DIR exists but is not a prepared run (no manifest.json)" \
        "Choose another name, or remove it if it is left over from a failed prepare"
else
    [ -f "$CONFIG" ] || report "prepare" "configuration $CONFIG does not exist" \
        "Create it with ./setup.sh, or pass --config FILE"
    [ -f "$ASSIGNMENTS" ] || report "prepare" "assignments $ASSIGNMENTS do not exist" \
        "Export it with: $PYTHON experiment.py --config $CONFIG catalog --output $ASSIGNMENTS"$'\n'"then fill role/block (README section 3), or pass --assignments FILE"
    say "Preparing $RUN_DIR (config $CONFIG, assignments $ASSIGNMENTS)"
    step "prepare" "$PYTHON" experiment.py --config "$CONFIG" prepare --assignments "$ASSIGNMENTS" --run-dir "$RUN_DIR"
fi

# --- conditions ----------------------------------------------------------------------
for condition in "${CONDITIONS[@]}"; do
    if condition_done "$RUN_DIR" "$condition"; then
        printf '  %s✓%s %s already complete\n' "$GREEN" "$RESET" "$condition"
        continue
    fi
    say "Condition '$condition' (started $(date +%T), workers $WORKERS${RETRY:+, retrying failed stages})"
    step "condition '$condition'" "$PYTHON" experiment.py run --run-dir "$RUN_DIR" --condition "$condition" \
        --live --workers "$WORKERS" "${RETRY[@]}"
done

printf '\n%s%s✓ All conditions complete in %s%s (%ss)\n' "$GREEN" "$BOLD" "$RUN_DIR" "$RESET" "$SECONDS"
"$PYTHON" experiment.py status --run-dir "$RUN_DIR" || true
echo "Report (reveals the reserved evaluations): $PYTHON experiment.py report --run-dir $RUN_DIR"
