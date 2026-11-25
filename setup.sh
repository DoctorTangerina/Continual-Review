#!/usr/bin/env bash
# Prepare this machine to run the experiment (Linux).
#
#   ./setup.sh                 set up everything; safe to run again at any time
#   ./setup.sh --pull-model    also `ollama pull` the review_model of the config (several GB)
#   ./setup.sh --no-swebench   only this project's environment (enough for tests and the demo)
#
# Steps:
#   1. .venv with Python >= 3.12 and this package installed editable (brings mini-swe-agent==2.4.6).
#      A .venv copied from another folder is recreated: its pip and scripts point to the old path.
#   2. SWE-bench harness checkout and swe-bench-tasks at the pinned commits, with their own .venv.
#   3. configs/swebench.local.json: created from the example, or its swebench_root and
#      swebench_python repointed when they do not reach the checkout.
#   4. Checks: Docker, Ollama and the model, .env; then the offline tests and `doctor`.
#
# Overrides (environment):
#   PYTHON         interpreter used to create .venv (default: first python3.1x >= 3.12 on PATH)
#   SWEBENCH_ROOT  SWE-bench checkout (default ../SWE-bench); also read by experiment.py itself
#   CONFIG         configuration to create/check (default configs/swebench.local.json)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PROJECT=$PWD

SWEBENCH_REPO=https://github.com/DoctorTangerina/SWE-bench.git
SWEBENCH_COMMIT=02e7a74ffd0b707aab73d203fe87bdc7c76afc8e
TASKS_REPO=https://github.com/SWE-bench/swe-bench-tasks.git
TASKS_COMMIT=3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d
CONFIG=${CONFIG:-configs/swebench.local.json}

PULL_MODEL=0
WITH_SWEBENCH=1
for arg in "$@"; do
    case $arg in
        --pull-model) PULL_MODEL=1 ;;
        --no-swebench) WITH_SWEBENCH=0 ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "setup: unknown option $arg (see --help)" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; WARNINGS+=("$*"); }
die()  { printf '  \033[31mfail\033[0m  %s\n' "$*" >&2; exit 1; }
WARNINGS=()

# A virtualenv activated in the calling shell (possibly a stale one) must not leak into
# the commands below: drop it from PATH and the environment.
if [ -n "${VIRTUAL_ENV:-}" ]; then
    PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$VIRTUAL_ENV/bin" | paste -sd:)
    unset VIRTUAL_ENV PYTHONHOME
fi

for tool in git curl; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
done

# --- 1. project environment ----------------------------------------------------------
step "Python environment (.venv)"

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; }
if [ -z "${PYTHON:-}" ]; then
    for candidate in python3 python3.14 python3.13 python3.12 python; do
        if command -v "$candidate" >/dev/null && python_ok "$candidate"; then PYTHON=$candidate; break; fi
    done
fi
[ -n "${PYTHON:-}" ] && python_ok "$PYTHON" || die "Python 3.12 or newer is required (set PYTHON=/path/to/python3.12+)"
ok "using $("$PYTHON" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

# A venv hardcodes its own path in pip and every script; one moved or copied from
# elsewhere imports fine but breaks `pip`, `mini` and editable installs.
venv_healthy() {
    [ -x .venv/bin/python ] && [ -f .venv/bin/pip ] \
        && [ "$(head -1 .venv/bin/pip)" = "#!$PROJECT/.venv/bin/python" ] \
        && .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null
}
if [ -d .venv ] && ! venv_healthy; then
    warn ".venv was created elsewhere or is broken ($(head -1 .venv/bin/pip 2>/dev/null | cut -c3-)); recreating it"
    rm -rf .venv
fi
if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
    ok "created .venv"
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
.venv/bin/python - <<'EOF' || die "the package did not install correctly"
import importlib.metadata as m, ef_review, minisweagent
print(f"  ok    ef-review {m.version('ef-continual-review')}, mini-swe-agent {m.version('mini-swe-agent')}")
EOF

