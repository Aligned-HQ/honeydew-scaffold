#!/usr/bin/env bash
# Run one of the local authoring skills and write an auditable run record.
set -uo pipefail

usage() {
    cat <<'USAGE'
Usage: run-skill.sh --skill SKILL [TARGET] [options]

Run SKILL through a locally installed agent CLI. TARGET is relative to this
repository (or an absolute path inside it). For a submission-ready scaffold
run, task-fixer and task-review target the literal task/ directory, while
trajectory-review targets trajectories/. A task-specific Harbor identity belongs
in task/task.toml under [task].name; it is not a directory rename.

Skills:
  task-fixer          Repair and normalize a task before review.
  task-review         Produce the rubric scorecard for a task.
  trajectory-review   Review a completed trajectory run or task path.

Options:
  --runner codex|claude|auto  Agent CLI to use (default: auto).
  --target PATH               Explicit target path.
  --docker-access auto|on|off Allow the skill to reach Docker (default: auto).
  --dry-run                   Record the command without invoking an agent.
  -h, --help                  Show this help.

The selected runner reads the local skill file from .agents/skills/ (Codex) or
.claude/skills/ (Claude). Each run overwrites its Markdown result in
skill-reports/ and updates skill-status.md.
USAGE
}

die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 2
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

SKILL=""
TARGET=""
RUNNER="${SKILL_RUNNER:-auto}"
DRY_RUN=0
DOCKER_ACCESS="${SKILL_DOCKER_ACCESS:-${TASK_FIXER_DOCKER_ACCESS:-auto}}"

while (($# > 0)); do
    case "$1" in
        --skill)
            (($# >= 2)) || die "--skill requires a value"
            SKILL="$2"
            shift 2
            ;;
        --runner)
            (($# >= 2)) || die "--runner requires codex, claude, or auto"
            RUNNER="$2"
            shift 2
            ;;
        --target)
            (($# >= 2)) || die "--target requires a path"
            TARGET="$2"
            shift 2
            ;;
        --docker-access)
            (($# >= 2)) || die "--docker-access requires auto, on, or off"
            DOCKER_ACCESS="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            (($# == 1 && -z "$TARGET")) || die "only one target path is allowed"
            TARGET="$1"
            shift
            ;;
        -* )
            die "unknown option: $1"
            ;;
        *)
            [[ -z "$TARGET" ]] || die "only one target path is allowed"
            TARGET="$1"
            shift
            ;;
    esac
done

case "$SKILL" in
    task-fixer|task-review)
        [[ -n "$TARGET" ]] || TARGET="task"
        ;;
    trajectory-review)
        [[ -n "$TARGET" ]] || die "trajectory-review requires a target path"
        ;;
    *)
        die "--skill must be task-fixer, task-review, or trajectory-review"
        ;;
esac

case "$RUNNER" in
    auto)
        if command -v codex >/dev/null 2>&1; then
            RUNNER="codex"
        elif command -v claude >/dev/null 2>&1; then
            RUNNER="claude"
        else
            die "no supported agent CLI found; install codex or claude"
        fi
        ;;
    codex|claude)
        ;;
    *)
        die "--runner must be codex, claude, or auto"
        ;;
esac

case "$DOCKER_ACCESS" in
    auto|on|off)
        ;;
    *)
        die "--docker-access must be auto, on, or off"
        ;;
esac

