#!/usr/bin/env bash
# Create the local authoring environment, then verify it.
#
# This script is the installing counterpart to check-setup.sh: it creates a
# Python virtual environment, installs the pinned host dependencies and the
# Harbor CLI, seeds .env, and finally runs ./scripts/check-setup.sh.
set -uo pipefail

usage() {
    cat <<'USAGE'
Usage: setup.sh [options]

Create the local authoring environment and verify it:

  1. Find a Python 3.11+ interpreter (3.12+ preferred; Harbor needs 3.12+).
  2. Create a virtual environment and install requirements.txt into it.
  3. Install the Harbor CLI (uv tool install, or pip inside the venv).
  4. Report on Docker, an agent CLI, and the other tools it cannot install.
  5. Copy .env.example to .env when .env is missing.
  6. Run ./scripts/check-setup.sh.

Docker, Claude Code, and Codex are never installed automatically; this script
prints the command for your platform instead.

Options:
  -y, --yes           Do not prompt; accept the documented installs (this
                      allows downloading uv from https://astral.sh when no
                      Python 3.12+ interpreter is available).
      --venv PATH     Virtual environment path (default: .venv).
      --skip-harbor   Do not install or check the Harbor CLI.
      --skip-check    Do not run ./scripts/check-setup.sh at the end.
  -h, --help          Show this help.
USAGE
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
ASSUME_YES=0
VENV_PATH="${REPO_ROOT}/.venv"
SKIP_HARBOR=0
SKIP_CHECK=0
MANUAL_STEPS=()

while (($# > 0)); do
    case "$1" in
        -y|--yes)
            ASSUME_YES=1
            shift
            ;;
        --venv)
            if (($# < 2)); then
                printf 'ERROR: --venv requires a path\n' >&2
                exit 2
            fi
            VENV_PATH="$2"
            shift 2
            ;;
        --skip-harbor)
            SKIP_HARBOR=1
            shift
            ;;
        --skip-check)
            SKIP_CHECK=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'ERROR: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

step() {
    printf '\n==> %s\n' "$1"
}

info() {
    printf '    %s\n' "$1"
}

note_manual() {
    MANUAL_STEPS+=("$1")
    printf '    TODO: %s\n' "$1"
}

die() {
    printf '\nERROR: %s\n' "$1" >&2
    exit 1
}

confirm() {
    local prompt="$1"
    if ((ASSUME_YES)); then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        info "Not a terminal and --yes was not passed; skipping: ${prompt}"
        return 1
    fi
    local reply=""
    read -r -p "    ${prompt} [y/N] " reply
    [[ "$reply" == [yY] || "$reply" == [yY][eE][sS] ]]
}

python_at_least() {
    # python_at_least <interpreter> <minor>: true when the interpreter is 3.<minor>+.
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, $2) else 1)" >/dev/null 2>&1
}

install_hint() {
    # Print the platform-appropriate install command for a package name.
    if [[ "$(uname -s)" == "Darwin" ]]; then
        printf 'brew install %s' "$1"
    else
        printf 'sudo apt-get install -y %s' "$1"
    fi
}

printf 'Honeydew authoring environment setup\n'
printf 'Repository: %s\n' "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 1. Python interpreter
# ---------------------------------------------------------------------------
step 'Selecting a Python interpreter'
HOST_PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    candidate_path="$(command -v "$candidate" 2>/dev/null || true)"
    [[ -n "$candidate_path" ]] || continue
    python_at_least "$candidate_path" 11 || continue
    HOST_PYTHON="$candidate_path"
    # Prefer 3.12+ so the venv can also host the Harbor CLI.
    python_at_least "$candidate_path" 12 && break
done

if [[ -z "$HOST_PYTHON" ]]; then
    die "no Python 3.11+ interpreter found. Install one with: $(install_hint 'python@3.12'), then rerun this script."
fi
info "Using $HOST_PYTHON ($("$HOST_PYTHON" --version 2>&1))"

VENV_PYTHON_312=0
python_at_least "$HOST_PYTHON" 12 && VENV_PYTHON_312=1

# ---------------------------------------------------------------------------
# 2. Virtual environment and pinned host dependencies
# ---------------------------------------------------------------------------
step "Creating the virtual environment at ${VENV_PATH}"
if [[ -x "${VENV_PATH}/bin/python3" ]]; then
    info 'Reusing the existing virtual environment.'
else
    "$HOST_PYTHON" -m venv "$VENV_PATH" ||
        die "could not create a virtual environment at ${VENV_PATH}. On Debian/Ubuntu install python3-venv first."
    info 'Created.'
fi
VENV_PYTHON="${VENV_PATH}/bin/python3"

step 'Installing pinned host dependencies (requirements.txt)'
"$VENV_PYTHON" -m pip install --quiet --upgrade pip ||
    info 'Could not upgrade pip; continuing with the bundled version.'
if "$VENV_PYTHON" -m pip install --quiet -r "${REPO_ROOT}/requirements.txt"; then
    info "Installed: $(tr '\n' ' ' <"${REPO_ROOT}/requirements.txt")"
else
    die "failed to install ${REPO_ROOT}/requirements.txt into ${VENV_PATH}."
fi