# --- 2. SWE-bench harness ------------------------------------------------------------
checkout() {  # checkout <dir> <repo> <commit> <name>
    local dir=$1 repo=$2 commit=$3 name=$4
    if [ ! -d "$dir/.git" ]; then
        [ -e "$dir" ] && die "$dir exists but is not a Git checkout"
        git clone --quiet "$repo" "$dir"
        git -C "$dir" checkout --quiet "$commit"
        ok "$name cloned at ${commit:0:12}"
        return
    fi
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    if [ "$head" = "$commit" ]; then
        ok "$name at the pinned commit ${commit:0:12}"
    elif [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
        warn "$name is at ${head:0:12}, not ${commit:0:12}, and has local changes; left as is"
    else
        warn "$name is at ${head:0:12}, not the pinned ${commit:0:12}; left as is (git -C $dir checkout $commit)"
    fi
}

if [ "$WITH_SWEBENCH" = 1 ]; then
    step "SWE-bench harness"
    SWEBENCH_ROOT=$(readlink -m "${SWEBENCH_ROOT:-$PROJECT/../SWE-bench}")
    checkout "$SWEBENCH_ROOT" "$SWEBENCH_REPO" "$SWEBENCH_COMMIT" "SWE-bench ($SWEBENCH_ROOT)"
    checkout "$SWEBENCH_ROOT/swe-bench-tasks" "$TASKS_REPO" "$TASKS_COMMIT" "swe-bench-tasks"

    SWEBENCH_PY=$SWEBENCH_ROOT/.venv/bin/python
    if [ -d "$SWEBENCH_ROOT/.venv" ] && [ "$(head -1 "$SWEBENCH_ROOT/.venv/bin/pip" 2>/dev/null)" != "#!$SWEBENCH_ROOT/.venv/bin/python" ] \
        && [ "$(head -1 "$SWEBENCH_ROOT/.venv/bin/pip" 2>/dev/null)" != "#!$SWEBENCH_ROOT/.venv/bin/python3" ]; then
        warn "SWE-bench .venv was created elsewhere; recreating it"
        rm -rf "$SWEBENCH_ROOT/.venv"
    fi
    if ! "$SWEBENCH_PY" -c 'import swebench, docker' 2>/dev/null; then
        [ -d "$SWEBENCH_ROOT/.venv" ] || "$PYTHON" -m venv "$SWEBENCH_ROOT/.venv"
        "$SWEBENCH_PY" -m pip install --quiet --upgrade pip
        (cd "$SWEBENCH_ROOT" && .venv/bin/python -m pip install --quiet -e .)
    fi
    "$SWEBENCH_PY" -c 'import swebench, docker' || die "the SWE-bench environment does not import swebench/docker"
    ok "SWE-bench environment ($SWEBENCH_PY)"

    # --- 3. configuration ------------------------------------------------------------
    step "Configuration ($CONFIG)"
    .venv/bin/python - "$CONFIG" configs/swebench.example.json "$SWEBENCH_ROOT" "$SWEBENCH_PY" <<'EOF'
import json, os, sys
from pathlib import Path

config, example, root, python = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
created = not config.exists()
cfg = json.loads((example if created else config).read_text())
base = config.resolve().parent
changes = []

current = cfg.get("swebench_root")
if not current or (base / current).resolve() != root.resolve():
    cfg["swebench_root"] = os.path.relpath(root, base)
    changes.append(f"swebench_root: {current!r} -> {cfg['swebench_root']!r}")

cmd = cfg.get("swebench_python")
cmd = [cmd] if isinstance(cmd, str) else cmd
if not cmd or not Path(cmd[-1]).exists() and not cmd[0].endswith(".exe"):
    cfg["swebench_python"] = [python]
    changes.append(f"swebench_python: {cmd!r} -> {[python]!r}")

if created or changes:
    config.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"  ok    {'created from ' + str(example) if created else 'exists'}")
for c in changes:
    print(f"  ok    set {c}")