if [[ "$TARGET" = /* ]]; then
    TARGET_ABS="$TARGET"
else
    TARGET_ABS="${REPO_ROOT}/${TARGET}"
fi
[[ -d "$TARGET_ABS" ]] || die "target directory does not exist: $TARGET"
TARGET_ABS="$(cd -- "$TARGET_ABS" && pwd -P)"
case "$TARGET_ABS" in
    "${REPO_ROOT}"/*)
        ;;
    *)
        die "target must be inside the repository: $TARGET_ABS"
        ;;
esac
TARGET_REL="${TARGET_ABS#${REPO_ROOT}/}"

case "$RUNNER" in
    codex)
        SKILL_ROOT="${REPO_ROOT}/.agents/skills"
        RUNNER_BIN="$(command -v codex)"
        ;;
    claude)
        SKILL_ROOT="${REPO_ROOT}/.claude/skills"
        RUNNER_BIN="$(command -v claude)"
        ;;
esac

# A workspace-write Codex sandbox generally cannot reach the host Docker
# socket. Claude Code's normal acceptEdits mode can also stop at a tool
# permission prompt in non-interactive runs. Every skill here needs to inspect
# task images — task-fixer and task-review to validate the environment, and
# trajectory-review to tell a structural environment defect apart from a genuine
# science failure — so the automatic mode selects a Docker-capable execution
# mode for the chosen CLI. This does not repair a denied host daemon or
# authorize an unapproved remote context; use --docker-access off for a
# static-only run.
CODEX_SANDBOX="workspace-write"
if [[ "$RUNNER" = codex && "$DOCKER_ACCESS" != off ]]; then
    CODEX_SANDBOX="danger-full-access"
fi
CLAUDE_PERMISSION_MODE="acceptEdits"
CLAUDE_ALLOW_DANGEROUS=0
if [[ "$RUNNER" = claude && "$DOCKER_ACCESS" != off ]]; then
    CLAUDE_PERMISSION_MODE="bypassPermissions"
    CLAUDE_ALLOW_DANGEROUS=1
fi

# An agent CLI that predates one of the flags below fails deep inside the run
# with an opaque parser error. Probe --help first and name the upgrade instead.
CLAUDE_KNOWN_GOOD_VERSION="2.1.220"
CODEX_KNOWN_GOOD_VERSION="0.145.0"

runner_help_text() {
    if [[ "$RUNNER" = codex ]]; then
        { "$RUNNER_BIN" --help; "$RUNNER_BIN" exec --help; } 2>&1
    else
        "$RUNNER_BIN" --help 2>&1
    fi
}

require_runner_flags() {
    local help_text required=() missing=() flag
    if [[ "$RUNNER" = codex ]]; then
        required=(--ask-for-approval --sandbox --skip-git-repo-check --cd)
    else
        required=(--print --no-session-persistence --permission-mode --add-dir)
        ((CLAUDE_ALLOW_DANGEROUS)) && required+=(--allow-dangerously-skip-permissions)
    fi

    help_text="$(runner_help_text || true)"
    # An unreadable --help is not evidence of an old CLI; let the run proceed.
    [[ -n "$help_text" ]] || return 0

    for flag in "${required[@]}"; do
        grep -q -- "$flag" <<<"$help_text" || missing+=("$flag")
    done
    ((${#missing[@]} > 0)) || return 0

    local version upgrade known_good
    version="$("$RUNNER_BIN" --version 2>&1 | head -n 1)"
    if [[ "$RUNNER" = codex ]]; then
        upgrade="codex update  (or: npm install -g @openai/codex@latest)"
        known_good="$CODEX_KNOWN_GOOD_VERSION"
    else
        upgrade="claude update  (or: npm install -g @anthropic-ai/claude-code@latest)"
        known_good="$CLAUDE_KNOWN_GOOD_VERSION"
    fi

    printf 'ERROR: %s is too old for the skill wrappers.\n' "$RUNNER" >&2
    printf '  installed:   %s (%s)\n' "$version" "$RUNNER_BIN" >&2
    printf '  missing:     %s\n' "${missing[*]}" >&2
    printf '  known good:  %s %s\n' "$RUNNER" "$known_good" >&2
    printf '  upgrade:     %s\n' "$upgrade" >&2
    printf 'Then rerun this command, or use --runner %s to try the other CLI.\n' \
        "$([[ "$RUNNER" = codex ]] && printf 'claude' || printf 'codex')" >&2
    exit 2
}

require_runner_flags

SKILL_FILE="${SKILL_ROOT}/${SKILL}/SKILL.md"
[[ -f "$SKILL_FILE" ]] || die "missing local skill file: $SKILL_FILE"

AGENTS_SKILL_FILE="${REPO_ROOT}/.agents/skills/${SKILL}/SKILL.md"
CLAUDE_SKILL_FILE="${REPO_ROOT}/.claude/skills/${SKILL}/SKILL.md"
if [[ -f "$AGENTS_SKILL_FILE" && -f "$CLAUDE_SKILL_FILE" ]]; then
    cmp -s "$AGENTS_SKILL_FILE" "$CLAUDE_SKILL_FILE" || \
        die "the .agents and .claude copies of ${SKILL} are not identical"
fi

sha256_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        return 1
    fi
}

SKILL_SHA256="$(sha256_file "$SKILL_FILE")" || die "could not calculate the skill hash"

REPORT_DIR="${REPO_ROOT}/skill-reports"
STATUS_FILE="${REPO_ROOT}/skill-status.md"
REPORT_REL="skill-reports/${SKILL}.md"
REPORT_FILE="${REPO_ROOT}/${REPORT_REL}"
mkdir -p "$REPORT_DIR" || die "could not create $REPORT_DIR"

status_row_field() {
    local wanted="$1"
    local field_number="$2"
    local fallback="$3"
    if [[ ! -f "$STATUS_FILE" ]]; then
        printf '%s\n' "$fallback"
        return
    fi
    awk -F'|' -v wanted="$wanted" -v field_number="$field_number" -v fallback="$fallback" '
        NR > 3 {
            name = $2
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            gsub(/`/, "", name)
            if (name == wanted) {
                value = $field_number
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                gsub(/`/, "", value)
                print value
                found = 1
                exit
            }
        }
        END {
            if (!found) print fallback
        }
    ' "$STATUS_FILE"
}

write_status_file() {
    local current_status="$1"
    local current_time="$2"
    local current_target="$3"
    local status_tmp="${STATUS_FILE}.tmp.$$"
    local skill_name
    local row_status
    local row_time
    local row_target

    {
        printf '%s\n\n' '# Skill status'
        printf '%s\n\n' 'This file is overwritten whenever a skill wrapper runs.'
        printf '| Skill | Status | Last run (UTC) | Target | Report |\n'
        printf '|---|---|---|---|---|\n'
        for skill_name in task-fixer task-review trajectory-review; do
            if [[ "$skill_name" = "$SKILL" ]]; then
                row_status="$current_status"
                row_time="$current_time"
                row_target="$current_target"
            else
                row_status="$(status_row_field "$skill_name" 3 "Not Run")"
                row_time="$(status_row_field "$skill_name" 4 "—")"
                row_target="$(status_row_field "$skill_name" 5 "—")"
            fi
            printf '| `%s` | %s | %s | `%s` | [%s.md](skill-reports/%s.md) |\n' \
                "$skill_name" "$row_status" "$row_time" "$row_target" \
                "$skill_name" "$skill_name"
        done
    } > "$status_tmp" || {
        rm -f "$status_tmp"
        return 1
    }
    mv "$status_tmp" "$STATUS_FILE"
}

OUTPUT_TMP="$(mktemp)" || die "could not create temporary skill output file"
FINAL_OUTPUT_TMP="$(mktemp)" || {
    rm -f "$OUTPUT_TMP"
    die "could not create temporary final skill output file"
}
STATUS_OUTPUT_TMP="$(mktemp)" || {
    rm -f "$OUTPUT_TMP" "$FINAL_OUTPUT_TMP"
    die "could not create temporary status output file"
}
AGENT_EXIT_TMP="$(mktemp)" || {
    rm -f "$OUTPUT_TMP" "$FINAL_OUTPUT_TMP" "$STATUS_OUTPUT_TMP"
    die "could not create temporary agent status file"
}
PROGRESS_DONE_TMP="$(mktemp)" || {
    rm -f "$OUTPUT_TMP" "$FINAL_OUTPUT_TMP" "$STATUS_OUTPUT_TMP" "$AGENT_EXIT_TMP"
    die "could not create temporary progress status file"
}
rm -f "$PROGRESS_DONE_TMP" || {
    rm -f "$OUTPUT_TMP" "$FINAL_OUTPUT_TMP" "$STATUS_OUTPUT_TMP" "$AGENT_EXIT_TMP"
    die "could not initialize temporary progress status file"
}

RUN_ID="$(date -u '+%Y%m%dT%H%M%SZ')-${SKILL}-$$"
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
RUN_STATUS="FAILED"
EXIT_CODE=1
FINALIZED=0
PROGRESS_PID=""
AGENT_PIPELINE_PID=""

write_status_file "Run" "$STARTED_AT" "$TARGET_REL" || die "could not update $STATUS_FILE"

extract_final_handoff() {
    local source="$1"
    local destination="$2"

    awk '
        function is_status(line, cleaned) {
            cleaned = line
            gsub(/[*][*]/, "", cleaned)
            return cleaned ~ /^[[:space:]]*(Status|Verdict):[[:space:]]*(PASS|FAIL|INCONCLUSIVE)([[:space:]]|$)/
        }
        {
            lines[NR] = $0
            if (is_status($0)) {
                start = NR
            }
        }
        END {
            if (!start) {
                start = 1
            }
            for (line = start; line <= NR; line++) {
                if (line > start && lines[line] ~ /^[[:space:]]*tokens used([[:space:]]|$)/) {
                    break
                }
                print lines[line]
            }
        }
    ' "$source" > "$destination"
}

extract_task_review_handoff() {
    local source="$1"
    local destination="$2"

    awk '
        {
            lines[NR] = $0
            heading = tolower($0)
            if (heading ~ /^##[[:space:]]+practitioner[[:space:]]+plausibility[[:space:]]*$/) {
                start = NR
            }
        }
        END {
            if (!start) {
                exit 2
            }
            for (line = start; line <= NR; line++) {
                if (line > start && lines[line] ~ /^[[:space:]]*tokens used([[:space:]]|$)/) {
                    break
                }
                print lines[line]
            }
        }
    ' "$source" > "$destination" || return $?
}

extract_trajectory_review_handoff() {
    local source="$1"
    local destination="$2"

    awk '
        function is_run_reviewed(line, cleaned) {
            cleaned = line
            sub(/^[[:space:]]*##?[[:space:]]*/, "", cleaned)
            return tolower(cleaned) ~ /^[[:space:]]*run reviewed[[:space:]]*$/
        }
        {
            lines[NR] = $0
            if (is_run_reviewed($0)) {
                start = NR
            }
        }
        END {
            if (!start) {
                exit 2
            }
            for (line = start; line <= NR; line++) {
                if (line > start && lines[line] ~ /^[[:space:]]*tokens used([[:space:]]|$)/) {
                    break
                }
                print lines[line]
            }
        }
    ' "$source" > "$destination" || return $?
}

