#!/usr/bin/env bash
# Smoke test: prepare a run from manifests/swebench_smoke.csv and run one condition live.
#
#   ./smoke.sh                 new run in runs/agent-smoke-<date-time>
#   ./smoke.sh runs/NAME       new run with that name, or resume it if it already exists
#
# The first pass stops at the human curation of the warmup seed. Fill the curation
# file it names and call the script again with the same run directory to continue.
#
# Overrides (environment): CONFIG, ASSIGNMENTS, CONDITION, WORKERS.
# WORKERS above 1 needs the Ollama server started with OLLAMA_NUM_PARALLEL of at least that number.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG=${CONFIG:-configs/swebench.local.json}
ASSIGNMENTS=${ASSIGNMENTS:-manifests/swebench_smoke.csv}
CONDITION=${CONDITION:-none}
WORKERS=${WORKERS:-1}
RUN_DIR=${1:-runs/agent-smoke-$(date +%m%d-%H%M%S)}

# The task containers need the docker group. A session opened before the user joined
# the group does not have it yet, so start again under that group.
if ! docker info >/dev/null 2>&1; then
    if [ -z "${SMOKE_REEXEC:-}" ] && getent group docker | grep -qw "$(id -un)"; then
        export SMOKE_REEXEC=1 CONFIG ASSIGNMENTS CONDITION WORKERS
        printf 'exec %q %q\n' "$PWD/$(basename "$0")" "$RUN_DIR" | newgrp docker
        exit $?
    fi
    echo "smoke: cannot reach the docker daemon (is it running? are you in the docker group?)" >&2
    exit 1
fi

PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python

if [ -d "$RUN_DIR" ]; then
    echo "smoke: resuming $RUN_DIR"
else
    echo "smoke: preparing $RUN_DIR ($CONFIG, $ASSIGNMENTS)"
    "$PYTHON" experiment.py --config "$CONFIG" prepare --assignments "$ASSIGNMENTS" --run-dir "$RUN_DIR"
fi

echo "smoke: running condition '$CONDITION' (started $(date +%T))"
log=$(mktemp)
trap 'rm -f "$log"' EXIT
status=0
"$PYTHON" experiment.py run --run-dir "$RUN_DIR" --condition "$CONDITION" --live --workers "$WORKERS" 2>&1 | tee "$log" || status=$?
echo "smoke: finished $(date +%T), ${SECONDS}s"

# The pause for curation ends the command with an error code; here it is the expected stop.
if grep -q "Human curation pending" "$log"; then
    echo "smoke: paused for curation. Fill the file above, then: ./smoke.sh $RUN_DIR"
    exit 0
fi
exit "$status"