if cfg.get("review_model") in (None, "", "SET_THE_REVIEW_MODEL"):
    print(f"  warn  review_model is not set in {config}; edit it before running")
EOF
else
    step "SWE-bench harness"
    ok "skipped (--no-swebench)"
fi

# --- 4. services and credentials -----------------------------------------------------
step "Docker"
if docker info >/dev/null 2>&1; then
    ok "daemon reachable"
elif ! command -v docker >/dev/null; then
    warn "docker is not installed; the harness and the agent solver need it"
elif getent group docker | grep -qw "$(id -un)"; then
    warn "you are in the docker group but this shell is not (log out and in, or: newgrp docker); or the daemon is stopped (sudo systemctl start docker)"
else
    warn "cannot reach the daemon; start it (sudo systemctl enable --now docker) and join the group (sudo usermod -aG docker $(id -un), then log in again)"
fi

MODEL=""
API=""
if [ -f "$CONFIG" ]; then
    read -r MODEL API < <(.venv/bin/python -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c.get("review_model") or "-", c.get("api_base") or "http://localhost:11434")' "$CONFIG")
fi
case $MODEL in
    ollama/*|ollama_chat/*)
        step "Ollama ($MODEL)"
        NAME=${MODEL#*/}
        if ! command -v ollama >/dev/null; then
            warn "ollama is not installed (https://ollama.com/download)"
        elif ! curl -sf "$API/api/tags" >/dev/null; then
            warn "no Ollama server at $API (sudo systemctl start ollama, or: OLLAMA_NUM_PARALLEL=1 ollama serve)"
        elif curl -sf "$API/api/tags" | .venv/bin/python -c 'import json,sys; m=sys.argv[1]; n={x["name"] for x in json.load(sys.stdin)["models"]}; sys.exit(0 if m in n or m+":latest" in n else 1)' "$NAME"; then
            ok "server at $API, $NAME pulled"
        elif [ "$PULL_MODEL" = 1 ]; then
            ollama pull "$NAME" && ok "$NAME pulled"
        else
            warn "$NAME is not pulled yet (ollama pull $NAME, or ./setup.sh --pull-model)"
        fi
        ;;
    nvidia_nim/*)
        step "Credentials ($MODEL)"
        if [ -n "${NVIDIA_NIM_API_KEY:-}" ] || grep -qE '^\s*NVIDIA_NIM_API_KEY\s*=\s*\S' .env 2>/dev/null; then
            ok "NVIDIA_NIM_API_KEY is set"
        else
            warn "NVIDIA_NIM_API_KEY is missing; add it to .env (NVIDIA_NIM_API_KEY=...)"
        fi
        ;;
esac
if [ ! -f .env ]; then
    printf '# Credentials for the model provider, KEY=value (ignored by Git).\n# NVIDIA_NIM_API_KEY=\n# HF_TOKEN=\n' > .env
    ok "created an empty .env template for credentials"
fi

# --- 5. verification -----------------------------------------------------------------
step "Offline tests"
if .venv/bin/python -m unittest discover -s tests -q 2>&1 | tail -3; then
    ok "tests passed"
else
    warn "some tests failed (.venv/bin/python -m unittest discover -s tests -v)"
fi

if [ "$WITH_SWEBENCH" = 1 ]; then
    step "doctor"
    if .venv/bin/python experiment.py --config "$CONFIG" doctor; then
        ok "doctor finished"
    else
        warn "doctor reported a problem (see above)"
    fi
fi

# --- summary -------------------------------------------------------------------------
step "Summary"
if [ ${#WARNINGS[@]} -eq 0 ]; then
    echo "  Ready."
else
    echo "  Set up with ${#WARNINGS[@]} warning(s):"
    printf '   - %s\n' "${WARNINGS[@]}"
fi
cat <<EOF

  Activate the environment:  source .venv/bin/activate   (fish: source .venv/bin/activate.fish)
  Smoke test:                ./smoke.sh
EOF