prepare_report_outputs() {
    local source="$1"

    case "$SKILL" in
        task-fixer)
            extract_final_handoff "$source" "$FINAL_OUTPUT_TMP"
            ;;
        task-review)
            if ! extract_task_review_handoff "$source" "$FINAL_OUTPUT_TMP"; then
                extract_final_handoff "$source" "$FINAL_OUTPUT_TMP"
            fi
            ;;
        trajectory-review)
            if ! extract_trajectory_review_handoff "$source" "$FINAL_OUTPUT_TMP"; then
                extract_final_handoff "$source" "$FINAL_OUTPUT_TMP"
            fi
            ;;
        *)
            cp "$source" "$FINAL_OUTPUT_TMP"
            ;;
    esac
    extract_final_handoff "$source" "$STATUS_OUTPUT_TMP"
}

skill_result_status() {
    if [[ "$RUN_STATUS" = "DRY_RUN" ]]; then
        printf '%s\n' 'Run'
        return
    fi
    if ((EXIT_CODE != 0)); then
        printf '%s\n' 'Fail'
        return
    fi
    if sed 's/[*][*]//g' "$STATUS_OUTPUT_TMP" | grep -Ei \
        '^[[:space:]]*(Status|Verdict):[[:space:]]*PASS([[:space:]]|$)' >/dev/null; then
        printf '%s\n' 'Pass'
    elif sed 's/[*][*]//g' "$STATUS_OUTPUT_TMP" | grep -Ei \
        '^[[:space:]]*(Status|Verdict):[[:space:]]*(FAIL|INCONCLUSIVE)([[:space:]]|$)' >/dev/null; then
        printf '%s\n' 'Fail'
    else
        printf '%s\n' 'Fail'
    fi
}