# ---------------------------------------------------------------------------
# 3. Harbor CLI
# ---------------------------------------------------------------------------
if ((SKIP_HARBOR)); then
    step 'Harbor CLI (skipped)'
else
    step 'Installing the Harbor CLI'
    if command -v harbor >/dev/null 2>&1; then
        info "Already installed: $(command -v harbor) ($(harbor --version 2>&1 | head -n 1))"
    else
        uv_path="$(command -v uv 2>/dev/null || true)"
        if [[ -z "$uv_path" ]] && ((VENV_PYTHON_312 == 0)); then
            info 'Harbor needs Python 3.12+, and neither uv nor a 3.12+ interpreter is available.'
            if confirm 'Download and install uv from https://astral.sh?'; then
                curl -LsSf https://astral.sh/uv/install.sh | sh
                uv_path="$(command -v uv 2>/dev/null || true)"
                if [[ -z "$uv_path" && -x "${HOME}/.local/bin/uv" ]]; then
                    uv_path="${HOME}/.local/bin/uv"
                fi
            fi
        fi

        if [[ -n "$uv_path" ]]; then
            if "$uv_path" tool install harbor; then
                info "Installed with uv: $("$uv_path" tool dir 2>/dev/null || printf 'uv tool directory')"
                command -v harbor >/dev/null 2>&1 ||
                    note_manual "Add uv's tool bin directory (usually ~/.local/bin) to your PATH so 'harbor' resolves."
            else
                note_manual 'Install the Harbor CLI manually: uv tool install harbor'
            fi
        elif ((VENV_PYTHON_312)); then
            if "$VENV_PYTHON" -m pip install --quiet harbor; then
                info "Installed into the virtual environment: ${VENV_PATH}/bin/harbor"
                note_manual "Activate the virtual environment before using harbor: source ${VENV_PATH}/bin/activate"
            else
                note_manual 'Install the Harbor CLI manually: uv tool install harbor'
            fi
        else
            note_manual 'Install the Harbor CLI manually: uv tool install harbor (needs Python 3.12+)'
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 4. Tools this script does not install
# ---------------------------------------------------------------------------
step 'Checking tools that must be installed by hand'

if command -v docker >/dev/null 2>&1; then
    info "Docker CLI: $(docker --version 2>&1 | head -n 1)"
    if docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
        info "Docker daemon: reachable (server $(docker info --format '{{.ServerVersion}}' 2>/dev/null))"
    else
        note_manual 'Start Docker Desktop or the Docker service; the daemon is not reachable.'
    fi
else
    if [[ "$(uname -s)" == "Darwin" ]]; then
        note_manual 'Install Docker Desktop: https://docs.docker.com/get-started/get-docker/ (Apple silicon: enable Settings -> General -> Use Rosetta for x86/amd64 emulation)'
    else
        note_manual 'Install Docker Engine and the Compose plugin: https://docs.docker.com/engine/install/'
    fi
fi

agent_found=""
for agent_command in claude codex; do
    if command -v "$agent_command" >/dev/null 2>&1; then
        agent_found="${agent_found} ${agent_command}"
    fi
done
if [[ -n "$agent_found" ]]; then
    info "Agent CLI:${agent_found}"
else
    note_manual 'Install an agent CLI: npm install -g @anthropic-ai/claude-code   (or: npm install -g @openai/codex)'
fi

for tool in git make rg; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        package="$tool"
        [[ "$tool" == "rg" ]] && package="ripgrep"
        note_manual "Install ${package}: $(install_hint "$package")"
    fi
done

# ---------------------------------------------------------------------------
# 5. Workbench runner token
# ---------------------------------------------------------------------------
step 'Preparing .env'
if [[ -f "${REPO_ROOT}/.env" ]]; then
    info '.env already exists; leaving it untouched.'
else
    if [[ -f "${REPO_ROOT}/.env.example" ]]; then
        cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
        info 'Copied .env.example to .env.'
    else
        note_manual 'Create .env with WORKBENCH_RUNNER_TOKEN=<token>.'
    fi
fi
if ! grep -qE '^[[:space:]]*WORKBENCH_RUNNER_TOKEN[[:space:]]*=[[:space:]]*[^[:space:]]' "${REPO_ROOT}/.env" 2>/dev/null; then
    note_manual 'Create an access token at https://workbench.alignedhq.ai (profile -> Settings) and paste it into .env as WORKBENCH_RUNNER_TOKEN=<token>. Never commit or share it.'
fi

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
if ((${#MANUAL_STEPS[@]} > 0)); then
    printf '\nSTEPS LEFT FOR YOU\n'
    for ((index = 0; index < ${#MANUAL_STEPS[@]}; index++)); do
        printf '%d. %s\n' "$((index + 1))" "${MANUAL_STEPS[$index]}"
    done
fi

printf '\nActivate the environment in every new shell before running the runner:\n'
printf '    source %s/bin/activate\n' "$VENV_PATH"

if ((SKIP_CHECK)); then
    printf '\nSkipping ./scripts/check-setup.sh (--skip-check).\n'
    exit 0
fi

step 'Verifying with ./scripts/check-setup.sh'
"${SCRIPT_DIR}/check-setup.sh"
exit $?