write_report() {
    local result_status="$1"
    local ended_at="$2"
    local output_sha256="$3"
    local report_tmp="${REPORT_FILE}.tmp.$$"
    {
        printf '# Skill report: %s\n\n' "$SKILL"
        printf '**Status:** %s\n\n' "$result_status"
        printf '| Field | Value |\n'
        printf '|---|---|\n'
        printf '| Run ID | `%s` |\n' "$RUN_ID"
        printf '| Skill | `%s` |\n' "$SKILL"
        printf '| Runner | `%s` |\n' "$RUNNER"
        printf '| Docker access | `%s` |\n' "$DOCKER_ACCESS"
        printf '| Codex sandbox | `%s` |\n' "$CODEX_SANDBOX"
        printf '| Claude permission mode | `%s` |\n' "$CLAUDE_PERMISSION_MODE"
        printf '| Target | `%s` |\n' "$TARGET_REL"
        printf '| Started (UTC) | `%s` |\n' "$STARTED_AT"
        printf '| Finished (UTC) | `%s` |\n' "$ended_at"
        printf '| Exit code | `%s` |\n' "$EXIT_CODE"
        printf '| Skill SHA-256 | `%s` |\n' "$SKILL_SHA256"
        printf '| Agent output SHA-256 | `%s` |\n' "$output_sha256"
        case "$SKILL" in
            task-fixer) printf '\n## Final handoff\n\n' ;;
            task-review) printf '\n## Final review\n\n' ;;
            *) printf '\n## Agent output\n\n' ;;
        esac
        if [[ -s "$FINAL_OUTPUT_TMP" ]]; then
            cat "$FINAL_OUTPUT_TMP"
            printf '\n'
        else
            printf '_The agent produced no output._\n'
        fi
    } > "$report_tmp" || {
        rm -f "$report_tmp"
        return 1
    }
    mv "$report_tmp" "$REPORT_FILE"
}

finalize() {
    local ended_at
    local result_status
    local output_sha256
    if ((FINALIZED)); then
        return
    fi
    FINALIZED=1
    if [[ -n "$PROGRESS_DONE_TMP" ]]; then
        : > "$PROGRESS_DONE_TMP" 2>/dev/null || true
    fi
    if [[ -n "$PROGRESS_PID" ]]; then
        kill "$PROGRESS_PID" 2>/dev/null || true
        wait "$PROGRESS_PID" 2>/dev/null || true
        printf '\r\033[2K' >&2
    fi
    ended_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    if ! prepare_report_outputs "$OUTPUT_TMP"; then
        cp "$OUTPUT_TMP" "$FINAL_OUTPUT_TMP" || true
        cp "$OUTPUT_TMP" "$STATUS_OUTPUT_TMP" || true
    fi
    result_status="$(skill_result_status)"
    output_sha256="$(sha256_file "$FINAL_OUTPUT_TMP" 2>/dev/null || true)"
    if ! write_report "$result_status" "$ended_at" "$output_sha256"; then
        printf 'ERROR: could not write skill report: %s\n' "$REPORT_FILE" >&2
        result_status="Fail"
    fi
    if ! write_status_file "$result_status" "$ended_at" "$TARGET_REL"; then
        printf 'ERROR: could not update skill status: %s\n' "$STATUS_FILE" >&2
    fi
    rm -f "$OUTPUT_TMP"
    rm -f "$FINAL_OUTPUT_TMP" "$STATUS_OUTPUT_TMP"
    rm -f "$AGENT_EXIT_TMP" "$PROGRESS_DONE_TMP"
    printf '\nReport: %s\n' "$REPORT_FILE" >&2
    printf 'Status: %s\n' "$result_status" >&2
}
trap finalize EXIT
trap 'RUN_STATUS=INTERRUPTED; EXIT_CODE=130; exit 130' INT TERM

PROMPT=$(cat <<EOF
Run the local ${SKILL} skill for the authoring project.

Read and follow the complete skill instructions at:
${SKILL_FILE}

Apply the skill to this target:
${TARGET_ABS}

The client policy is mandatory: task environments set
network_mode = "public" for the Oracle and the agent trials, and each final
runtime image must be at most 2 GB (2,000,000,000 bytes). The submission uses
that runtime image for verification; an optional local two-image fixture is
subject to the same limit if used.
Preserve those constraints. Public network exists so the Oracle and the agent
can reach scientific databases and tools over HTTP; it is not a licence to
install dependencies at run time. Keep Python libraries and code vendored in
the image and do not replace a vendored dependency with a run-time install.

Docker access mode for this invocation is ${DOCKER_ACCESS}. For a Codex run,
Docker access is provided through the wrapper-selected ${CODEX_SANDBOX}
sandbox. For a Claude Code run, the wrapper selects ${CLAUDE_PERMISSION_MODE}
when Docker access is not off. Use Docker only for read-only inspection of the
target task image, the static image validation the skill defines, and cleanup. The skill cannot repair a host Docker daemon or grant access to
an unapproved remote daemon.

Use the required skill workflow and evidence rules. For task-fixer, make the
smallest task-local edits needed and return only the final handoff, without
planning, tool transcripts, or duplicated status sections. For task-review and
trajectory-review, do not edit the task; return the complete requested scorecard
or verdict as Markdown, not only a summary or a file path. Start the final
response with the exact
Markdown line **Status:** PASS when the skill passes and **Status:** FAIL when
it does not.
Do not modify skill-reports/ or skill-status.md; the wrapper saves your final
Markdown output and updates those files.
EOF
)

progress_indicator() {
    local done_file="$1"
    local frame_index=0
    local elapsed=0
    local frames='|/-\\'

    while [[ ! -e "$done_file" ]]; do
        if [[ -t 2 ]]; then
            printf '\r\033[2K[%s] Agent working... (%ss)' \
                "${frames:$frame_index:1}" "$elapsed" >&2
            frame_index=$(( (frame_index + 1) % 4 ))
        elif (( elapsed == 0 || elapsed % 10 == 0 )); then
            printf 'Agent still working... (%ss elapsed)\n' "$elapsed" >&2
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done

    if [[ -t 2 ]]; then
        printf '\r\033[2K' >&2
    else
        printf 'Agent finished after %ss.\n' "$elapsed" >&2
    fi
}

invoke_runner() {
    if [[ "$RUNNER" = codex ]]; then
        "$RUNNER_BIN" \
            --ask-for-approval never \
            exec \
            -C "$REPO_ROOT" \
            --sandbox "$CODEX_SANDBOX" \
            --skip-git-repo-check \
            "$PROMPT"
    else
        (
            cd -- "$REPO_ROOT" || exit 1
            CLAUDE_ARGS=(
                --print
                --no-session-persistence
                --permission-mode "$CLAUDE_PERMISSION_MODE"
                --add-dir "$REPO_ROOT"
            )
            if ((CLAUDE_ALLOW_DANGEROUS)); then
                CLAUDE_ARGS+=(--allow-dangerously-skip-permissions)
            fi
            # --add-dir is variadic, so the prompt has to be separated from the
            # option list with --; otherwise it is parsed as another directory and
            # claude exits with "Input must be provided ... when using --print".
            "$RUNNER_BIN" "${CLAUDE_ARGS[@]}" -- "$PROMPT"
        )
    fi
}

if ((DRY_RUN)); then
    printf 'DRY RUN: would invoke %s for %s on %s\n' "$RUNNER" "$SKILL" "$TARGET_ABS" | tee "$OUTPUT_TMP"
    RUN_STATUS=DRY_RUN
    EXIT_CODE=0
    exit 0
fi

(
    set +e
    invoke_runner 2>&1 | tee "$OUTPUT_TMP"
    printf '%s\n' "${PIPESTATUS[0]}" > "$AGENT_EXIT_TMP"
) &
AGENT_PIPELINE_PID=$!
progress_indicator "$PROGRESS_DONE_TMP" &
PROGRESS_PID=$!

wait "$AGENT_PIPELINE_PID" || true
: > "$PROGRESS_DONE_TMP"
wait "$PROGRESS_PID" || true
EXIT_CODE="$(sed -n '1p' "$AGENT_EXIT_TMP")"
[[ "$EXIT_CODE" =~ ^[0-9]+$ ]] || EXIT_CODE=1

if ! prepare_report_outputs "$OUTPUT_TMP"; then
    cp "$OUTPUT_TMP" "$FINAL_OUTPUT_TMP" || true
    cp "$OUTPUT_TMP" "$STATUS_OUTPUT_TMP" || true
fi

if ((EXIT_CODE == 0)); then
    RUN_STATUS=COMPLETED
    RESULT_STATUS="$(skill_result_status)"
    if [[ "$RESULT_STATUS" = "Fail" ]]; then
        EXIT_CODE=1
        RUN_STATUS=FAIL
    fi
else
    RUN_STATUS=FAIL
fi
exit "$EXIT_CODE"
