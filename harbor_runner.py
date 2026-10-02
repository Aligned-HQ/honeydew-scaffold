#!/usr/bin/env python3
"""Run a Harbor task on Modal through an Oracle gate and three agent jobs.

The vendored runner first executes the Oracle and inspects its trial results.
The three model jobs are started only when the Oracle trial finishes with a
passing reward. Each agent then gets one Harbor job for this task with the
requested number of attempts, using the same default Harbor concurrency for
each model. Harbor -- not this script -- owns the attempt fan-out, so the run
is resumable via `harbor jobs resume` and every attempt for an agent lives in a
single job for easy pass@k aggregation.

Workbench remote mode is the default execution path; use `--no-remote` for a
local Modal run. `--quick` is a separate host-local smoke trial: it invokes an
already-installed `claude` or `codex` executable once, verifies the resulting
output in a local Docker container, and never contacts Workbench or Modal.
Preflight requires explicit
`FROM --platform=linux/amd64` declarations in task Dockerfiles and rejects
prebuilt image manifests that are not a single Linux/amd64 image. The source
task must declare `[environment].network_mode = "public"`; normal runs create a
separate Oracle snapshot and agent snapshot, both with
`network_mode = "public"`.

Examples:
    # Build the task image and run the reference solution/verifier locally:
    ./harbor_runner.py ./task --no-remote --smoke-test

    # Submit remotely (the default), then monitor the Oracle and agents:
    ./harbor_runner.py ./task

    # Run locally on Modal:
    ./harbor_runner.py ./task --no-remote

    # Preview the local Harbor commands without running them:
    ./harbor_runner.py ./task --no-remote --dry-run

    # Run one local trial with the installed Codex CLI (no Workbench or Modal):
    ./harbor_runner.py ./task --quick --quick-agent codex

    # Let quick mode choose an installed CLI (Codex is preferred, then Claude):
    ./harbor_runner.py ./task --quick

    # Resume an interrupted run (reuse the printed --run-id):
    ./harbor_runner.py ./task --run-id 20260528-101500-a1b2c3d4e5 --resume

    # Override agents / concurrency:
    ./harbor_runner.py ./task \\
        --run claude-code:anthropic/claude-opus-5:claude:3 \\
        --run codex:openai/gpt-6-astra:codex:3

API credentials should be supplied to Modal with named secrets, for example
`--modal-secret openai-api-key`. Secret values are never written by this
runner; only the Modal secret names are placed in the Harbor command.

Each live run claims a unique Modal App name in a run manifest. The Oracle and
agent jobs for that run share the owned app, while cleanup stops only that app
on normal completion, Ctrl-C, or SIGTERM. A second live process cannot reuse a
claimed run ID without `--resume`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import math
import os
import posixpath
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - exercised only on minimal host installs
    Console = None  # type: ignore[assignment,misc]
    Live = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment]
    Progress = None  # type: ignore[assignment,misc]
    BarColumn = DownloadColumn = SpinnerColumn = TaskProgressColumn = None  # type: ignore[assignment]
    TextColumn = TimeRemainingColumn = TransferSpeedColumn = None  # type: ignore[assignment]
    Table = Text = None  # type: ignore[assignment,misc]


RICH_AVAILABLE = Console is not None


def runner_console(stream: object | None = None, *, stderr: bool = False) -> object | None:
    """Return a Rich console bound to the requested stream when Rich is installed."""
    if not RICH_AVAILABLE:
        return None
    if stream is None:
        return Console(stderr=stderr, soft_wrap=True)
    return Console(file=stream, soft_wrap=True)


def _rich_text(value: str) -> object:
    """Keep service-provided text from being interpreted as Rich markup."""
    return Text(value) if RICH_AVAILABLE else value


def print_runner_panel(
    title: str,
    lines: list[str],
    *,
    stream: object | None = None,
    border_style: str = "cyan",
) -> None:
    """Print a compact structured block, with a plain-text fallback."""
    console = runner_console(stream)
    if console is None:
        for line in lines:
            print(line, file=stream if stream is not None else sys.stdout, flush=True)
        return

    body = Table.grid(padding=(0, 1))
    body.add_column()
    for line in lines:
        body.add_row(_rich_text(line))
    console.print(Panel(body, title=title, border_style=border_style))


# Local Harbor defaults: four attempts per agent with one worker. Workbench's
# remote concurrency is a fan-out multiplier, so use a separate remote
# schedule below to fit the same coverage inside its 60-minute ceiling.
DEFAULT_CONCURRENCY = 1
DEFAULT_REPEATS = 4
REMOTE_DEFAULT_CONCURRENCY = 4
REMOTE_DEFAULT_REPEATS = 1
MODAL_PLATFORM = "linux/amd64"
# Client policy: the Oracle and the agent trials both run with public network.
DEFAULT_NETWORK_MODE = "public"
SUPPORTED_NETWORK_MODES = ("public", "no-network")
MODAL_APP_NAME_PREFIX = "honeydew"
MODAL_RUN_MANIFEST_SUFFIX = ".modal-run.json"
QUICK_AGENT_CHOICES = ("auto", "claude", "claude-code", "codex")
QUICK_DEFAULT_AGENT_TIMEOUT_SEC = 1800.0
QUICK_DEFAULT_VERIFIER_TIMEOUT_SEC = 300.0
QUICK_AGENT_PROGRESS_FPS = 8.0
QUICK_AGENT_PROGRESS_FRAMES = ("|", "/", "-", "\\")
DOCKERFILE_FROM_RE = re.compile(
    r"^\s*FROM(?:\s+--platform=(?P<platform>\S+))?\s+(?P<image>\S+)",
    re.IGNORECASE,
)

# Keep default agent jobs at the same Harbor concurrency so model comparisons
# run with the same scheduling pressure. Use --n-concurrent to override all
# defaults together, or --run AGENT:MODEL:LABEL:N_CONCURRENT for an explicit
# per-model exception.
DEFAULT_RUNS: tuple[tuple[str, str, str, int], ...] = (
    ("claude-code", "anthropic/claude-opus-5", "claude-opus-5", DEFAULT_CONCURRENCY),
    ("codex", "openai/gpt-6-astra", "codex-gpt-6-astra", DEFAULT_CONCURRENCY),
    (
        "antigravity",
        "google/gemini-3.8-flash",
        "gemini-3-8-flash",
        DEFAULT_CONCURRENCY,
    ),
)

REMOTE_TERMINAL_STATES = {
    "ORACLE_FAILED",
    "ORACLE_EXCEPTION",
    "COMPLETE",
    "CANCELED",
    "EXPIRED",
    "ERROR",
}
REMOTE_ACTIVE_STATES = {
    "CREATED",
    "UPLOADING",
    "VALIDATING",
    "QUEUED",
    "ORACLE_RUNNING",
    "AGENTS_QUEUED",
    "AGENTS_RUNNING",
    "FINALIZING",
}
REMOTE_MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
REMOTE_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
REMOTE_TRAJECTORY_ARCHIVE_SCOPE = "trajectories-only"
REMOTE_EXECUTION_POLICY_ID = "scientific-offline-v1"
REMOTE_DEFAULT_PROGRESS_INTERVAL_SECONDS = 30.0
REMOTE_ARCHIVE_PROGRESS_BYTES = 10 * 1024 * 1024
REMOTE_TRANSFER_TIMEOUT_SECONDS = 30 * 60
REMOTE_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
REMOTE_REQUEST_RETRY_ATTEMPTS = 5
REMOTE_REQUEST_RETRY_MIN_SECONDS = 1.0
REMOTE_REQUEST_RETRY_MAX_SECONDS = 30.0
LOCAL_DEFAULT_PROGRESS_INTERVAL_SECONDS = 30.0
ORACLE_SPINNER_FRAMES = ("|", "/", "-", "\\")
REMOTE_AGENT_CONFIGS = {
    ("claude-code", "anthropic/claude-opus-5"): "claude-opus",
    ("codex", "openai/gpt-6-astra"): "codex-gpt-6-astra",
    # Previous Codex request signature; the server rewrites it to Astra.
    ("codex", "openai/gpt-5.6-sol"): "codex-gpt-5-6-sol",
    ("antigravity", "google/gemini-3.8-flash"): "gemini-3-8-flash",
}
REMOTE_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "harbor-jobs",
    "jobs",
    "trajectories",
    "reports",
    "report",
    "caches",
    ".runner-logs",
    "node_modules",
}
REMOTE_SECRET_NAME_RE = re.compile(
    r"(^\.env(?:\..*)?$|credentials?|service[-_.]?account|private[-_.]?key|modal[-_.]?token|\.(?:pem|p12|pfx|key)$)",
    re.IGNORECASE,
)
REMOTE_SECRET_CONTENT_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bmodal_[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:ANTHROPIC|OPENAI|GEMINI|GOOGLE|MODAL)_?(?:API_)?KEY\s*[:=]\s*[^\s$<{[]+", re.IGNORECASE),
    re.compile(r"\bMODAL_TOKEN_(?:ID|SECRET)\s*[:=]\s*[^\s$<{[]+", re.IGNORECASE),
)
REMOTE_HOST_PATH_RE = re.compile(r"(?:^|[\s\"'=])(?:/(?:Users|Volumes)/|[A-Za-z]:\\Users\\)")

RUNNING_PROCESSES: dict[str, subprocess.Popen[str]] = {}
PROCESS_LOCK = threading.Lock()
SHUTDOWN_MODAL_ON_INTERRUPT = False
SHUTDOWN_MODAL_COMPLETED = False
MODAL_CLEANUP_ARMED = False
MODAL_APP_NAME: str | None = None

DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class AgentSpec:
    agent: str
    model: str
    label: str
    n_concurrent: int


@dataclass(frozen=True)
class JobSpec:
    task_root: Path
    num_tasks: int
    agent: str
    model: str
    label: str
    n_concurrent: int
    repeats: int
    job_name: str
    jobs_dir: Path
    job_dir: Path
    command: list[str]
    runner_log: Path
    resume: bool
    completion_grace_sec: float
    progress_interval_sec: float


@dataclass
class JobResult:
    agent: str
    model: str
    label: str
    job_name: str
    n_trials_expected: int
    returncode: int
    elapsed_sec: float
    job_dir: str
    runner_log: str
    resumed: bool


@dataclass(frozen=True)
class QuickTrialPaths:
    """Paths for one host-local trial and its staged public workspace."""

    job_name: str
    job_dir: Path
    trial_dir: Path
    workspace_dir: Path
    runner_log: Path


@dataclass(frozen=True)
class JobProgress:
    expected_trials: int
    result_files: int
    finished_trials: int
    passed_trials: int
    failed_trials: int
    errored_trials: int
    complete_tasks: int
    total_tasks_seen: int


@dataclass
class TrialArchive:
    job_name: str
    agent: str
    label: str
    model: str
    job_dir: Path
    runner_log: Path
    trial_dir: Path
    result_path: Path
    task_path: Path
    finished: bool
    reward: float | None
    exception_type: str | None


@dataclass(frozen=True)
class OracleSortJobSpec:
    task_root: Path
    num_tasks: int
    job_name: str
    jobs_dir: Path
    job_dir: Path
    command: list[str]
    runner_log: Path
    resume: bool
    completion_grace_sec: float
    progress_interval_sec: float


@dataclass(frozen=True)
class OracleTrialResult:
    task_path: Path
    finished: bool
    reward: float | None
    exception_type: str | None
    result_path: Path


@dataclass(frozen=True)
class OracleArchive:
    task_path: Path
    job_dir: Path
    trial_dir: Path
    result_path: Path
    finished: bool
    reward: float | None
    exception_type: str | None
    mtime: float


@dataclass
class TaskArchiveState:
    task_path: Path
    archives: list[TrialArchive]
    oracle: OracleArchive | None
    missing_jobs: list[str]
    unfinished: list[TrialArchive]
    exception_archives: list[TrialArchive]
    short_jobs: list[str]
    finished_counts_by_job: dict[str, int]


@dataclass
class OracleSortMoveResult:
    task: str
    status: str
    reward: float | None
    source: str
    destination: str | None
    result_path: str | None
    error: str | None = None


def _parse_dotenv_value(raw: str, *, path: Path, line_number: int) -> str:
    """Parse the small, dependency-free .env syntax used by the runner."""
    value = raw.strip()
    if not value:
        return ""

    def valid_suffix(suffix: str) -> bool:
        suffix = suffix.strip()
        return not suffix or suffix.startswith("#")

    if value[0] == "'":
        closing = value.find("'", 1)
        if closing < 0 or not valid_suffix(value[closing + 1 :]):
            raise SystemExit(f"error: invalid quoted value in {path}:{line_number}")
        return value[1:closing]
    if value[0] == '"':
        chars: list[str] = []
        escaped = False
        closing = -1
        for index, char in enumerate(value[1:], start=1):
            if escaped:
                chars.append({"n": "\n", "r": "\r", "t": "\t"}.get(char, f"\\{char}"))
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                closing = index
                break
            else:
                chars.append(char)
        if closing < 0 or escaped or not valid_suffix(value[closing + 1 :]):
            raise SystemExit(f"error: invalid quoted value in {path}:{line_number}")
        return "".join(chars)

    comment_start = -1
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            comment_start = index
            break
    return value[:comment_start].rstrip() if comment_start >= 0 else value


def read_dotenv_values(path: Path) -> dict[str, str]:
    """Read the small, dependency-free .env syntax used by the runner."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"error: cannot read env file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not DOTENV_KEY_RE.fullmatch(key):
            raise SystemExit(f"error: invalid .env assignment in {path}:{line_number}")
        values[key] = _parse_dotenv_value(raw_value, path=path, line_number=line_number)
    return values


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load a local .env file without overwriting explicitly exported values."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        candidates.extend(
            (
                Path.cwd() / ".env",
                Path(__file__).resolve().parent / ".env",
            )
        )
    env_path = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
    if env_path is None:
        return None

    for key, value in read_dotenv_values(env_path).items():
        if key not in os.environ:
            os.environ[key] = value
    return env_path


def is_task_dir(path: Path) -> bool:
    return (path / "task.toml").is_file() and (path / "instruction.md").is_file()


def resolve_single_task(path: Path) -> Path:
    """Resolve exactly one Harbor task; never treat a directory as a dataset."""
    path = path.resolve()
    if not path.is_dir():
        raise SystemExit(f"error: {path} is not a directory")
    if not is_task_dir(path):
        raise SystemExit(
            f"error: {path} is not a single Harbor task; "
            "pass the repo's task/ directory containing task.toml and instruction.md"
        )
    return path


def set_snapshot_network_mode(task_root: Path, network_mode: str) -> None:
    """Set the generated snapshot's network policy without touching the source task."""
    if network_mode not in set(SUPPORTED_NETWORK_MODES):
        expected = " or ".join(f'"{mode}"' for mode in SUPPORTED_NETWORK_MODES)
        raise SystemExit(
            f"error: unsupported snapshot network_mode {network_mode!r}; "
            f"expected {expected}"
        )
    config_path = task_root / "task.toml"
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_environment = False
    replaced = False
    for index, line in enumerate(lines):
        section = re.match(r"^\s*\[([^\[].*?)\]\s*(?:#.*)?(?:\r?\n)?$", line)
        if section:
            in_environment = section.group(1).strip() == "environment"
            continue
        if not in_environment:
            continue
        if re.match(r"^\s*network_mode\s*=", line):
            newline = "\n" if line.endswith("\n") else ""
            comment = ""
            content = line.rstrip("\r\n")
            if "#" in content:
                comment_text = content.split("#", 1)[1].strip()
                comment = f"  # {comment_text}" if comment_text else "  #"
            lines[index] = f'network_mode = "{network_mode}"{comment}{newline}'
            replaced = True
            break
    if not replaced:
        raise SystemExit(
            f"error: {config_path} must contain [environment].network_mode "
            "so the runner can create the Oracle and agent snapshots"
        )
    config_path.write_text("".join(lines), encoding="utf-8")
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    environment = parsed.get("environment")
    if not isinstance(environment, dict) or environment.get("network_mode") != network_mode:
        raise SystemExit(
            f'error: failed to set [environment].network_mode="{network_mode}" '
            f"in generated snapshot {config_path}"
        )


def snapshot_task_root(
    task_root: Path,
    jobs_dir: Path,
    run_id: str,
    snapshot_label: str = "task-snapshot",
    *,
    network_mode: str | None = DEFAULT_NETWORK_MODE,
) -> Path:
    """Create an immutable copy of the single task for this Harbor run."""

    snapshot_root = jobs_dir.resolve() / f"{run_id}.{snapshot_label}"
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        ".runner-logs",
    )
    shutil.copytree(task_root, snapshot_root, symlinks=True, ignore=ignore)
    if network_mode is not None:
        set_snapshot_network_mode(snapshot_root, network_mode)
    stable_time = time.time() - 60.0
    for path in snapshot_root.rglob("*"):
        if not path.is_symlink():
            os.utime(path, (stable_time, stable_time))
    os.utime(snapshot_root, (stable_time, stable_time))
    time.sleep(2.0)
    return snapshot_root


def slug(value: str) -> str:
    value = value.strip().lower().replace("/", "-")
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "job"


class RemoteInputError(Exception):
    """A local request/archive error that maps to the documented exit code 2."""


class RemoteClientError(Exception):
    """A sanitized HTTP/service failure from the Workbench Harbor API."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        *,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.headers = headers or {}
        self.retryable = retryable


def remote_service_base(raw: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", value, re.IGNORECASE):
        raise RemoteInputError("--service-url must be an http(s) URL")
    parsed = urllib.parse.urlsplit(value)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise RemoteInputError("--service-url must not contain credentials, a query string, or a fragment")
    path_value = parsed.path.rstrip("/")
    if path_value.endswith("/v1"):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path_value, "", ""))
    path_value = f"{path_value}/v1" if path_value else "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path_value, "", ""))


def remote_url(base: str, endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    if base.endswith("/v1") and endpoint.startswith("/v1"):
        return f"{base}{endpoint[3:]}"
    return f"{base}{endpoint}"


def _remote_json(raw: bytes) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _remote_response_headers(response: object) -> dict[str, str]:
    headers = getattr(response, "headers", {})
    return {str(key).lower(): str(value) for key, value in headers.items()}


def remote_json_request(
    method: str,
    url: str,
    token: str,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, object], dict[str, str]]:
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _remote_json(response.read()), _remote_response_headers(response)
    except urllib.error.HTTPError as error:
        response_headers = _remote_response_headers(error)
        body = error.read()
        if error.code == 304:
            return 304, {}, response_headers
        parsed = _remote_json(body)
        details = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
        code = str(details.get("code") or f"http_{error.code}")
        message = str(details.get("message") or f"Workbench Harbor API returned HTTP {error.code}")
        raise RemoteClientError(error.code, code, message[:500], response_headers) from None
    except (urllib.error.URLError, OSError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        # Report transport diagnostics without exposing URLs or proxy credentials
        # that may be embedded in the exception's free-form message.
        detail = type(reason).__name__
        if isinstance(reason, OSError) and isinstance(reason.errno, int):
            detail += f" (errno {reason.errno})"
        raise RemoteClientError(
            0,
            "network",
            f"Could not reach the Workbench Harbor service: {detail}. "
            f"Python interpreter: {sys.executable}",
            retryable=True,
        ) from error


class UploadProgressReader:
    """File-like request body that reports bytes as urllib hands them to HTTP."""

    def __init__(self, data: bytes | Path, on_read: Callable[[int], None]) -> None:
        self._file = data.open("rb") if isinstance(data, Path) else None
        self._data = memoryview(data) if isinstance(data, bytes) else None
        self._length = len(data) if isinstance(data, bytes) else data.stat().st_size
        self._position = 0
        self._on_read = on_read

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._position >= self._length:
            return b""
        if self._file is not None:
            chunk = self._file.read(1024 * 1024 if size is None or size < 0 else size)
            self._position += len(chunk)
        else:
            assert self._data is not None
            end = self._length if size is None or size < 0 else min(self._length, self._position + size)
            chunk = self._data[self._position:end].tobytes()
            self._position = end
        self._on_read(self._position)
        return chunk

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _remote_upload_once(url: str, archive: bytes | Path, headers: dict[str, str]) -> None:
    archive_size = len(archive) if isinstance(archive, bytes) else archive.stat().st_size
    if archive_size < 1 or archive_size > REMOTE_MAX_BUNDLE_BYTES:
        raise RemoteInputError(f"compressed task bundle exceeds {REMOTE_MAX_BUNDLE_BYTES} bytes")
    request_headers = {str(key): str(value) for key, value in headers.items()}
    if not any(key.lower() == "content-length" for key in request_headers):
        # Explicitly preserve a fixed-length PUT. Without this header urllib
        # falls back to chunked transfer for a file-like body, which many
        # signed object-storage URLs reject.
        request_headers["Content-Length"] = str(archive_size)

    console = runner_console()
    progress = None
    progress_task = None
    uploaded = 0
    fallback_next_report = max(1, archive_size // 20)

    def report(completed: int) -> None:
        nonlocal fallback_next_report, uploaded
        uploaded = completed
        if progress is not None and progress_task is not None:
            progress.update(progress_task, completed=completed)
        elif not RICH_AVAILABLE:
            # Keep redirected logs useful on a host where Rich is not present.
            if completed >= fallback_next_report or completed == archive_size:
                print(
                    f"remote upload: {completed}/{archive_size} bytes "
                    f"({100.0 * completed / archive_size:.1f}%)",
                    flush=True,
                )
                fallback_next_report = min(
                    archive_size, fallback_next_report + max(1, archive_size // 20)
                )

    if RICH_AVAILABLE and console is not None:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=bool(getattr(console, "is_terminal", False)),
            refresh_per_second=10,
        )
        progress_task = progress.add_task("Uploading task bundle", total=archive_size)
        progress.start()
    else:
        print(f"remote upload: uploading {archive_size} bytes", flush=True)

    # A signed URL is the authorization for this one object. The Workbench
    # bearer token is intentionally not sent to Storage.
    request_body = UploadProgressReader(archive, report)
    request = urllib.request.Request(url, data=request_body, headers=request_headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=REMOTE_TRANSFER_TIMEOUT_SECONDS) as response:
            if response.status < 200 or response.status >= 300:
                raise RemoteClientError(response.status, "upload_failed", "The task bundle upload failed.")
        if progress is not None and progress_task is not None:
            progress.update(progress_task, completed=archive_size)
        elif not RICH_AVAILABLE and uploaded != archive_size:
            report(archive_size)
    except urllib.error.HTTPError as error:
        raise RemoteClientError(
            error.code,
            "upload_failed",
            "The task bundle upload failed.",
            _remote_response_headers(error),
        ) from None
    except (urllib.error.URLError, OSError) as error:
        raise RemoteClientError(
            0,
            "network",
            "The task bundle upload could not reach Storage.",
            retryable=True,
        ) from error
    finally:
        request_body.close()
        if progress is not None:
            progress.stop()


def remote_upload(url: str, archive: bytes | Path, headers: dict[str, str]) -> None:
    """Upload a bundle, retrying transient storage failures."""
    for attempt in range(REMOTE_REQUEST_RETRY_ATTEMPTS):
        try:
            _remote_upload_once(url, archive, headers)
            return
        except RemoteClientError as error:
            if not remote_error_is_retryable(error) or attempt + 1 >= REMOTE_REQUEST_RETRY_ATTEMPTS:
                raise
            fallback = remote_backoff_delay(attempt)
            wait = (
                remote_retry_after(error.headers, fallback)
                if error.status in REMOTE_RETRYABLE_HTTP_STATUSES
                else fallback
            )
            print(
                "remote upload temporarily unavailable; "
                f"retrying in {wait:g}s (attempt {attempt + 1}/{REMOTE_REQUEST_RETRY_ATTEMPTS})",
                flush=True,
            )
            time.sleep(wait)


def remote_archive_name_allowed(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return not any(part in REMOTE_EXCLUDED_PARTS or part.startswith("._") for part in relative.parts)


def remote_reject_sensitive_name(path: Path) -> None:
    if REMOTE_SECRET_NAME_RE.search(path.name):
        raise RemoteInputError(f"task bundle contains a credential-looking file: {path.name}")


def remote_scan_file(path: Path) -> None:
    remote_reject_sensitive_name(path)
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            return
        data = path.read_bytes()
    except OSError as error:
        raise RemoteInputError(f"could not read task file {path}: {error}") from error
    if b"\x00" in data:
        return
    text = data.decode("utf-8", errors="ignore")
    if REMOTE_HOST_PATH_RE.search(text):
        raise RemoteInputError(f"task file contains a host-specific author-machine path: {path}")
    if any(pattern.search(text) for pattern in REMOTE_SECRET_CONTENT_RES):
        raise RemoteInputError(f"task file appears to contain a provider or Modal credential: {path}")


def build_remote_task_bundle(task_root: Path) -> tuple[bytes, str, int]:
    """Create the exact immutable tar.gz sent to Workbench.

    This deliberately avoids ``tar.add(..., recursive=True)`` so local jobs,
    reports, macOS metadata, and credentials cannot enter the request by
    accident. Symlinks are retained only when their target stays under the
    submitted task root; the server validates the same boundary again.
    """
    root = task_root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", root.name):
        raise RemoteInputError("the task directory name must contain only letters, digits, '.', '_' or '-'")
    required = (
        root / "task.toml",
        root / "instruction.md",
        root / "solution",
        root / "tests",
        root / "environment",
    )
    missing = [str(item.relative_to(root)) for item in required if not item.exists()]
    if missing:
        raise RemoteInputError(f"task is missing required Harbor paths: {', '.join(missing)}")

    output = io.BytesIO()
    try:
        with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            archive.add(root, arcname=root.name, recursive=False)
            for item in sorted(root.rglob("*")):
                if not remote_archive_name_allowed(item, root):
                    continue
                remote_reject_sensitive_name(item)
                if item.is_symlink():
                    target = (item.parent / os.readlink(item)).resolve()
                    try:
                        target.relative_to(root)
                    except ValueError as error:
                        raise RemoteInputError(f"task symlink leaves the task root: {item}") from error
                elif item.is_file():
                    remote_scan_file(item)
                archive.add(item, arcname=f"{root.name}/{item.relative_to(root).as_posix()}", recursive=False)
    except (OSError, tarfile.TarError) as error:
        if isinstance(error, RemoteInputError):
            raise
        raise RemoteInputError(f"could not create the task bundle: {error}") from error
    data = output.getvalue()
    if len(data) > REMOTE_MAX_BUNDLE_BYTES:
        raise RemoteInputError(f"compressed task bundle exceeds {REMOTE_MAX_BUNDLE_BYTES} bytes")
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return data, digest, len(data)


def build_remote_task_bundle_file(task_root: Path, output_path: Path) -> tuple[Path, str, int]:
    """Create and hash a task bundle without retaining the compressed bytes."""
    root = task_root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", root.name):
        raise RemoteInputError("the task directory name must contain only letters, digits, '.', '_' or '-'")
    required = (
        root / "task.toml",
        root / "instruction.md",
        root / "solution",
        root / "tests",
        root / "environment",
    )
    missing = [str(item.relative_to(root)) for item in required if not item.exists()]
    if missing:
        raise RemoteInputError(f"task is missing required Harbor paths: {', '.join(missing)}")

    output_path = output_path.resolve()
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as output:
            with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT, dereference=False) as archive:
                archive.add(root, arcname=root.name, recursive=False)
                for item in sorted(root.rglob("*")):
                    if not remote_archive_name_allowed(item, root):
                        continue
                    remote_reject_sensitive_name(item)
                    if item.is_symlink():
                        target = (item.parent / os.readlink(item)).resolve()
                        try:
                            target.relative_to(root)
                        except ValueError as error:
                            raise RemoteInputError(f"task symlink leaves the task root: {item}") from error
                    elif item.is_file():
                        remote_scan_file(item)
                    archive.add(item, arcname=f"{root.name}/{item.relative_to(root).as_posix()}", recursive=False)
    except RemoteInputError:
        try:
            output_path.unlink()
        except OSError:
            pass
        raise
    except (OSError, tarfile.TarError) as error:
        raise RemoteInputError(f"could not create the task bundle: {error}") from error

    size = output_path.stat().st_size
    if size > REMOTE_MAX_BUNDLE_BYTES:
        raise RemoteInputError(f"compressed task bundle exceeds {REMOTE_MAX_BUNDLE_BYTES} bytes")
    digest = hashlib.sha256()
    with output_path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return output_path, f"sha256:{digest.hexdigest()}", size


def remote_agent_payload(args: argparse.Namespace) -> list[dict[str, object]]:
    default_concurrency = args.n_concurrent or args.default_concurrency
    try:
        specs = (
            [parse_agent_spec(item, default_concurrency) for item in args.run]
            if args.run
            else [
                AgentSpec(agent, model, label, default_concurrency)
                for agent, model, label, _ in DEFAULT_RUNS
            ]
        )
    except SystemExit as error:
        raise RemoteInputError(str(error)) from error
    agents: list[dict[str, object]] = []
    for spec in specs:
        agent_id = REMOTE_AGENT_CONFIGS.get((spec.agent, spec.model))
        if agent_id is None:
            raise RemoteInputError(
                f"remote mode only supports the server-approved agent/model pairs; got {spec.agent}:{spec.model}"
            )
        if spec.n_concurrent < 1 or spec.n_concurrent > 5:
            raise RemoteInputError(f"remote concurrency for {agent_id} must be between 1 and 5")
        agents.append({
            "id": agent_id,
            "agent": spec.agent,
            "model": spec.model,
            "concurrency": spec.n_concurrent,
        })
    if not agents:
        raise RemoteInputError("remote mode requires at least one approved agent")
    total_trials = args.repeats * sum(int(agent["concurrency"]) for agent in agents)
    if total_trials > 30:
        raise RemoteInputError("remote mode exceeds the server's 30-trial configuration limit")
    return agents


def remote_execution_payload(args: argparse.Namespace, agents: list[dict[str, object]]) -> dict[str, object]:
    """Build the server-selected execution contract for one uploaded task."""
    return {
        "attempts": args.repeats,
        "oracle_pass_threshold": args.pass_threshold,
        "execution_policy_id": REMOTE_EXECUTION_POLICY_ID,
        "agents": agents,
    }


def remote_state_path(jobs_dir: Path, run_id: str) -> Path:
    return jobs_dir.resolve() / run_id / "service-run.json"


def remote_request_state_path(jobs_dir: Path, local_id: str) -> Path:
    return jobs_dir.resolve() / f"{local_id}.service-request.json"


def load_remote_state(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RemoteInputError(f"could not read remote run state {path}: {error}") from error
    if not isinstance(value, dict):
        raise RemoteInputError(f"remote run state is not a JSON object: {path}")
    return value


def save_remote_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def remote_retry_after(headers: dict[str, str], fallback: float) -> float:
    raw = headers.get("retry-after")
    try:
        value = float(raw) if raw is not None else fallback
    except ValueError:
        value = fallback
    return max(0.0, min(value, 60.0))


def remote_error_is_retryable(error: RemoteClientError) -> bool:
    return error.retryable or error.status in REMOTE_RETRYABLE_HTTP_STATUSES


def remote_backoff_delay(
    attempt: int,
    *,
    minimum: float = REMOTE_REQUEST_RETRY_MIN_SECONDS,
    maximum: float = REMOTE_REQUEST_RETRY_MAX_SECONDS,
) -> float:
    return min(maximum, max(0.0, minimum) * (2**attempt))


def remote_json_request_with_retry(
    method: str,
    url: str,
    token: str,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    operation: str = "remote request",
) -> tuple[int, dict[str, object], dict[str, str]]:
    """Retry a bounded set of safe API requests after transient failures."""
    for attempt in range(REMOTE_REQUEST_RETRY_ATTEMPTS):
        try:
            return remote_json_request(
                method,
                url,
                token,
                payload=payload,
                headers=headers,
                timeout=timeout,
            )
        except RemoteClientError as error:
            if not remote_error_is_retryable(error) or attempt + 1 >= REMOTE_REQUEST_RETRY_ATTEMPTS:
                raise
            fallback = remote_backoff_delay(attempt)
            wait = (
                remote_retry_after(error.headers, fallback)
                if error.status in REMOTE_RETRYABLE_HTTP_STATUSES
                else fallback
            )
            print(
                f"{operation} temporarily unavailable; retrying in {wait:g}s "
                f"(attempt {attempt + 1}/{REMOTE_REQUEST_RETRY_ATTEMPTS})",
                flush=True,
            )
            time.sleep(wait)
    raise AssertionError("remote request retry loop did not return or raise")


REMOTE_STATE_MESSAGES = {
    "CREATED": "run record created",
    "UPLOADING": "waiting for the task bundle upload",
    "VALIDATING": "Workbench is validating the task bundle and execution policy",
    "QUEUED": "queued; waiting for a Harbor worker",
    "ORACLE_RUNNING": "Oracle is running; agent jobs wait for a passing Oracle result",
    "ORACLE_FAILED": "Oracle did not meet the pass threshold; agent jobs were not started",
    "ORACLE_EXCEPTION": "Oracle finished with an exception",
    "AGENTS_QUEUED": "Oracle passed; agent jobs are starting",
    "AGENTS_RUNNING": "agent jobs are RUNNING; Workbench will publish trial counts as the executor reports them",
    "FINALIZING": "execution results are being collected and the trajectory archive is being built",
    "COMPLETE": "all requested execution work is complete",
    "CANCELED": "run cancellation was requested",
    "EXPIRED": "the worker lease expired; cleanup was attempted",
    "ERROR": "the service recorded an execution error",
}
REMOTE_PROGRESS_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
REMOTE_AGENT_DISPLAY_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    # The Gemini slot runs through the Antigravity CLI; keep the Gemini label.
    "antigravity": "Gemini",
    "gemini-cli": "Gemini",
}


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {remainder:02d}s"


def remote_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def ordered_remote_agents(
    agents: list[object],
    agent_order: tuple[str, ...] | None = None,
) -> list[object]:
    if not agent_order:
        return agents
    positions = {agent_id: index for index, agent_id in enumerate(agent_order)}
    return [
        agent
        for _index, agent in sorted(
            enumerate(agents),
            key=lambda item: (
                positions.get(
                    str(item[1].get("id")) if isinstance(item[1], dict) else "",
                    len(positions),
                ),
                item[0],
            ),
        )
    ]


def remote_progress_signature(
    status: dict[str, object],
    *,
    agent_order: tuple[str, ...] | None = None,
) -> tuple[object, ...]:
    state = status.get("state")
    agents = status.get("agents") if isinstance(status.get("agents"), list) else []
    signature_parts: list[object] = [state]
    for agent in ordered_remote_agents(agents, agent_order):
        if not isinstance(agent, dict):
            continue
        signature_parts.extend((
            agent.get("id"),
            agent.get("state"),
            agent.get("finished_trials"),
            agent.get("pass_count"),
            agent.get("fail_count"),
            agent.get("exception_count"),
        ))
    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    exception = oracle.get("exception") if isinstance(oracle.get("exception"), dict) else {}
    error = status.get("error") if isinstance(status.get("error"), dict) else {}
    signature_parts.extend((
        oracle.get("state"),
        oracle.get("reward"),
        exception.get("type"),
        status.get("terminal_reason"),
        error.get("type"),
        error.get("message"),
    ))
    return tuple(signature_parts)


def remote_progress_table(
    status: dict[str, object],
    *,
    elapsed_sec: float = 0.0,
    agent_order: tuple[str, ...] | None = None,
    spinner_index: int = 0,
) -> object:
    """Build the single live remote progress table shown on a terminal."""
    state = str(status.get("state") or "UNKNOWN")
    message = REMOTE_STATE_MESSAGES.get(state, "waiting for the service to report progress")
    frame = REMOTE_PROGRESS_SPINNER_FRAMES[spinner_index % len(REMOTE_PROGRESS_SPINNER_FRAMES)]
    table = Table(
        title=f"{frame} {state} · elapsed {format_elapsed(elapsed_sec)} · {message}",
        expand=True,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Run", style="bold")
    table.add_column("PASS", justify="right", style="green")
    table.add_column("FAIL", justify="right", style="red")
    table.add_column("STATUS")

    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    oracle_state = str(oracle.get("state") or "INCOMPLETE")
    oracle_pass = "1" if oracle_state == "PASS" else "—"
    oracle_fail = "1" if oracle_state == "FAIL" else "—"
    oracle_status = oracle_state
    if oracle.get("reward") is not None:
        oracle_status += f" · reward={oracle.get('reward')}"
    oracle_exception = oracle.get("exception")
    if isinstance(oracle_exception, dict) and oracle_exception.get("type"):
        oracle_status += f" · {oracle_exception.get('type')}"
    table.add_row("Oracle", oracle_pass, oracle_fail, oracle_status)

    raw_agents = status.get("agents") if isinstance(status.get("agents"), list) else []
    agents_by_id = {
        str(agent.get("id")): agent
        for agent in raw_agents
        if isinstance(agent, dict) and agent.get("id") is not None
    }
    ordered_ids = list(agent_order or ())
    ordered_ids.extend(agent_id for agent_id in agents_by_id if agent_id not in ordered_ids)
    for agent_id in ordered_ids:
        agent = agents_by_id.get(agent_id)
        if agent is None:
            table.add_row(agent_id, "—", "—", "NOT REPORTED")
            continue
        finished = remote_count(agent.get("finished_trials"))
        expected = remote_count(agent.get("expected_trials"))
        passed = remote_count(agent.get("pass_count"))
        failed = remote_count(agent.get("fail_count"))
        exceptions = remote_count(agent.get("exception_count"))
        agent_name = str(agent.get("agent") or agent_id)
        display_name = REMOTE_AGENT_DISPLAY_NAMES.get(agent_name, agent_name)
        agent_state = str(agent.get('state', 'UNKNOWN'))
        if state == "AGENTS_RUNNING" and agent_state == "QUEUED":
            agent_state = "RUNNING"
        agent_status = f"{agent_state} · {finished}/{expected}"
        if exceptions:
            agent_status += f" · {exceptions} exception"
        table.add_row(display_name, str(passed), str(failed), agent_status)

    caption_parts: list[str] = []
    if status.get("updated_at"):
        caption_parts.append(f"server updated: {status['updated_at']}")
    if status.get("terminal_reason"):
        caption_parts.append(f"terminal reason: {status['terminal_reason']}")
    error = status.get("error")
    if isinstance(error, dict) and (error.get("type") or error.get("message")):
        caption_parts.append(str(error.get("message") or error.get("type")))
    if caption_parts:
        table.caption = " · ".join(caption_parts)
    return table


def remote_progress_plain_lines(
    status: dict[str, object],
    *,
    elapsed_sec: float = 0.0,
    agent_order: tuple[str, ...] | None = None,
) -> tuple[str, list[str]]:
    """Build a compact non-TTY fallback without repeated heartbeat details."""
    state = status.get("state")
    message = REMOTE_STATE_MESSAGES.get(str(state), "waiting for the service to report progress")
    heading = f"remote state: {state} | {message} | elapsed {format_elapsed(elapsed_sec)}"
    lines: list[str] = []
    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    oracle_state = str(oracle.get("state") or "INCOMPLETE")
    oracle_status = oracle_state
    if oracle.get("reward") is not None:
        oracle_status += f" reward={oracle.get('reward')}"
    if status.get("updated_at"):
        lines.append(f"  server updated: {status['updated_at']}")
    lines.append(f"  Oracle: {oracle_status}")
    agents = status.get("agents") if isinstance(status.get("agents"), list) else []
    agents_by_id = {
        str(agent.get("id")): agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("id") is not None
    }
    ordered_ids = list(agent_order or ())
    ordered_ids.extend(agent_id for agent_id in agents_by_id if agent_id not in ordered_ids)
    for agent_id in ordered_ids:
        agent = agents_by_id.get(agent_id)
        if agent is None:
            lines.append(f"  {agent_id}: NOT REPORTED")
            continue
        agent_state = str(agent.get('state', 'UNKNOWN'))
        if state == "AGENTS_RUNNING" and agent_state == "QUEUED":
            agent_state = "RUNNING"
        lines.append(
            f"  {REMOTE_AGENT_DISPLAY_NAMES.get(str(agent.get('agent') or agent_id), str(agent.get('agent') or agent_id))}: "
            f"{agent_state} "
            f"{remote_count(agent.get('finished_trials'))}/{remote_count(agent.get('expected_trials'))} "
            f"pass={remote_count(agent.get('pass_count'))} fail={remote_count(agent.get('fail_count'))}",
        )
    return heading, lines


def print_remote_progress(
    status: dict[str, object],
    previous: tuple[object, ...] | None,
    *,
    elapsed_sec: float = 0.0,
    force: bool = False,
    agent_order: tuple[str, ...] | None = None,
    live: object | None = None,
    spinner_index: int = 0,
) -> tuple[object, ...]:
    state = status.get("state")
    signature = remote_progress_signature(status, agent_order=agent_order)
    changed = signature != previous
    if live is not None:
        live.update(
            remote_progress_table(
                status,
                elapsed_sec=elapsed_sec,
                agent_order=agent_order,
                spinner_index=spinner_index,
            ),
            refresh=True,
        )
    elif changed:
        heading, lines = remote_progress_plain_lines(
            status,
            elapsed_sec=elapsed_sec,
            agent_order=agent_order,
        )
        console = runner_console()
        if console is not None and getattr(console, "is_terminal", False):
            console.print(
                remote_progress_table(
                    status,
                    elapsed_sec=elapsed_sec,
                    agent_order=agent_order,
                    spinner_index=spinner_index,
                )
            )
        else:
            print(heading, flush=True)
            for line in lines:
                print(line, flush=True)
    return signature


REMOTE_PROGRESS_SPINNER_FPS = 8.0


def sleep_with_live_progress(
    seconds: float,
    *,
    live: object | None,
    status: dict[str, object] | None,
    started: float,
    agent_order: tuple[str, ...] | None,
    spinner_index: int,
) -> int:
    """Sleep while the live table's spinner and elapsed clock keep moving.

    The status poll delay backs off to ``--remote-poll-max`` (30s by default),
    and the service can sit in one state for minutes. Without ticking here the
    table would freeze between responses and an in-progress run would be
    indistinguishable from a hung one. Returns the next spinner index.
    """
    if seconds <= 0:
        return spinner_index
    if live is None or status is None:
        time.sleep(seconds)
        return spinner_index
    deadline = time.monotonic() + seconds
    tick = 1.0 / REMOTE_PROGRESS_SPINNER_FPS
    while True:
        now = time.monotonic()
        if now >= deadline:
            return spinner_index
        time.sleep(min(tick, deadline - now))
        spinner_index += 1
        live.update(
            remote_progress_table(
                status,
                elapsed_sec=time.monotonic() - started,
                agent_order=agent_order,
                spinner_index=spinner_index,
            ),
            refresh=True,
        )


def _poll_remote_status_loop(
    base: str,
    run_id: str,
    token: str,
    *,
    minimum_delay: float,
    maximum_delay: float,
    progress_interval: float,
    agent_order: tuple[str, ...] | None,
    state_path: Path,
    state: dict[str, object],
    live: object | None,
) -> dict[str, object]:
    etag: str | None = None
    previous_signature: tuple[object, ...] | None = None
    delay = max(0.25, minimum_delay)
    started = time.monotonic()
    last_announcement = 0.0
    last_status: dict[str, object] | None = None
    spinner_index = 0
    while True:
        headers = {"If-None-Match": etag} if etag else {}
        try:
            http_status, status, response_headers = remote_json_request(
                "GET", remote_url(base, f"/v1/harbor/runs/{run_id}"), token, headers=headers
            )
        except RemoteClientError as error:
            if remote_error_is_retryable(error):
                wait = remote_retry_after(error.headers, delay)
                reason = "network error" if error.retryable else "temporarily unavailable"
                print(
                    f"remote poll {reason}; retrying in {wait:g}s "
                    f"(elapsed {format_elapsed(time.monotonic() - started)})",
                    flush=True,
                )
                spinner_index = sleep_with_live_progress(
                    wait,
                    live=live,
                    status=last_status,
                    started=started,
                    agent_order=agent_order,
                    spinner_index=spinner_index,
                )
                delay = min(maximum_delay, max(minimum_delay, delay * 2))
                continue
            raise
        if http_status == 304:
            now = time.monotonic()
            if last_status is not None and now - last_announcement >= progress_interval:
                previous_signature = print_remote_progress(
                    last_status,
                    previous_signature,
                    elapsed_sec=now - started,
                    force=True,
                    agent_order=agent_order,
                    live=live,
                    spinner_index=spinner_index,
                )
                spinner_index += 1
                last_announcement = now
            spinner_index = sleep_with_live_progress(
                remote_retry_after(response_headers, delay),
                live=live,
                status=last_status,
                started=started,
                agent_order=agent_order,
                spinner_index=spinner_index,
            )
            delay = min(maximum_delay, max(minimum_delay, delay * 2))
            continue
        etag = response_headers.get("etag", etag)
        last_status = status
        now = time.monotonic()
        signature = remote_progress_signature(status, agent_order=agent_order)
        should_announce = (
            previous_signature is None
            or signature != previous_signature
            or now - last_announcement >= progress_interval
        )
        previous_signature = print_remote_progress(
            status,
            previous_signature,
            elapsed_sec=now - started,
            force=should_announce,
            agent_order=agent_order,
            live=live,
            spinner_index=spinner_index,
        )
        spinner_index += 1
        if should_announce:
            last_announcement = now
        state.update({"run_id": run_id, "last_status": status.get("state"), "last_status_response": status})
        save_remote_state(state_path, state)
        if status.get("state") in REMOTE_TERMINAL_STATES:
            return status
        wait = remote_retry_after(response_headers, delay)
        spinner_index = sleep_with_live_progress(
            wait,
            live=live,
            status=status,
            started=started,
            agent_order=agent_order,
            spinner_index=spinner_index,
        )
        delay = min(maximum_delay, max(minimum_delay, delay * 2))


def poll_remote_status(
    base: str,
    run_id: str,
    token: str,
    *,
    minimum_delay: float,
    maximum_delay: float,
    progress_interval: float,
    agent_order: tuple[str, ...] | None,
    state_path: Path,
    state: dict[str, object],
) -> dict[str, object]:
    """Poll remote status with one in-place live table on interactive terminals."""
    console = runner_console()
    if Live is not None and console is not None and getattr(console, "is_terminal", False):
        live = Live(console=console, refresh_per_second=4, transient=False)
        with live:
            return _poll_remote_status_loop(
                base,
                run_id,
                token,
                minimum_delay=minimum_delay,
                maximum_delay=maximum_delay,
                progress_interval=progress_interval,
                agent_order=agent_order,
                state_path=state_path,
                state=state,
                live=live,
            )
    return _poll_remote_status_loop(
        base,
        run_id,
        token,
        minimum_delay=minimum_delay,
        maximum_delay=maximum_delay,
        progress_interval=progress_interval,
        agent_order=agent_order,
        state_path=state_path,
        state=state,
        live=None,
    )


REMOTE_EVIDENCE_FILE_NAMES = {
    "summary.md",
    "summary.json",
    "remote-error.json",
    "remote-results.json",
    "remote-status.json",
    "oracle-exception.json",
    "oracle-exception.txt",
    "oracle_exception.txt",
    "exception.json",
    "exception.txt",
    "exception.log",
}


def remote_archive_member_is_evidence(normalized: str, run_id: str) -> bool:
    """Keep trajectory and exception evidence, not task source files.

    Workbench has used more than one layout for trial artifacts. Trajectory
    files are identified by their ``trajectories/`` directory, but an
    ``exception.txt`` can also be nested directly under an Oracle or agent
    run. Preserve that exact evidence filename at any depth without allowing
    the rest of the submitted task tree through the archive filter.
    """
    relative = posixpath.relpath(normalized, run_id)
    if relative == ".":
        return True
    parts = relative.split("/")
    if parts[-1].lower() == "exception.txt":
        return True
    if any(part.lower() == "trajectories" for part in parts):
        return True
    if len(parts) <= 2 and parts[-1].lower() in REMOTE_EVIDENCE_FILE_NAMES:
        return True
    return len(parts) <= 2 and "exception" in parts[-1].lower()


def prune_remote_archive_to_evidence(destination: Path, run_id: str) -> None:
    """Remove task source files from an already verified remote archive."""
    if not destination.is_dir() or destination.is_symlink():
        return
    for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        normalized = f"{run_id}/{path.relative_to(destination).as_posix()}"
        if path.is_symlink() or path.is_file():
            if not remote_archive_member_is_evidence(normalized, run_id):
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def safe_remote_extract(archive_bytes: bytes | Path, destination: Path, run_id: str) -> int:
    if not re.fullmatch(r"hr_[A-Za-z0-9_-]{12,100}", run_id):
        raise RemoteClientError(0, "unsafe_archive", "The trajectory archive run id is invalid.")
    if isinstance(archive_bytes, Path):
        try:
            with archive_bytes.open("rb") as probe:
                magic = probe.read(2)
        except OSError as error:
            raise RemoteClientError(0, "archive_format", "The trajectory archive could not be opened.") from error
        archive_source = {"name": str(archive_bytes)}
    else:
        magic = archive_bytes[:2]
        archive_source = {"fileobj": io.BytesIO(archive_bytes)}
    if magic != b"\x1f\x8b":
        raise RemoteClientError(0, "archive_format", "The trajectory download was not gzip-compressed.")
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    roots: set[str] = set()
    with tarfile.open(**archive_source, mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            if not name or "\\" in name or name.startswith("/"):
                raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains an unsafe path.")
            if any(part in {".", ".."} for part in name.split("/")):
                raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains path traversal.")
            normalized = posixpath.normpath(name)
            if normalized == "." or normalized == ".." or normalized.startswith("../"):
                raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains path traversal.")
            root = normalized.split("/", 1)[0]
            roots.add(root)
            if normalized in names:
                raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains duplicate paths.")
            names.add(normalized)
            if member.islnk() or member.ischr() or member.isblk() or member.isfifo():
                raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains an unsupported link or device.")
            if not member.isdir() and not member.isfile() and not member.issym():
                raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains an unsupported entry type.")
            if member.issym():
                if not member.linkname or "\\" in member.linkname or re.match(r"^[A-Za-z]:", member.linkname):
                    raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains an unsafe symlink.")
                target = posixpath.normpath(posixpath.join(posixpath.dirname(normalized), member.linkname))
                if target != root and not target.startswith(f"{root}/"):
                    raise RemoteClientError(0, "unsafe_archive", "The trajectory archive contains a symlink outside its run root.")
            members.append(member)
        if roots != {run_id}:
            raise RemoteClientError(0, "unsafe_archive", "The trajectory archive root does not match the run id.")
        root_members = [member for member in members if posixpath.normpath(member.name) == run_id]
        if len(root_members) != 1 or not root_members[0].isdir():
            raise RemoteClientError(0, "unsafe_archive", "The trajectory archive must contain one run-root directory.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        stage_parent = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=destination.parent))
        stage = stage_parent / run_id
        stage.mkdir()
        try:
            evidence_members = [
                member
                for member in members
                if remote_archive_member_is_evidence(posixpath.normpath(member.name), run_id)
            ]
            directories = sorted(
                (member for member in evidence_members if member.isdir()),
                key=lambda item: item.name.count("/"),
            )
            regular_files = [member for member in evidence_members if member.isfile()]
            symlinks = [member for member in evidence_members if member.issym()]
            for member in directories:
                relative = posixpath.relpath(posixpath.normpath(member.name), run_id)
                target = stage / Path(relative)
                target.mkdir(parents=True, exist_ok=True)
            for member in regular_files:
                relative = posixpath.relpath(posixpath.normpath(member.name), run_id)
                target = stage / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RemoteClientError(0, "archive_extract", "A trajectory file could not be read.")
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            for member in symlinks:
                relative = posixpath.relpath(posixpath.normpath(member.name), run_id)
                target = stage / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise RemoteClientError(0, "archive_extract", "The local trajectory destination is not a directory.")
                shutil.rmtree(destination)
            os.replace(stage, destination)
        finally:
            shutil.rmtree(stage_parent, ignore_errors=True)
    return len(members)


def remote_local_archive_ready(base_dir: Path, run_id: str, sha256: str) -> bool:
    destination = base_dir / run_id
    marker = base_dir / f".{run_id}.sha256"
    return destination.is_dir() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == sha256


def _download_remote_archive_once(base_dir: Path, run_id: str, manifest: dict[str, object]) -> Path:
    download_url = manifest.get("download_url")
    sha256 = manifest.get("sha256")
    size_bytes = manifest.get("size_bytes")
    root_directory = manifest.get("root_directory")
    expected_entries = manifest.get("entry_count")
    archive_scope = manifest.get("archive_scope")
    if archive_scope != REMOTE_TRAJECTORY_ARCHIVE_SCOPE:
        raise RemoteClientError(
            0,
            "archive_scope",
            "Refusing to download an archive that is not explicitly trajectory-only.",
        )
    try:
        download_parts = urllib.parse.urlsplit(download_url) if isinstance(download_url, str) else None
    except ValueError:
        download_parts = None
    if (
        not isinstance(download_url, str)
        or download_parts is None
        or download_parts.scheme not in {"http", "https"}
        or not download_parts.hostname
        or download_parts.username is not None
        or download_parts.password is not None
        or not isinstance(sha256, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", sha256)
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 1
        or size_bytes > REMOTE_MAX_ARCHIVE_BYTES
        or root_directory != run_id
        or not isinstance(expected_entries, int)
        or isinstance(expected_entries, bool)
        or expected_entries < 1
    ):
        raise RemoteClientError(0, "archive_manifest", "The service returned an invalid trajectory manifest.")
    destination = base_dir.resolve() / run_id
    marker = base_dir.resolve() / f".{run_id}.sha256"
    if remote_local_archive_ready(base_dir.resolve(), run_id, sha256):
        prune_remote_archive_to_evidence(destination, run_id)
        print(f"trajectory archive: already verified at {destination}", flush=True)
        return destination
    print(f"trajectory archive: downloading {size_bytes} bytes", flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    download_path: Path | None = None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(download_url, method="GET"),
            timeout=REMOTE_TRANSFER_TIMEOUT_SECONDS,
        ) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > REMOTE_MAX_ARCHIVE_BYTES:
                        raise RemoteClientError(0, "archive_too_large", "The trajectory archive exceeds the client size limit.")
                except ValueError:
                    raise RemoteClientError(0, "archive_download", "The trajectory archive returned an invalid size.") from None
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{run_id}.",
                suffix=".tar.gz",
                dir=destination.parent,
                delete=False,
            ) as output:
                download_path = Path(output.name)
                digest = hashlib.sha256()
                total = 0
                next_progress = min(REMOTE_ARCHIVE_PROGRESS_BYTES, size_bytes)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > REMOTE_MAX_ARCHIVE_BYTES:
                        raise RemoteClientError(0, "archive_too_large", "The trajectory archive exceeds the client size limit.")
                    output.write(chunk)
                    digest.update(chunk)
                    if total >= next_progress:
                        percent = 100.0 * total / size_bytes
                        print(
                            f"trajectory archive: downloaded {total}/{size_bytes} bytes ({percent:.1f}%)",
                            flush=True,
                        )
                        next_progress = min(size_bytes, next_progress + REMOTE_ARCHIVE_PROGRESS_BYTES)
    except urllib.error.HTTPError as error:
        if download_path is not None:
            try:
                download_path.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise RemoteClientError(
            error.code,
            "archive_download",
            "The trajectory archive download failed.",
            _remote_response_headers(error),
        ) from None
    except RemoteClientError:
        if download_path is not None:
            try:
                download_path.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise
    except (urllib.error.URLError, OSError) as error:
        if download_path is not None:
            try:
                download_path.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise RemoteClientError(
            0,
            "archive_download",
            "The trajectory archive download failed.",
            retryable=True,
        ) from error
    try:
        if download_path is None or total != size_bytes:
            raise RemoteClientError(0, "archive_size", "The trajectory archive size did not match its manifest.")
        actual = f"sha256:{digest.hexdigest()}"
        if actual != sha256:
            raise RemoteClientError(0, "archive_checksum", "The trajectory archive checksum did not match its manifest.")
        count = safe_remote_extract(download_path, destination, run_id)
        if count != expected_entries:
            raise RemoteClientError(0, "archive_manifest", "The trajectory archive entry count did not match its manifest.")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{sha256}\n", encoding="utf-8")
        print(
            f"trajectory archive: verified server archive ({count} entries); "
            f"retained trajectory/exception evidence at {destination}",
            flush=True,
        )
        return destination
    finally:
        if download_path is not None:
            try:
                download_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def download_remote_archive(base_dir: Path, run_id: str, manifest: dict[str, object]) -> Path:
    """Download a trajectory archive, retrying transient transfer failures."""
    for attempt in range(REMOTE_REQUEST_RETRY_ATTEMPTS):
        try:
            return _download_remote_archive_once(base_dir, run_id, manifest)
        except RemoteClientError as error:
            if not remote_error_is_retryable(error) or attempt + 1 >= REMOTE_REQUEST_RETRY_ATTEMPTS:
                raise
            fallback = remote_backoff_delay(attempt)
            wait = (
                remote_retry_after(error.headers, fallback)
                if error.status in REMOTE_RETRYABLE_HTTP_STATUSES
                else fallback
            )
            print(
                "trajectory archive temporarily unavailable; "
                f"retrying in {wait:g}s (attempt {attempt + 1}/{REMOTE_REQUEST_RETRY_ATTEMPTS})",
                flush=True,
            )
            time.sleep(wait)
    raise AssertionError("trajectory archive retry loop did not return or raise")


def promote_remote_trajectory_archive(
    archive_destination: Path,
    trajectories_dir: Path,
) -> Path:
    """Promote available remote agent evidence into the direct local layout.

    Workbench archives retain a run root and task root so the download can be
    validated and partial runs can be inspected. The local authoring workflow
    exposes the task's direct ``trajectories/`` contents whenever agent trial
    evidence is available, including trials that ended with exceptions.
    """
    archive_destination = archive_destination.resolve()
    trajectories_dir = trajectories_dir.expanduser().resolve()
    if not archive_destination.is_dir():
        raise RemoteClientError(
            0,
            "archive_layout",
            "The downloaded trajectory archive is missing its extracted directory.",
        )

    direct_source = archive_destination / "trajectories"
    if not direct_source.is_dir():
        candidates = sorted(
            child / "trajectories"
            for child in archive_destination.iterdir()
            if child.is_dir() and (child / "trajectories").is_dir()
        )
        if len(candidates) != 1:
            raise RemoteClientError(
                0,
                "archive_layout",
                "The downloaded trajectory archive does not contain one task trajectories directory.",
            )
        direct_source = candidates[0]

    output_children = [
        child
        for child in sorted(direct_source.iterdir(), key=lambda path: path.name)
        if child.name not in {"summary.md", "summary.json"}
    ]
    if not any(child.is_dir() for child in output_children):
        raise RemoteClientError(
            0,
            "archive_layout",
            "The downloaded trajectory archive contains no Oracle or agent trajectory directories.",
        )

    summary_source = next(
        (
            candidate
            for candidate in (
                direct_source / "summary.md",
                archive_destination / "summary.md",
                direct_source / "summary.json",
                archive_destination / "summary.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    trajectories_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{archive_destination.name}.promote-",
            dir=trajectories_dir.parent,
        )
    )
    stage = stage_parent / trajectories_dir.name
    stage.mkdir()
    try:
        if summary_source is not None:
            summary_name = "summary.md" if summary_source.suffix == ".md" else "summary.json"
            shutil.copy2(summary_source, stage / summary_name)
        for child in output_children:
            destination = stage / child.name
            if child.is_symlink():
                destination.symlink_to(os.readlink(child))
            elif child.is_dir():
                shutil.copytree(child, destination, symlinks=True)
            elif child.is_file():
                shutil.copy2(child, destination)

        clear_trajectories_dir(trajectories_dir)
        trajectories_dir.mkdir(parents=True, exist_ok=True)
        for child in stage.iterdir():
            shutil.move(str(child), str(trajectories_dir / child.name))
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
    print(
        f"trajectory archive: promoted direct output -> {trajectories_dir}",
        flush=True,
    )
    return trajectories_dir


def preserve_remote_trajectory_archive(
    archive_destination: Path,
    trajectories_dir: Path,
    task_name: str,
) -> Path:
    """Keep a remote partial run in the same run-scoped shape as local output."""
    archive_destination = archive_destination.resolve()
    trajectories_dir = trajectories_dir.expanduser().resolve()
    if not archive_destination.is_dir():
        raise RemoteClientError(
            0,
            "archive_layout",
            "The downloaded trajectory archive is missing its extracted directory.",
        )

    direct_source = archive_destination / "trajectories"
    if direct_source.is_dir():
        task_archive_dir = archive_destination / task_name
        if task_archive_dir.exists():
            raise RemoteClientError(
                0,
                "archive_layout",
                "The downloaded trajectory archive has conflicting task layouts.",
            )
        task_archive_dir.mkdir(parents=True)
        shutil.move(str(direct_source), str(task_archive_dir / "trajectories"))

    nested_sources = sorted(
        child / "trajectories"
        for child in archive_destination.iterdir()
        if child.is_dir() and (child / "trajectories").is_dir()
    )
    if len(nested_sources) > 1:
        raise RemoteClientError(
            0,
            "archive_layout",
            "The downloaded trajectory archive does not contain one task trajectories directory.",
        )

    if not nested_sources:
        # Oracle exceptions may publish only an exception payload or log. Keep
        # that evidence under the run-scoped archive even without a normal
        # trajectories/ directory.
        cleanup_remote_archive_download(trajectories_dir, archive_destination.name)
        print(
            f"trajectory archive: preserved exception evidence -> {archive_destination}",
            flush=True,
        )
        return archive_destination

    summary_source = next(
        (
            candidate
            for candidate in (
                nested_sources[0] / "summary.md",
                archive_destination / "summary.md",
                nested_sources[0] / "summary.json",
                archive_destination / "summary.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    if summary_source is not None:
        summary_destination = archive_destination / (
            "summary.md" if summary_source.suffix == ".md" else "summary.json"
        )
        if not summary_destination.is_file():
            shutil.copy2(summary_source, summary_destination)

    cleanup_remote_archive_download(trajectories_dir, archive_destination.name)
    print(
        f"trajectory archive: preserved partial run -> {archive_destination}",
        flush=True,
    )
    return archive_destination


def cleanup_remote_archive_download(base_dir: Path, run_id: str) -> None:
    """Remove the checksum sidecar left by a verified remote download."""
    marker = base_dir.expanduser().resolve() / f".{run_id}.sha256"
    if marker.is_file() or marker.is_symlink():
        marker.unlink()


def remote_exception_detail(
    status: dict[str, object],
    results: dict[str, object],
) -> tuple[str | None, str]:
    """Return the most useful bounded diagnostic from a remote failure."""
    status_error = status.get("error") if isinstance(status.get("error"), dict) else {}
    oracle = results.get("oracle") if isinstance(results.get("oracle"), dict) else {}
    oracle_error = oracle.get("exception") if isinstance(oracle.get("exception"), dict) else {}
    sources = (
        (oracle_error, status_error)
        if status.get("state") == "ORACLE_EXCEPTION"
        else (status_error, oracle_error)
    )
    for source in sources:
        message = source.get("message")
        if isinstance(message, str) and message.strip():
            error_type = source.get("type")
            return (error_type.strip() if isinstance(error_type, str) and error_type.strip() else None, message.strip()[:4000])
    for source in sources:
        error_type = source.get("type")
        if isinstance(error_type, str) and error_type.strip():
            return error_type.strip()[:160], "The remote Harbor service returned an exception without a diagnostic message."
    return None, f"The remote Harbor service ended in {status.get('state') or 'ERROR'} without a diagnostic message."


def remote_exception_evidence_text(
    run_id: str,
    status: dict[str, object],
    results: dict[str, object],
) -> str:
    """Render the server diagnostic saved as local Oracle exception evidence."""
    error_type, message = remote_exception_detail(status, results)
    lines = [
        f"Harbor run: {run_id}",
        f"State: {status.get('state') or 'ERROR'}",
        f"Terminal reason: {status.get('terminal_reason') or 'unknown'}",
    ]
    if error_type:
        lines.append(f"Type: {error_type}")
    lines.extend(("", message, ""))
    return "\n".join(lines)


def write_remote_exception_text(
    archive_destination: Path,
    run_id: str,
    status: dict[str, object],
    results: dict[str, object],
) -> None:
    """Save the server diagnostic in the local exception evidence file."""
    try:
        (archive_destination / "oracle_exception.txt").write_text(
            remote_exception_evidence_text(run_id, status, results),
            encoding="utf-8",
        )
    except OSError as error:
        raise RemoteClientError(0, "archive_write", "Could not save the remote exception evidence.") from error


def write_remote_oracle_exception_evidence(
    archive_destination: Path,
    run_id: str,
    status: dict[str, object],
    results: dict[str, object],
) -> None:
    """Ensure an Oracle exception remains inspectable in the local archive."""
    evidence_path = archive_destination / "oracle-exception.json"
    oracle = results.get("oracle") if isinstance(results.get("oracle"), dict) else {}
    evidence = {
        "run_id": run_id,
        "state": status.get("state"),
        "terminal_reason": status.get("terminal_reason"),
        "oracle": oracle,
    }
    try:
        if not evidence_path.exists():
            evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise RemoteClientError(0, "archive_write", "Could not save the Oracle exception evidence.") from error
    write_remote_exception_text(archive_destination, run_id, status, results)


def remote_results_have_agent_trials(results: dict[str, object]) -> bool:
    """Return whether the service produced any agent trial evidence."""
    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    if remote_count(summary.get("agent_trials_finished")) > 0:
        return True
    trials = results.get("trials")
    return isinstance(trials, list) and any(isinstance(trial, dict) for trial in trials)


def remote_agent_archive_should_replace_direct(
    status: dict[str, object],
    results: dict[str, object],
) -> bool:
    """Return whether available agent evidence should replace direct output.

    An agent trial can finish with an exception, which makes the remote run
    nonzero even though its trajectory archive is the evidence we need to
    review. Oracle-only exceptions stay run-scoped because no agent campaign
    was produced for them.
    """
    if status.get("state") == "ORACLE_EXCEPTION":
        return False
    if remote_results_have_agent_trials(results):
        return True

    # Some COMPLETE/ERROR responses report the agent exception only in the
    # aggregate count. Treat that as agent evidence; ERROR-with-no-trials is
    # handled before archive download by remote_error_has_no_agent_trials().
    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    return (
        status.get("state") in {"COMPLETE", "ERROR"}
        and remote_count(summary.get("exception_count")) > 0
    )


def remote_error_has_no_agent_trials(
    status: dict[str, object],
    results: dict[str, object],
) -> bool:
    """Identify terminal service errors that cannot have agent trajectories."""
    return status.get("state") == "ERROR" and not remote_results_have_agent_trials(results)


def write_remote_error_evidence(
    base_dir: Path,
    run_id: str,
    status: dict[str, object],
    results: dict[str, object],
) -> Path:
    """Save compact failure evidence without copying the submitted task."""
    if not re.fullmatch(r"hr_[A-Za-z0-9_-]{12,100}", run_id):
        raise RemoteClientError(0, "archive_write", "The remote run id is invalid.")
    destination = base_dir.expanduser().resolve() / run_id
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise RemoteClientError(0, "archive_write", "The local trajectory evidence path is not a directory.")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        (destination / "remote-status.json").write_text(
            json.dumps(status, indent=2) + "\n",
            encoding="utf-8",
        )
        (destination / "remote-results.json").write_text(
            json.dumps(results, indent=2) + "\n",
            encoding="utf-8",
        )
        error = status.get("error") if isinstance(status.get("error"), dict) else {}
        summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
        error_message = error.get("message") or error.get("type") or "unknown service error"
        summary_text = "\n".join(
            (
                f"# Harbor run {run_id}",
                "",
                f"State: {status.get('state', 'ERROR')}",
                f"Terminal reason: {status.get('terminal_reason') or 'unknown'}",
                f"Service error: {error_message}",
                f"Oracle: {(results.get('oracle') or {}).get('verdict', 'INCOMPLETE') if isinstance(results.get('oracle'), dict) else 'INCOMPLETE'}",
                f"Agent trials: {remote_count(summary.get('agent_trials_finished'))}/{remote_count(summary.get('agent_trials_expected'))}",
                "",
                "No agent trial trajectories were produced; the remote task archive was not downloaded.",
                "",
            )
        )
        (destination / "summary.md").write_text(summary_text, encoding="utf-8")
        write_remote_exception_text(destination, run_id, status, results)
    except (OSError, TypeError, ValueError) as error:
        raise RemoteClientError(0, "archive_write", "Could not save remote error evidence.") from error
    return destination


def remote_exit_code(status: dict[str, object], results: dict[str, object]) -> int:
    state = status.get("state")
    if state == "ORACLE_FAILED":
        return 3
    if state == "COMPLETE":
        summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
        try:
            exception_count = int(summary.get("exception_count", 0) or 0)
        except (TypeError, ValueError):
            return 5
        return 4 if exception_count > 0 else 0
    return 5


def print_remote_results(
    results: dict[str, object],
    *,
    agent_order: tuple[str, ...] | None = None,
) -> None:
    oracle = results.get("oracle") if isinstance(results.get("oracle"), dict) else {}
    oracle_text = f"oracle: {oracle.get('verdict', 'INCOMPLETE')}"
    if oracle.get("reward") is not None:
        oracle_text += f" reward={oracle.get('reward')}"
    oracle_exception = oracle.get("exception")
    if isinstance(oracle_exception, dict) and oracle_exception.get("type"):
        oracle_text += f" exception={oracle_exception.get('type')}"

    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    finished = remote_count(summary.get("agent_trials_finished"))
    expected = remote_count(summary.get("agent_trials_expected"))
    trial_summary = (
        f"  agent trials: {finished}/{expected} finished, "
        f"{remote_count(summary.get('pass_count'))} pass, "
        f"{remote_count(summary.get('fail_count'))} fail, "
        f"{remote_count(summary.get('exception_count'))} exception"
    )

    trials = results.get("trials") if isinstance(results.get("trials"), list) else []
    by_agent: dict[str, dict[str, int]] = {}
    for trial in trials:
        if not isinstance(trial, dict):
            continue
        agent_id = str(trial.get("agent_id") or "unknown")
        counts = by_agent.setdefault(agent_id, {"PASS": 0, "FAIL": 0, "EXCEPTION": 0, "INCOMPLETE": 0})
        verdict = str(trial.get("verdict") or "INCOMPLETE")
        counts[verdict] = counts.get(verdict, 0) + 1
    ordered_ids = sorted(
        by_agent,
        key=lambda agent_id: (
            agent_order.index(agent_id) if agent_order and agent_id in agent_order else len(agent_order or ()),
            agent_id,
        ),
    )
    console = runner_console()
    if console is None:
        print(f"remote results: {oracle_text}", flush=True)
        print(trial_summary, flush=True)
        for agent_id in ordered_ids:
            counts = by_agent[agent_id]
            print(
                f"  {agent_id}: {counts.get('PASS', 0)} pass, "
                f"{counts.get('FAIL', 0)} fail, "
                f"{counts.get('EXCEPTION', 0)} exception, "
                f"{counts.get('INCOMPLETE', 0)} incomplete",
                flush=True,
            )
        return

    console.print(_rich_text(f"remote results: {oracle_text}"))
    console.print(_rich_text(trial_summary))
    table = Table(title="Agent verdicts", show_header=True, header_style="bold cyan")
    table.add_column("Agent", style="bold")
    table.add_column("Pass", justify="right")
    table.add_column("Fail", justify="right")
    table.add_column("Exception", justify="right")
    table.add_column("Incomplete", justify="right")
    for agent_id in ordered_ids:
        counts = by_agent[agent_id]
        table.add_row(
            str(agent_id),
            str(counts.get("PASS", 0)),
            str(counts.get("FAIL", 0)),
            str(counts.get("EXCEPTION", 0)),
            str(counts.get("INCOMPLETE", 0)),
        )
    console.print(table)


def remote_agent_name(agent_id: str) -> str:
    for (agent, _model), known_id in REMOTE_AGENT_CONFIGS.items():
        if known_id == agent_id:
            return agent
    # The server may report a model id this client does not know yet (for
    # example after a model rollover); fall back to the agent prefix so every
    # model of one agent shares a single summary column.
    for agent in REMOTE_AGENT_DISPLAY_NAMES:
        if agent_id == agent or agent_id.startswith(f"{agent}-"):
            return agent
    return agent_id


def write_remote_results_summary(
    destination: Path,
    run_id: str,
    task_name: str,
    state: object,
    results: dict[str, object],
    agent_order: tuple[str, ...],
) -> None:
    """Write local review summaries from /results, outside the downloaded archive."""
    trials = results.get("trials") if isinstance(results.get("trials"), list) else []
    by_agent: dict[str, dict[str, object]] = {
        agent_id: {"finished": 0, "passed": 0, "failed": 0, "exceptions": 0, "rewards": []}
        for agent_id in agent_order
    }
    for trial in trials:
        if not isinstance(trial, dict):
            continue
        agent_id = str(trial.get("agent_id") or "unknown")
        stats = by_agent.setdefault(
            agent_id,
            {"finished": 0, "passed": 0, "failed": 0, "exceptions": 0, "rewards": []},
        )
        verdict = str(trial.get("verdict") or "INCOMPLETE").upper()
        if verdict in {"PASS", "FAIL", "EXCEPTION"}:
            stats["finished"] = int(stats["finished"]) + 1
        if verdict == "PASS":
            stats["passed"] = int(stats["passed"]) + 1
        elif verdict == "FAIL":
            stats["failed"] = int(stats["failed"]) + 1
        elif verdict == "EXCEPTION":
            stats["exceptions"] = int(stats["exceptions"]) + 1
        reward = trial.get("reward")
        if isinstance(reward, (int, float)) and not isinstance(reward, bool):
            rewards = stats["rewards"]
            assert isinstance(rewards, list)
            rewards.append(float(reward))

    ordered_ids = list(agent_order)
    ordered_ids.extend(agent_id for agent_id in sorted(by_agent) if agent_id not in agent_order)
    # Merge model ids that belong to the same agent (e.g. two Codex models) into
    # one column; the submission check rejects duplicate agent columns.
    by_name: dict[str, dict[str, object]] = {}
    for agent_id in ordered_ids:
        stats = by_agent[agent_id]
        merged = by_name.setdefault(
            remote_agent_name(agent_id),
            {"finished": 0, "passed": 0, "failed": 0, "exceptions": 0, "rewards": []},
        )
        for key in ("finished", "passed", "failed", "exceptions"):
            merged[key] = int(merged[key]) + int(stats[key])
        merged_rewards = merged["rewards"]
        stats_rewards = stats["rewards"]
        assert isinstance(merged_rewards, list) and isinstance(stats_rewards, list)
        merged_rewards.extend(stats_rewards)
    oracle = results.get("oracle") if isinstance(results.get("oracle"), dict) else {}
    oracle_verdict = str(oracle.get("verdict") or "INCOMPLETE").lower()
    oracle_reward = oracle.get("reward")
    oracle_reward_text = format_metric(float(oracle_reward)) if isinstance(oracle_reward, (int, float)) else "-"
    oracle_cell = f"{oracle_verdict}, reward {oracle_reward_text}"

    headers = ["Task", "Oracle", *by_name, "Status"]
    separator = ["---"] * len(headers)
    cells = [markdown_escape(task_name), oracle_cell]
    for stats in by_name.values():
        rewards = stats["rewards"]
        assert isinstance(rewards, list)
        mean = sum(rewards) / len(rewards) if rewards else None
        cell = f"{stats['passed']}/{stats['finished']} pass, mean {format_metric(mean)}"
        if stats["exceptions"]:
            cell += f", {stats['exceptions']} errors"
        cells.append(cell)
    cells.append(str(state).lower())
    markdown = "\n".join(
        (
            f"# Harbor Run Summary: {run_id}",
            "",
            f"| {' | '.join(headers)} |",
            f"| {' | '.join(separator)} |",
            f"| {' | '.join(cells)} |",
            "",
            "Completed tasks: 1/1" if str(state) == "COMPLETE" else "Completed tasks: 0/1",
            "",
        )
    )
    try:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "summary.json").write_text(
            json.dumps(results, indent=2) + "\n",
            encoding="utf-8",
        )
        (destination / "summary.md").write_text(markdown, encoding="utf-8")
    except (OSError, TypeError, ValueError) as error:
        raise RemoteClientError(0, "archive_write", "Could not save the remote results summary.") from error


def run_remote(task_root: Path, args: argparse.Namespace) -> int:
    base: str | None = None
    token = ""
    run_id: str | None = None
    bundle_temp_dirs: list[tempfile.TemporaryDirectory[str]] = []
    try:
        if args.env != "modal":
            raise RemoteInputError("--remote requires --env modal")
        if args.archive_only or args.oracle_sort or args.dry_run:
            raise RemoteInputError("--remote cannot be combined with --archive-only, --oracle-sort, or --dry-run")
        forbidden = {
            "--env-file": args.env_file,
            "--agent-env": args.agent_env,
            "--verifier-env": args.verifier_env,
            "--environment-kwarg": args.environment_kwarg,
            "--agent-kwarg": args.agent_kwarg,
            "--artifact": args.artifact,
            "--modal-secret": args.modal_secret,
        }
        used = [flag for flag, values in forbidden.items() if values]
        if used:
            raise RemoteInputError(f"remote mode does not accept client-controlled settings: {', '.join(used)}")
        token = (args.workbench_token or os.environ.get("WORKBENCH_RUNNER_TOKEN", "")).strip()
        if not token:
            raise RemoteInputError("provide --workbench-token or WORKBENCH_RUNNER_TOKEN for remote mode")
        if args.remote_poll_min <= 0 or args.remote_poll_max <= 0 or args.remote_poll_max < args.remote_poll_min:
            raise RemoteInputError("remote poll intervals must be positive, with --remote-poll-max >= --remote-poll-min")
        if args.remote_progress_interval_sec <= 0:
            raise RemoteInputError("--remote-progress-interval-sec must be positive")
        base = remote_service_base(args.service_url)
        agents = remote_agent_payload(args)
        agent_order = tuple(str(agent["id"]) for agent in agents)
        jobs_dir = args.jobs_dir.resolve()
        local_id = args.run_id
        request_path = remote_request_state_path(jobs_dir, local_id)
        state = load_remote_state(request_path) or {}
        if not args.resume and not state:
            clear_harbor_jobs_dir(jobs_dir)
        run_id = state.get("run_id") if isinstance(state.get("run_id"), str) else None
        service_run_path: Path | None = None

        if run_id is None and local_id.startswith("hr_"):
            run_id = local_id
        if run_id is None:
            print(f"remote submit: packaging task {task_root.resolve()}", flush=True)
            bundle_temp = tempfile.TemporaryDirectory(prefix="workbench-remote-bundle-")
            bundle_temp_dirs.append(bundle_temp)
            archive, bundle_sha256, bundle_size = build_remote_task_bundle_file(
                task_root,
                Path(bundle_temp.name) / "task.tar.gz",
            )
            print(
                f"remote submit: bundle ready ({bundle_size} bytes, {bundle_sha256}); "
                f"requesting {len(agents)} agent job(s) x {args.repeats} attempt(s) "
                f"x {sum(int(agent['concurrency']) for agent in agents)} fan-out "
                f"lane(s) = {args.repeats * sum(int(agent['concurrency']) for agent in agents)} "
                "configured trial(s)",
                flush=True,
            )
            idempotency_key = state.get("idempotency_key") if isinstance(state.get("idempotency_key"), str) else f"harbor-runner:{uuid4().hex}"
            client_request_id = state.get("client_request_id") if isinstance(state.get("client_request_id"), str) else f"remote-{slug(task_root.name)}-{slug(local_id)}"
            payload = {
                "client_request_id": client_request_id,
                "task": {"name": task_root.name, "format": "tar.gz", "sha256": bundle_sha256, "size_bytes": bundle_size},
                "execution": remote_execution_payload(args, agents),
            }
            save_remote_state(request_path, {
                **state,
                "service_url": base,
                "client_request_id": client_request_id,
                "idempotency_key": idempotency_key,
                "bundle_sha256": bundle_sha256,
                "bundle_size_bytes": bundle_size,
                "request_payload": payload,
            })
            try:
                _, created, _ = remote_json_request_with_retry(
                    "POST",
                    remote_url(base, "/v1/harbor/runs"),
                    token,
                    payload=payload,
                    headers={"Idempotency-Key": idempotency_key},
                    operation="remote submit",
                )
            except RemoteClientError as error:
                if error.status in {400, 401, 403, 409, 422}:
                    print(f"remote request rejected: {error}", file=sys.stderr)
                    return 2
                raise
            run_id = created.get("run_id")
            if not isinstance(run_id, str) or not run_id.startswith("hr_"):
                raise RemoteClientError(0, "response", "The service returned an invalid run id.")
            service_run_path = remote_state_path(jobs_dir, run_id)
            state = {
                **state,
                "service_url": base,
                "run_id": run_id,
                "client_request_id": client_request_id,
                "idempotency_key": idempotency_key,
                "bundle_sha256": bundle_sha256,
                "bundle_size_bytes": bundle_size,
                "request_payload": payload,
            }
            save_remote_state(request_path, state)
            save_remote_state(service_run_path, state)
            print(
                f"remote submit: Workbench created {run_id} "
                f"({created.get('state', 'UPLOADING')}); uploading task bundle",
                flush=True,
            )
            upload = created.get("upload")
            if isinstance(upload, dict) and isinstance(upload.get("url"), str):
                upload_headers = upload.get("headers") if isinstance(upload.get("headers"), dict) else {}
                remote_upload(str(upload["url"]), archive, {str(k): str(v) for k, v in upload_headers.items()})
                print(f"remote upload: {bundle_size} bytes verified locally ({bundle_sha256})", flush=True)
            bundle_temp.cleanup()
            bundle_temp_dirs.remove(bundle_temp)
            print(f"remote start: enqueueing {run_id}", flush=True)
            try:
                _, started, _ = remote_json_request_with_retry(
                    "POST",
                    remote_url(base, f"/v1/harbor/runs/{run_id}:start"),
                    token,
                    operation="remote start",
                )
            except RemoteClientError as error:
                if error.status in {400, 401, 403, 409, 422}:
                    print(f"remote start rejected: {error}", file=sys.stderr)
                    return 2
                raise
            print(f"remote run: {run_id} ({started.get('state', 'QUEUED')})", flush=True)
        else:
            service_run_path = remote_state_path(jobs_dir, run_id)
            state = load_remote_state(service_run_path) or state
            if state.get("service_url") and state.get("service_url") != base:
                raise RemoteInputError("the saved remote run belongs to a different --service-url")
            _, resume_status, _ = remote_json_request_with_retry(
                "GET",
                remote_url(base, f"/v1/harbor/runs/{run_id}"),
                token,
                operation="remote resume",
            )
            print(
                f"remote resume: {run_id} is {resume_status.get('state', 'UNKNOWN')}; "
                "checking whether upload or execution needs to continue",
                flush=True,
            )
            if resume_status.get("state") in {"CREATED", "UPLOADING"}:
                request_payload = state.get("request_payload")
                if not isinstance(request_payload, dict):
                    raise RemoteClientError(409, "upload_not_resumable", "The saved remote run has no resumable upload request.")
                started = None
                try:
                    _, started, _ = remote_json_request_with_retry(
                        "POST",
                        remote_url(base, f"/v1/harbor/runs/{run_id}:start"),
                        token,
                        operation="remote resume start",
                    )
                except RemoteClientError as error:
                    if error.status == 409 and error.code == "bundle_not_found":
                        started = None
                    elif error.status in {400, 401, 403, 409, 422}:
                        print(f"remote start rejected: {error}", file=sys.stderr)
                        return 2
                    else:
                        raise
                if started is None:
                    bundle_temp = tempfile.TemporaryDirectory(prefix="workbench-remote-bundle-")
                    bundle_temp_dirs.append(bundle_temp)
                    archive, bundle_sha256, bundle_size = build_remote_task_bundle_file(
                        task_root,
                        Path(bundle_temp.name) / "task.tar.gz",
                    )
                    if bundle_sha256 != state.get("bundle_sha256"):
                        raise RemoteInputError("the local task changed after the remote run was created; submit a new run")
                    _, created, _ = remote_json_request_with_retry(
                        "POST",
                        remote_url(base, "/v1/harbor/runs"),
                        token,
                        payload=request_payload,
                        headers={"Idempotency-Key": str(state.get("idempotency_key") or "")},
                        operation="remote resume upload",
                    )
                    if created.get("run_id") != run_id:
                        raise RemoteClientError(0, "response", "The service returned a different run id while resuming upload.")
                    upload = created.get("upload")
                    if not isinstance(upload, dict) or not isinstance(upload.get("url"), str):
                        raise RemoteClientError(409, "upload_not_resumable", "The service did not return a resumable upload URL.")
                    upload_headers = upload.get("headers") if isinstance(upload.get("headers"), dict) else {}
                    remote_upload(str(upload["url"]), archive, {str(k): str(v) for k, v in upload_headers.items()})
                    state.update({"bundle_size_bytes": bundle_size, "request_payload": request_payload})
                    save_remote_state(service_run_path, state)
                    bundle_temp.cleanup()
                    bundle_temp_dirs.remove(bundle_temp)
                    try:
                        _, started, _ = remote_json_request_with_retry(
                            "POST",
                            remote_url(base, f"/v1/harbor/runs/{run_id}:start"),
                            token,
                            operation="remote resume start",
                        )
                    except RemoteClientError as error:
                        if error.status in {400, 401, 403, 409, 422}:
                            print(f"remote start rejected: {error}", file=sys.stderr)
                            return 2
                        raise
                print(f"remote run: {run_id} ({started.get('state', 'QUEUED')})", flush=True)
            else:
                print(f"remote resume: {run_id}", flush=True)

        assert isinstance(run_id, str)
        service_run_path = service_run_path or remote_state_path(jobs_dir, run_id)
        cancel_on_interrupt = bool(getattr(args, "cancel_on_interrupt", True))
        interrupt_behavior = (
            "Ctrl-C requests server cancellation"
            if cancel_on_interrupt
            else "Ctrl-C leaves the server run running"
        )
        print(
            f"remote monitor: polling {run_id}; progress messages every "
            f"{args.remote_progress_interval_sec:g}s ({interrupt_behavior})",
            flush=True,
        )
        status = poll_remote_status(
            base,
            run_id,
            token,
            minimum_delay=max(0.25, args.remote_poll_min),
            maximum_delay=max(args.remote_poll_min, args.remote_poll_max),
            progress_interval=args.remote_progress_interval_sec,
            agent_order=agent_order,
            state_path=service_run_path,
            state=state,
        )
        print(
            f"remote monitor: terminal state {status.get('state')} "
            f"({status.get('terminal_reason') or 'no terminal reason'})",
            flush=True,
        )
        print("remote results: fetching Oracle and agent verdicts", flush=True)
        _, results, _ = remote_json_request_with_retry(
            "GET",
            remote_url(base, f"/v1/harbor/runs/{run_id}/results"),
            token,
            operation="remote results",
        )
        print_remote_results(results, agent_order=agent_order)
        remote_exit = remote_exit_code(status, results)
        manifest: dict[str, object] | None = None
        archive_destination: Path | None = None
        archive_downloaded = False
        should_archive = bool(args.archive_completed) and status.get("state") != "ORACLE_FAILED"
        if not args.archive_completed:
            print("remote archive: skipped (--no-archive-completed)", flush=True)
            cleanup_remote_archive_download(args.completed_trajectories_dir, run_id)
        elif not should_archive:
            print(
                f"remote archive: skipped for terminal state {status.get('state')}",
                flush=True,
            )
        elif remote_error_has_no_agent_trials(status, results):
            archive_destination = write_remote_error_evidence(
                args.completed_trajectories_dir,
                run_id,
                status,
                results,
            )
            print(
                "remote archive: skipped; the service failed before producing "
                "agent trials, so no trajectory archive was downloaded",
                flush=True,
            )
        else:
            print("remote archive: waiting for the trajectory manifest", flush=True)
            for attempt in range(20):
                try:
                    _, manifest_value, _ = remote_json_request_with_retry(
                        "GET",
                        remote_url(base, f"/v1/harbor/runs/{run_id}/trajectories"),
                        token,
                        operation="remote archive manifest",
                    )
                    manifest = manifest_value
                    break
                except RemoteClientError as error:
                    if error.status != 409:
                        raise
                    print(
                        f"remote archive: still finalizing; retrying manifest "
                        f"({attempt + 1}/20, elapsed {format_elapsed(attempt + 1.0)})",
                        flush=True,
                    )
                    time.sleep(1.0)
            if manifest is None:
                raise RemoteClientError(409, "trajectory_not_ready", "The service did not publish a trajectory archive.")
            print(
                f"remote archive: trajectory-only manifest ready ({manifest.get('size_bytes', '?')} bytes, "
                f"{manifest.get('entry_count', '?')} entries); downloading",
                flush=True,
            )
            archive_destination = download_remote_archive(args.completed_trajectories_dir, run_id, manifest)
            archive_downloaded = True
            if remote_exit == 0 or remote_agent_archive_should_replace_direct(status, results):
                archive_destination = promote_remote_trajectory_archive(
                    archive_destination,
                    args.completed_trajectories_dir,
                )
                write_remote_results_summary(
                    archive_destination,
                    run_id,
                    task_root.name,
                    status.get("state"),
                    results,
                    agent_order,
                )
            else:
                archive_destination = preserve_remote_trajectory_archive(
                    archive_destination,
                    args.completed_trajectories_dir,
                    task_root.name,
                )
                write_remote_results_summary(
                    archive_destination,
                    run_id,
                    task_root.name,
                    status.get("state"),
                    results,
                    agent_order,
                )
                if status.get("state") == "ORACLE_EXCEPTION":
                    write_remote_oracle_exception_evidence(
                        archive_destination,
                        run_id,
                        status,
                        results,
                    )
                elif status.get("state") == "ERROR":
                    write_remote_exception_text(
                        archive_destination,
                        run_id,
                        status,
                        results,
                    )
        state.update({
            "run_id": run_id,
            "service_url": base,
            "terminal_state": status.get("state"),
            "trajectory_sha256": manifest.get("sha256") if manifest else None,
            "trajectory_directory": str(archive_destination) if archive_destination else None,
            "archive_downloaded": archive_downloaded,
        })
        save_remote_state(service_run_path, state)
        if request_path != service_run_path:
            save_remote_state(request_path, state)
        if archive_destination is not None:
            if archive_downloaded:
                print(f"remote complete: trajectory archive available at {archive_destination}", flush=True)
            else:
                print(f"remote complete: failure evidence available at {archive_destination}", flush=True)
        else:
            print("remote complete: no local trajectory archive", flush=True)
        return remote_exit
    except KeyboardInterrupt:
        cancel_on_interrupt = bool(getattr(args, "cancel_on_interrupt", True))
        if cancel_on_interrupt and isinstance(run_id, str) and base:
            try:
                remote_json_request(
                    "POST",
                    remote_url(base, f"/v1/harbor/runs/{run_id}:cancel"),
                    token,
                )
                print("remote run cancellation requested", flush=True)
            except Exception:
                print("remote run cancellation could not be confirmed", file=sys.stderr)
        print(
            "interrupt: remote run was not canceled"
            if not cancel_on_interrupt
            else "interrupt: remote run cancellation requested",
            file=sys.stderr,
        )
        return 130
    except RemoteInputError as error:
        print(f"remote input error: {error}", file=sys.stderr)
        return 2
    except RemoteClientError as error:
        print(f"remote service error: {error}", file=sys.stderr)
        return 5
    finally:
        for bundle_temp in bundle_temp_dirs:
            bundle_temp.cleanup()


def default_run_id() -> str:
    """Return a readable run ID that is still unique within one second."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:10]}"


def validate_run_id(run_id: str) -> None:
    """Keep run-derived paths and Modal names bounded and unambiguous."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", run_id):
        raise SystemExit(
            "error: --run-id must start with a letter or digit and contain only "
            "letters, digits, '.', '_' or '-' (maximum 64 characters)"
        )


def make_modal_app_name(run_id: str, nonce: str) -> str:
    """Build a unique Modal object name that fits Modal's 64-character limit."""
    # Keep the tail: callers may pass a timestamp-prefixed run ID, so the
    # beginning of the nonce is not necessarily the random part.
    suffix = f"-{slug(nonce)[-16:]}"
    prefix = f"{MODAL_APP_NAME_PREFIX}-{slug(run_id)}"
    room = max(1, 64 - len(suffix))
    return f"{prefix[:room].rstrip('-')}{suffix}"


def modal_run_manifest_path(jobs_dir: Path, run_id: str) -> Path:
    return jobs_dir / f"{run_id}{MODAL_RUN_MANIFEST_SUFFIX}"


def read_modal_run_manifest(path: Path, run_id: str) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read Modal run manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise SystemExit(f"error: Modal run manifest does not belong to run-id {run_id!r}: {path}")
    app_name = payload.get("modal_app_name")
    if not isinstance(app_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", app_name):
        raise SystemExit(f"error: Modal run manifest has an invalid app name: {path}")
    return app_name


def create_modal_run_manifest(jobs_dir: Path, run_id: str) -> tuple[Path, str]:
    """Atomically claim a run ID and record the Modal app that owns it."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = modal_run_manifest_path(jobs_dir, run_id)
    app_name = make_modal_app_name(run_id, uuid4().hex)
    payload = {
        "version": 1,
        "run_id": run_id,
        "modal_app_name": app_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        raise SystemExit(
            f"error: run-id {run_id!r} is already claimed by {manifest_path}; "
            "use --resume to continue that run or choose a new run-id"
        ) from exc
    except OSError as exc:
        raise SystemExit(f"error: cannot create Modal run manifest {manifest_path}: {exc}") from exc
    return manifest_path, app_name


def resolve_modal_run_identity(
    jobs_dir: Path,
    run_id: str,
    *,
    resume: bool,
    archive_only: bool,
    dry_run: bool,
) -> tuple[Path, str]:
    """Resolve the app name without allowing two live runs to share ownership."""
    manifest_path = modal_run_manifest_path(jobs_dir, run_id)
    if manifest_path.is_file():
        if resume or archive_only or dry_run:
            return manifest_path, read_modal_run_manifest(manifest_path, run_id)
        raise SystemExit(
            f"error: run-id {run_id!r} is already claimed by {manifest_path}; "
            "use --resume to continue that run or choose a new run-id"
        )

    if dry_run:
        return manifest_path, make_modal_app_name(run_id, "dryrun-" + uuid4().hex)

    if resume or archive_only:
        action = "resume" if resume else "archive"
        raise SystemExit(
            f"error: cannot {action} run-id {run_id!r} without its Modal run manifest "
            f"({manifest_path}); start a new run or restore the original harbor-jobs directory"
        )

    return create_modal_run_manifest(jobs_dir, run_id)


def parse_agent_spec(raw: str, default_concurrency: int) -> AgentSpec:
    parts = raw.split(":")
    if not (2 <= len(parts) <= 4) or not parts[0] or not parts[1]:
        raise SystemExit(
            "error: --run expects AGENT:MODEL[:LABEL[:N_CONCURRENT]], "
            f"got {raw!r}"
        )
    agent, model = parts[0], parts[1]
    label = parts[2] if len(parts) >= 3 and parts[2] else f"{agent}-{model}"
    if len(parts) == 4 and parts[3]:
        try:
            n_concurrent = int(parts[3])
        except ValueError:
            raise SystemExit(
                f"error: --run concurrency must be an integer, got {parts[3]!r}"
            )
        if n_concurrent < 1:
            raise SystemExit("error: --run concurrency must be >= 1")
    else:
        n_concurrent = default_concurrency
    return AgentSpec(
        agent=agent, model=model, label=slug(label), n_concurrent=n_concurrent
    )


def parse_key_value(raw: str, flag: str) -> tuple[str, str]:
    if "=" not in raw:
        raise SystemExit(f"error: {flag} expects KEY=VALUE, got {raw!r}")
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise SystemExit(f"error: {flag} has an empty key in {raw!r}")
    return key, value


def require_executable(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"error: required executable not found on PATH: {name}")


def clear_harbor_jobs_dir(jobs_dir: Path) -> None:
    """Remove prior local Harbor output before starting a fresh run.

    The caller must pass the explicitly configured jobs directory. Guard the
    broadest accidental targets, then remove only its immediate children so a
    typo cannot turn into a recursive delete of a parent directory.
    """
    resolved = jobs_dir.expanduser().resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in forbidden:
        raise SystemExit(
            f"error: refusing to clear unsafe Harbor jobs directory: {resolved}"
        )
    if resolved.exists() and not resolved.is_dir():
        raise SystemExit(f"error: Harbor jobs path is not a directory: {resolved}")

    resolved.mkdir(parents=True, exist_ok=True)
    children = list(resolved.iterdir())
    print(
        f"clearing Harbor jobs directory: {resolved} "
        f"({len(children)} existing item(s))",
        flush=True,
    )
    for child in children:
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)


def clear_trajectories_dir(trajectories_dir: Path) -> None:
    """Remove the previous successful trajectory output before replacement."""
    resolved = trajectories_dir.expanduser().resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in forbidden:
        raise SystemExit(
            f"error: refusing to clear unsafe trajectory directory: {resolved}"
        )
    if resolved.exists() and not resolved.is_dir():
        raise SystemExit(f"error: trajectory path is not a directory: {resolved}")

    resolved.mkdir(parents=True, exist_ok=True)
    children = list(resolved.iterdir())
    print(
        f"clearing trajectory directory: {resolved} "
        f"({len(children)} existing item(s))",
        flush=True,
    )
    for child in children:
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)


def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"error: could not read task metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"error: task metadata is not a TOML table: {path}")
    return value


def task_network_mode(task_root: Path) -> str:
    """Read the task's declared network policy, defaulting to the client policy."""
    environment = load_toml(task_root / "task.toml").get("environment")
    mode = environment.get("network_mode") if isinstance(environment, dict) else None
    if not isinstance(mode, str) or mode not in set(SUPPORTED_NETWORK_MODES):
        return DEFAULT_NETWORK_MODE
    return mode


def docker_network_args(network_mode: str) -> list[str]:
    """Map a task network_mode onto local `docker run` networking flags.

    `public` keeps Docker's default bridge network so a local container matches
    the Oracle and agent snapshots the runner submits to Harbor.
    """
    return ["--network", "none"] if network_mode == "no-network" else []


@dataclass(frozen=True)
class SmokeProject:
    """The task paths needed by the local Docker smoke test."""

    root: Path
    name: str

    @property
    def dockerfile_dir(self) -> Path:
        return self.root / "environment"

    @property
    def solution_dir(self) -> Path:
        return self.root / "solution"

    @property
    def tests_dir(self) -> Path:
        return self.root / "tests"

    @property
    def task_toml(self) -> Path:
        return self.root / "task.toml"


def load_smoke_project(task_root: Path) -> SmokeProject:
    required = (
        task_root / "environment" / "Dockerfile",
        task_root / "solution" / "solve.sh",
        task_root / "tests" / "test.sh",
        task_root / "tests" / "test_outputs.py",
    )
    missing = [str(path.relative_to(task_root)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit(
            f"error: {task_root} is missing smoke-test files: {', '.join(missing)}"
        )
    config = load_toml(task_root / "task.toml")
    task_config = config.get("task")
    name = task_config.get("name") if isinstance(task_config, dict) else None
    return SmokeProject(root=task_root.resolve(), name=str(name or task_root.name))


def smoke_project_env(task_toml: Path) -> dict[str, str]:
    config = load_toml(task_toml)
    environment = config.get("environment", {})
    raw = environment.get("env", {}) if isinstance(environment, dict) else {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemExit(f"error: [environment].env must be a TOML table: {task_toml}")
    return {str(key): str(value) for key, value in raw.items()}


def parse_smoke_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"error: cannot read smoke env file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise SystemExit(
                f"error: {path}:{line_number}: expected KEY=VALUE, got {line!r}"
            )
        key, _, value = stripped.partition("=")
        values[key.strip()] = value
    return values


def parse_smoke_env_arg(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise SystemExit(f"error: --smoke-env expects KEY=VALUE, got {raw!r}")
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise SystemExit(f"error: --smoke-env has an empty key in {raw!r}")
    return key, value


def redact_smoke_command(command: list[str]) -> str:
    rendered: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        rendered.append(token)
        if token == "-e" and index + 1 < len(command):
            rendered.append("<redacted>")
            index += 2
            continue
        index += 1
    return " ".join(rendered)


def run_smoke_command(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {redact_smoke_command(command)}", flush=True)
    return subprocess.run(command, **kwargs)


def build_smoke_image(project: SmokeProject, image_tag: str, no_cache: bool) -> None:
    command = [
        "docker",
        "build",
        "--platform",
        MODAL_PLATFORM,
        "-t",
        image_tag,
    ]
    if no_cache:
        command.append("--no-cache")
    command.append(str(project.dockerfile_dir))
    result = run_smoke_command(command)
    if result.returncode != 0:
        raise SystemExit(2)


def run_smoke_container(
    project: SmokeProject,
    image_tag: str,
    logs_dir: Path,
    keep_container: bool,
    env: dict[str, str],
) -> int:
    """Run the reference solution and verifier locally in one container."""
    container_name = f"comp-smoke-{uuid4().hex[:12]}"
    entrypoint = (
        "set -o pipefail; "
        "echo '=== solve.sh ==='; "
        "bash /solution/solve.sh; SOLVE=$?; "
        "echo \"solve.sh exit=$SOLVE\"; "
        "if [ $SOLVE -eq 0 ]; then "
        "  echo '=== test.sh ==='; "
        "  bash /tests/test.sh; TEST=$?; "
        "else TEST=$SOLVE; fi; "
        "if [ -d /workspace/output ]; then "
        "  mkdir -p /logs/workspace_output && "
        "  cp -R /workspace/output/. /logs/workspace_output/ 2>/dev/null || true; "
        "fi; "
        "exit $TEST"
    )
    command = [
        "docker",
        "run",
        "--platform",
        MODAL_PLATFORM,
        *docker_network_args(task_network_mode(project.root)),
        "--name",
        container_name,
        "-v",
        f"{project.solution_dir}:/solution:ro",
        "-v",
        f"{project.tests_dir}:/tests:ro",
        "-v",
        f"{logs_dir}:/logs",
    ]
    data_dir = project.root / "environment" / "data"
    if data_dir.is_dir():
        command.extend(["-v", f"{data_dir}:/workspace/data:ro"])
    for key, value in env.items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([image_tag, "bash", "-c", entrypoint])

    try:
        result = run_smoke_command(command)
        return result.returncode
    finally:
        if not keep_container:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            print(f"(container retained: {container_name})")


def summarize_smoke(logs_dir: Path, container_exit: int, elapsed: float) -> int:
    reward_path = logs_dir / "verifier" / "reward.txt"
    ctrf_path = logs_dir / "verifier" / "ctrf.json"
    passed = reward_path.read_text(encoding="utf-8").strip() == "1" if reward_path.exists() else container_exit == 0

    print()
    print("=" * 60)
    print(f"  SMOKE RESULT: {'PASS' if passed else 'FAIL'}")
    print(f"  container exit: {container_exit}")
    print(f"  elapsed:        {elapsed:.1f}s")
    print(f"  logs:           {logs_dir}")
    if ctrf_path.exists():
        try:
            ctrf = json.loads(ctrf_path.read_text(encoding="utf-8"))
            summary = ctrf.get("results", {}).get("summary", {})
            if summary:
                print(
                    f"  tests:          {summary.get('passed', 0)} passed, "
                    f"{summary.get('failed', 0)} failed, "
                    f"{summary.get('skipped', 0)} skipped "
                    f"(of {summary.get('tests', 0)})"
                )
        except Exception as exc:
            print(f"  (could not parse ctrf.json: {exc})")
    print("=" * 60)
    return 0 if passed else 1


def run_local_smoke(task_root: Path, args: argparse.Namespace) -> int:
    require_executable("docker")
    project = load_smoke_project(task_root)
    image_tag = args.smoke_image_tag or (
        f"comp-smoke/{slug(project.name)}-{uuid4().hex[:8]}"
    )
    logs_dir = (
        args.smoke_logs_dir.resolve()
        if args.smoke_logs_dir
        else project.root / ".runner-logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    )
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "verifier").mkdir(exist_ok=True)

    env = smoke_project_env(project.task_toml)
    for env_file in args.smoke_env_file:
        env.update(parse_smoke_env_file(env_file))
    for raw in args.smoke_env:
        key, value = parse_smoke_env_arg(raw)
        env[key] = value

    print(f"smoke project: {project.name}")
    print(f"root:          {project.root}")
    print(f"image:         {image_tag}")
    print(f"logs:          {logs_dir}")
    if env:
        redacted = ", ".join(
            f"{key}={'***' if any(secret in key.upper() for secret in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')) else value}"
            for key, value in env.items()
        )
        print(f"env:           {redacted}")

    started = time.time()
    try:
        build_smoke_image(project, image_tag, args.smoke_no_cache)
        container_exit = run_smoke_container(
            project,
            image_tag,
            logs_dir,
            args.smoke_keep_container,
            env,
        )
    finally:
        if not args.smoke_keep_image:
            subprocess.run(
                ["docker", "image", "rm", "-f", image_tag],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    return summarize_smoke(logs_dir, container_exit, time.time() - started)


def resolve_quick_agent(requested: str) -> tuple[str, str]:
    """Resolve the host CLI used by ``--quick`` without installing anything."""
    aliases = {
        "claude-code": "claude",
        "claude": "claude",
        "codex": "codex",
    }
    normalized = requested.strip().lower()
    if normalized == "auto":
        # Match the local skill wrappers: Codex is preferred when both are
        # installed, but an explicit --quick-agent remains available.
        candidates = ("codex", "claude")
    else:
        canonical = aliases.get(normalized)
        if canonical is None:
            choices = ", ".join(QUICK_AGENT_CHOICES)
            raise SystemExit(
                f"error: --quick-agent must be one of {choices}; got {requested!r}"
            )
        candidates = (canonical,)

    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            return candidate, executable

    if normalized == "auto":
        raise SystemExit(
            "error: --quick needs an installed local agent CLI; "
            "install Codex (`codex`) or Claude Code (`claude`) and authenticate it first"
        )
    candidate = candidates[0]
    raise SystemExit(
        f"error: --quick-agent {candidate!r} is not installed on PATH; "
        f"install or authenticate `{candidate}` first"
    )


def quick_trial_paths(
    jobs_dir: Path,
    run_id: str,
    agent: str,
) -> QuickTrialPaths:
    """Return isolated, non-destructive paths for a quick trial."""
    jobs_dir = jobs_dir.expanduser().resolve()
    job_name = slug(f"{run_id}-quick-{agent}")
    job_dir = jobs_dir / job_name
    trial_name = f"{run_id}.agent__{agent}"
    trial_dir = job_dir / trial_name
    return QuickTrialPaths(
        job_name=job_name,
        job_dir=job_dir,
        trial_dir=trial_dir,
        workspace_dir=job_dir / "workspace",
        runner_log=jobs_dir / f"{job_name}.runner.log",
    )


def quick_agent_prompt(workspace_dir: Path) -> str:
    """Explain the host-path mapping that replaces Harbor's /workspace mount."""
    workspace = str(workspace_dir.resolve())
    return (
        "Solve the Harbor task in this local quick trial. Read the task contract "
        f"at {workspace}/instruction.md, inspect the public inputs under "
        f"{workspace}/data, and write every required deliverable under "
        f"{workspace}/output. In the normal Harbor container these paths are "
        "called /workspace/data and /workspace/output; in this host-local trial "
        f"use the mapped host directory {workspace} instead and do not use the "
        "literal /workspace path. Work only inside the mapped workspace, do not "
        "look for solution or verifier files, and do not stop until the requested "
        "output files are present."
    )


def build_quick_agent_command(
    agent: str,
    executable: str,
    workspace_dir: Path,
    prompt: str,
) -> list[str]:
    """Build a non-interactive command for the already-installed host CLI."""
    workspace = str(workspace_dir.resolve())
    if agent == "codex":
        return [
            executable,
            "--ask-for-approval",
            "never",
            "exec",
            "-C",
            workspace,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            prompt,
        ]
    if agent == "claude":
        return [
            executable,
            "--print",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
            "--allow-dangerously-skip-permissions",
            "--add-dir",
            workspace,
            "--",
            prompt,
        ]
    raise SystemExit(f"error: unsupported quick agent {agent!r}")


def load_quick_project(task_root: Path) -> SmokeProject:
    """Load the public task pieces needed for a host-agent/local-verifier run."""
    config = load_toml(task_root / "task.toml")
    environment = config.get("environment", {})
    if not isinstance(environment, dict):
        raise SystemExit(f"error: [environment] must be a TOML table: {task_root / 'task.toml'}")

    required = (task_root / "tests" / "test.sh",)
    missing = [str(path.relative_to(task_root)) for path in required if not path.is_file()]
    docker_image = environment.get("docker_image")
    if docker_image is not None and not isinstance(docker_image, str):
        missing.append("[environment].docker_image must be a string")
    if not docker_image and not (task_root / "environment" / "Dockerfile").is_file():
        missing.append("environment/Dockerfile or [environment].docker_image")
    if missing:
        raise SystemExit(
            f"error: {task_root} is missing quick-mode files: {', '.join(missing)}"
        )

    task_config = config.get("task")
    name = task_config.get("name") if isinstance(task_config, dict) else None
    return SmokeProject(root=task_root.resolve(), name=str(name or task_root.name))


def stage_quick_workspace(task_root: Path, workspace_dir: Path) -> None:
    """Stage only the task contract and public runtime inputs for the host CLI."""
    if workspace_dir.exists():
        raise SystemExit(f"error: quick workspace already exists: {workspace_dir}")
    workspace_dir.mkdir(parents=True, exist_ok=False)

    instruction = task_root / "instruction.md"
    if not instruction.is_file():
        raise SystemExit(f"error: task is missing instruction.md: {instruction}")
    shutil.copy2(instruction, workspace_dir / "instruction.md")

    environment_dir = task_root / "environment"
    data_dir = environment_dir / "data"
    if data_dir.is_dir():
        shutil.copytree(data_dir, workspace_dir / "data", symlinks=True)
    else:
        (workspace_dir / "data").mkdir()

    # A Dockerfile's other public runtime files can be useful to an agent, but
    # the image build recipe and wheelhouse are authoring/runtime metadata. In
    # particular, never copy solution/ or tests/ into the agent workspace.
    if environment_dir.is_dir():
        for child in sorted(environment_dir.iterdir(), key=lambda path: path.name):
            if child.name in {"Dockerfile", "data", "wheels"}:
                continue
            destination = workspace_dir / child.name
            if child.is_dir():
                shutil.copytree(child, destination, symlinks=True)
            elif child.is_file():
                shutil.copy2(child, destination)

    (workspace_dir / "output").mkdir()


def quick_process_environment(args: argparse.Namespace) -> dict[str, str]:
    """Build the host agent environment without leaking env-file values to logs."""
    environment = os.environ.copy()
    for env_file in args.env_file:
        values = read_dotenv_values(env_file)
        for key, value in values.items():
            if key not in os.environ:
                environment[key] = value
    for item in args.agent_env:
        key, value = parse_key_value(item, "--agent-env")
        environment[key] = value
    return environment


def quick_verifier_environment(args: argparse.Namespace) -> dict[str, str]:
    """Build only the explicit environment passed to the local verifier."""
    environment: dict[str, str] = {}
    for env_file in args.env_file:
        environment.update(read_dotenv_values(env_file))
    for item in args.verifier_env:
        key, value = parse_key_value(item, "--verifier-env")
        environment[key] = value
    return environment


def quick_timeout_seconds(
    task_root: Path,
    section: str,
    override: float | None,
    multiplier: float,
    fallback: float,
) -> float:
    if override is not None:
        timeout = override
    else:
        config = load_toml(task_root / "task.toml")
        section_config = config.get(section, {})
        configured = section_config.get("timeout_sec") if isinstance(section_config, dict) else None
        try:
            timeout = float(configured) if configured is not None else fallback
        except (TypeError, ValueError):
            timeout = fallback
        timeout *= multiplier
    if timeout <= 0:
        raise SystemExit(f"error: quick {section} timeout must be positive")
    return timeout


def build_quick_verifier_image(
    project: SmokeProject,
    args: argparse.Namespace,
) -> tuple[str, bool]:
    """Build or select the local runtime image used by the verifier."""
    config = load_toml(project.task_toml)
    environment = config.get("environment", {})
    configured_image = environment.get("docker_image") if isinstance(environment, dict) else None
    if configured_image:
        return str(configured_image), False

    image_tag = getattr(args, "quick_image_tag", None) or (
        f"comp-quick/{slug(project.name)}-{uuid4().hex[:8]}"
    )
    command = [
        "docker",
        "build",
        "--platform",
        MODAL_PLATFORM,
        "-t",
        image_tag,
    ]
    if getattr(args, "force_build", False):
        command.append("--no-cache")
    command.append(str(project.dockerfile_dir))
    result = run_smoke_command(command)
    if result.returncode != 0:
        raise SystemExit(2)
    # An explicit tag may refer to an image the user owns already; never remove
    # it automatically. Generated quick tags are safe to clean up by default.
    remove_image = not getattr(args, "quick_keep_image", False) and not getattr(
        args, "quick_image_tag", None
    )
    return image_tag, remove_image


def run_quick_verifier_container(
    project: SmokeProject,
    image_tag: str,
    trial: QuickTrialPaths,
    args: argparse.Namespace,
    timeout_sec: float,
) -> int:
    """Run the verifier locally with the task's network policy and no Modal."""
    logs_dir = trial.trial_dir / "verifier"
    logs_dir.mkdir(parents=True, exist_ok=True)
    container_name = f"comp-quick-{uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--platform",
        MODAL_PLATFORM,
        *docker_network_args(task_network_mode(project.root)),
        "--name",
        container_name,
        "-v",
        f"{trial.workspace_dir / 'output'}:/workspace/output",
        "-v",
        f"{project.tests_dir}:/tests:ro",
        "-v",
        f"{trial.trial_dir}:/logs",
    ]
    for key, value in quick_verifier_environment(args).items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([image_tag, "bash", "-c", "bash /tests/test.sh"])

    try:
        print(f"quick verifier timeout: {timeout_sec:.1f}s", flush=True)
        completed = subprocess.run(
            command,
            timeout=timeout_sec,
            check=False,
        )
        return completed.returncode
    except subprocess.TimeoutExpired:
        print("quick verifier timed out; stopping its local container", file=sys.stderr, flush=True)
        return 124
    finally:
        if not getattr(args, "quick_keep_container", False):
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            print(f"(container retained: {container_name})", flush=True)


def read_quick_reward(path: Path) -> float | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        reward = float(raw)
    except (OSError, ValueError):
        return None
    return reward if math.isfinite(reward) else None


def write_quick_trial_result(
    *,
    trial: QuickTrialPaths,
    task_root: Path,
    agent: str,
    started_at: str,
    finished_at: str | None,
    agent_returncode: int,
    verifier_returncode: int | None,
    reward: float | None,
    exception: tuple[str, str] | None,
    agent_elapsed_sec: float | None,
    verifier_elapsed_sec: float | None,
    total_elapsed_sec: float,
) -> Path:
    payload: dict[str, object] = {
        "task_id": {"path": str(task_root.resolve())},
        "trial_id": trial.trial_dir.name,
        "started_at": started_at,
        "finished_at": finished_at,
        "agent": {"name": agent, "model": "configured-local"},
        "agent_returncode": agent_returncode,
        "verifier_returncode": verifier_returncode,
        "verifier_result": {
            "rewards": {"reward": reward},
            "returncode": verifier_returncode,
        },
        "timing": {
            "agent_elapsed_sec": (
                round(agent_elapsed_sec, 3) if agent_elapsed_sec is not None else None
            ),
            "verifier_elapsed_sec": (
                round(verifier_elapsed_sec, 3)
                if verifier_elapsed_sec is not None
                else None
            ),
            "total_elapsed_sec": round(total_elapsed_sec, 3),
        },
    }
    if exception is not None:
        exception_type_name, message = exception
        payload["exception_info"] = {
            "exception_type": exception_type_name,
            "message": message,
        }

    result_path = trial.trial_dir / "result.json"
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return result_path


def write_quick_markdown_summary(
    *,
    jobs_dir: Path,
    task_root: Path,
    result: JobResult,
    reward: float | None,
    exception: str | None,
    run_id: str,
) -> Path:
    summary_path = jobs_dir / f"{run_id}.quick-summary.md"
    status = "pass" if result.returncode == 0 and reward is not None and reward > 0 else "fail"
    if exception:
        status = f"error ({exception})"
    lines = [
        f"# Harbor Quick Trial Summary: {run_id}",
        "",
        f"- Updated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- Task: `{task_root.name}`",
        f"- Agent: `{result.agent}` (configured local CLI)",
        f"- Trial: `{result.job_name}`",
        "",
        "| Task | Agent | Reward | Status |",
        "| --- | --- | ---: | --- |",
        f"| {markdown_escape(task_root.name)} | {markdown_escape(result.agent)} | "
        f"{format_metric(reward)} | {markdown_escape(status)} |",
        "",
        f"Evidence: `{result.job_dir}`",
    ]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def archive_quick_trial(
    *,
    trial: QuickTrialPaths,
    agent: str,
    destination_root: Path,
    run_id: str,
    summary_path: Path,
    markdown_summary_path: Path,
) -> Path:
    """Keep quick evidence under a namespaced archive, not campaign agent dirs."""
    archive_root = destination_root.expanduser().resolve() / "quick" / run_id
    if archive_root.exists():
        raise SystemExit(f"error: quick trajectory archive already exists: {archive_root}")
    destination = archive_root / slug(agent) / trial.trial_dir.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(trial.trial_dir, destination, symlinks=True)
    copy_file_if_exists(summary_path, archive_root / summary_path.name)
    copy_file_if_exists(markdown_summary_path, archive_root / "summary.md")
    return archive_root


def run_quick_trial(task_root: Path, args: argparse.Namespace) -> int:
    """Run one host-installed agent CLI and one local verifier trial."""
    if args.run:
        raise SystemExit("error: --quick cannot be combined with --run; choose --quick-agent")
    incompatible = {
        "--resume": args.resume,
        "--archive-only": args.archive_only,
        "--oracle-sort": args.oracle_sort,
        "--smoke-test": args.smoke_test,
        "--modal-secret": args.modal_secret,
        "--environment-kwarg": args.environment_kwarg,
        "--agent-kwarg": args.agent_kwarg,
        "--artifact": args.artifact,
        "--no-delete": args.no_delete,
        "--smoke-env": args.smoke_env,
        "--smoke-env-file": args.smoke_env_file,
    }
    used = [flag for flag, value in incompatible.items() if value]
    if used:
        raise SystemExit(
            "error: --quick cannot be combined with " + ", ".join(used)
        )
    for env_file in args.env_file:
        if not env_file.is_file():
            raise SystemExit(f"error: --env-file does not exist: {env_file}")
    for item in args.agent_env:
        parse_key_value(item, "--agent-env")
    for item in args.verifier_env:
        parse_key_value(item, "--verifier-env")
    if args.quick_timeout_sec is not None and args.quick_timeout_sec <= 0:
        raise SystemExit("error: --quick-timeout-sec must be positive")

    agent, executable = resolve_quick_agent(args.quick_agent)
    project = load_quick_project(task_root)
    trial = quick_trial_paths(args.jobs_dir, args.run_id, agent)
    if trial.job_dir.exists() and not args.dry_run:
        raise SystemExit(
            f"error: quick job already exists: {trial.job_dir}; choose a new --run-id"
        )
    prompt = quick_agent_prompt(trial.workspace_dir)
    agent_command = build_quick_agent_command(
        agent,
        executable,
        trial.workspace_dir,
        prompt,
    )
    project_config = load_toml(project.task_toml)
    project_environment = project_config.get("environment", {})
    configured_image = (
        project_environment.get("docker_image")
        if isinstance(project_environment, dict)
        else None
    )
    image_description = (
        "[environment].docker_image"
        if configured_image
        else str(project.dockerfile_dir)
    )
    overview = [
        f"run-id:    {args.run_id}",
        "action:    quick local single trial",
        f"agent:     {agent} ({executable})",
        f"task root: {task_root}",
        f"workspace: {trial.workspace_dir}",
        f"verifier:  local Docker ({image_description})",
        (
            "network:   host agent as configured; verifier container follows "
            f"network_mode={task_network_mode(project.root)}"
        ),
        f"job:       {trial.job_dir}",
        f"log:       {trial.runner_log}",
    ]
    print_runner_panel("Harbor quick trial", overview)
    print_runner_panel("Local agent command", [redacted_command(agent_command)], border_style="dim")
    if args.dry_run:
        return 0

    require_executable("docker")
    trial.job_dir.mkdir(parents=True, exist_ok=False)
    stage_quick_workspace(task_root, trial.workspace_dir)
    config_payload = {
        "version": 1,
        "quick": True,
        "task": {"path": str(task_root.resolve())},
        "agents": [{"name": agent, "model": "configured-local"}],
        "environment": {"type": "host-local-agent-and-docker-verifier"},
    }
    (trial.job_dir / "config.json").write_text(
        json.dumps(config_payload, indent=2) + "\n", encoding="utf-8"
    )

    agent_timeout = quick_timeout_seconds(
        task_root,
        "agent",
        args.quick_timeout_sec,
        args.agent_timeout_multiplier,
        QUICK_DEFAULT_AGENT_TIMEOUT_SEC,
    )
    verifier_timeout = quick_timeout_seconds(
        task_root,
        "verifier",
        None,
        args.timeout_multiplier,
        QUICK_DEFAULT_VERIFIER_TIMEOUT_SEC,
    )
    started_clock = time.time()
    total_started_clock = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    agent_returncode = 2
    verifier_returncode: int | None = None
    exception: tuple[str, str] | None = None
    interrupted = False
    agent_started_clock: float | None = None
    agent_elapsed_sec: float | None = None
    verifier_elapsed_sec: float | None = None
    image_tag = ""
    remove_image = False

    try:
        agent_environment = quick_process_environment(args)
        # Parse all explicit env inputs before building an image so malformed
        # configuration cannot leave a generated image behind.
        quick_verifier_environment(args)
        image_tag, remove_image = build_quick_verifier_image(project, args)
        with trial.runner_log.open("w", encoding="utf-8") as log:
            log.write(f"$ {redacted_command(agent_command)}\n\n")
            log.write(f"workspace: {trial.workspace_dir}\n")
            log.write(f"agent timeout: {agent_timeout:.1f}s\n")
            log.flush()
            process: subprocess.Popen[str] | None = None
            progress_tty = False

            def clear_agent_progress() -> None:
                if progress_tty:
                    sys.stdout.write("\r\033[2K")
                    sys.stdout.flush()

            try:
                process = subprocess.Popen(
                    agent_command,
                    cwd=trial.workspace_dir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=agent_environment,
                )
                with PROCESS_LOCK:
                    RUNNING_PROCESSES[trial.job_name] = process
                agent_started_clock = time.monotonic()
                agent_started = agent_started_clock
                progress_interval = getattr(
                    args,
                    "progress_interval_sec",
                    LOCAL_DEFAULT_PROGRESS_INTERVAL_SECONDS,
                )
                progress_tty = bool(
                    getattr(sys.stdout, "isatty", lambda: False)()
                )
                display_interval = (
                    1.0 / QUICK_AGENT_PROGRESS_FPS
                    if progress_tty
                    else progress_interval
                )
                if display_interval <= 0:
                    display_interval = None
                next_progress_at = (
                    agent_started + display_interval
                    if display_interval is not None
                    else None
                )
                next_log_progress_at = (
                    agent_started + progress_interval
                    if progress_interval > 0
                    else None
                )
                progress_frame = 0

                def announce_agent_running(*, write_log: bool = False) -> None:
                    nonlocal progress_frame
                    line = (
                        (
                            f"[{QUICK_AGENT_PROGRESS_FRAMES[progress_frame % len(QUICK_AGENT_PROGRESS_FRAMES)]}] "
                            if progress_tty
                            else ""
                        )
                        + "Agent running... "
                        f"({format_elapsed(time.monotonic() - agent_started)} elapsed)"
                    )
                    if progress_tty:
                        sys.stdout.write("\r\033[2K" + line)
                        sys.stdout.flush()
                    else:
                        print(line, flush=True)
                    if write_log:
                        log.write(line + "\n")
                        log.flush()
                    progress_frame += 1

                # The agent's transcript is intentionally kept in the runner log,
                # so announce its start and keep a visible heartbeat on the host.
                announce_agent_running(write_log=True)
                deadline = agent_started + agent_timeout
                try:
                    while True:
                        now = time.monotonic()
                        remaining = deadline - now
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(
                                process.args,
                                agent_timeout,
                            )
                        wait_timeout = min(5.0, remaining)
                        if next_progress_at is not None:
                            wait_timeout = min(
                                wait_timeout,
                                max(0.1, next_progress_at - now),
                            )
                        try:
                            agent_returncode = process.wait(timeout=wait_timeout)
                            break
                        except subprocess.TimeoutExpired:
                            now = time.monotonic()
                            if (
                                next_progress_at is not None
                                and now >= next_progress_at
                            ):
                                write_log = (
                                    not progress_tty
                                    or (
                                        next_log_progress_at is not None
                                        and now >= next_log_progress_at
                                    )
                                )
                                announce_agent_running(write_log=write_log)
                                if write_log and next_log_progress_at is not None:
                                    while next_log_progress_at <= now:
                                        next_log_progress_at += progress_interval
                                while next_progress_at <= now:
                                    next_progress_at += display_interval
                            if now >= deadline:
                                raise
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    agent_returncode = 124
                    exception = (
                        "QuickAgentTimeout",
                        f"local {agent} CLI exceeded {agent_timeout:.1f}s",
                    )
                except KeyboardInterrupt:
                    interrupted = True
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    agent_returncode = 130
            except OSError as exc:
                exception = ("QuickAgentLaunchError", str(exc))
                agent_returncode = 127
            finally:
                clear_agent_progress()
                if process is not None:
                    with PROCESS_LOCK:
                        RUNNING_PROCESSES.pop(trial.job_name, None)
                if agent_started_clock is not None:
                    agent_elapsed_sec = time.monotonic() - agent_started_clock
            log.write(f"\nagent exit: {agent_returncode}\n")
            log.flush()
    finally:
        transcript_dir = trial.trial_dir / "agent"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        if trial.runner_log.is_file():
            shutil.copy2(trial.runner_log, transcript_dir / f"{agent}.log")

    if not interrupted:
        verifier_started_clock = time.monotonic()
        try:
            verifier_returncode = run_quick_verifier_container(
                project,
                image_tag,
                trial,
                args,
                verifier_timeout,
            )
        except KeyboardInterrupt:
            interrupted = True
            verifier_returncode = 130
        except (OSError, subprocess.SubprocessError) as exc:
            verifier_returncode = 2
            exception = exception or ("QuickVerifierLaunchError", str(exc))
        finally:
            verifier_elapsed_sec = time.monotonic() - verifier_started_clock

    reward_path = trial.trial_dir / "verifier" / "reward.txt"
    raw_reward = read_quick_reward(reward_path)
    if agent_returncode != 0 and exception is None:
        exception = (
            "QuickAgentError",
            f"local {agent} CLI exited with {agent_returncode}",
        )
    if (
        not interrupted
        and verifier_returncode is not None
        and not reward_path.is_file()
        and exception is None
    ):
        exception = (
            "QuickVerifierError",
            f"verifier exited with {verifier_returncode} without writing reward.txt",
        )
    reward = raw_reward if agent_returncode == 0 and not interrupted else 0.0
    finished_at = None if interrupted else time.strftime("%Y-%m-%dT%H:%M:%S%z")

    artifacts_dir = trial.trial_dir / "artifacts"
    output_dir = trial.workspace_dir / "output"
    if output_dir.is_dir():
        shutil.copytree(output_dir, artifacts_dir / "output", symlinks=True)

    total_elapsed_sec = time.monotonic() - total_started_clock
    write_quick_trial_result(
        trial=trial,
        task_root=task_root,
        agent=agent,
        started_at=started_at,
        finished_at=finished_at,
        agent_returncode=agent_returncode,
        verifier_returncode=verifier_returncode,
        reward=reward,
        exception=exception,
        agent_elapsed_sec=agent_elapsed_sec,
        verifier_elapsed_sec=verifier_elapsed_sec,
        total_elapsed_sec=total_elapsed_sec,
    )
    trial_log_lines = [
        f"run_id: {args.run_id}",
        f"agent: {agent}",
        f"workspace: {trial.workspace_dir}",
        f"started_at: {started_at}",
        f"finished_at: {finished_at}",
        f"agent_returncode: {agent_returncode}",
        f"verifier_returncode: {verifier_returncode}",
        f"reward: {reward}",
        f"agent_elapsed_sec: {agent_elapsed_sec}",
        f"verifier_elapsed_sec: {verifier_elapsed_sec}",
        f"total_elapsed_sec: {total_elapsed_sec:.3f}",
    ]
    if exception:
        trial_log_lines.append(f"exception: {exception[0]}: {exception[1]}")
    (trial.trial_dir / "trial.log").write_text(
        "\n".join(trial_log_lines) + "\n", encoding="utf-8"
    )

    if interrupted:
        result_returncode = 130
    elif exception is not None:
        result_returncode = 2
    elif agent_returncode != 0:
        result_returncode = agent_returncode or 1
    elif verifier_returncode != 0:
        result_returncode = 1
    elif reward is None or reward <= 0:
        result_returncode = 1
    else:
        result_returncode = 0

    result = JobResult(
        agent=agent,
        model="configured-local",
        label=f"quick-{agent}",
        job_name=trial.job_name,
        n_trials_expected=1,
        returncode=result_returncode,
        elapsed_sec=round(time.time() - started_clock, 3),
        job_dir=str(trial.job_dir),
        runner_log=str(trial.runner_log),
        resumed=False,
    )
    summary_path = write_summary(args.jobs_dir.resolve(), task_root, args.run_id, [result])
    markdown_summary_path = write_quick_markdown_summary(
        jobs_dir=args.jobs_dir.resolve(),
        task_root=task_root,
        result=result,
        reward=reward,
        exception=exception[0] if exception else None,
        run_id=args.run_id,
    )
    (trial.job_dir / "job.log").write_text(
        trial.runner_log.read_text(encoding="utf-8") if trial.runner_log.is_file() else "",
        encoding="utf-8",
    )

    archive_path: Path | None = None
    try:
        if args.archive_completed:
            archive_path = archive_quick_trial(
                trial=trial,
                agent=agent,
                destination_root=args.completed_trajectories_dir,
                run_id=args.run_id,
                summary_path=summary_path,
                markdown_summary_path=markdown_summary_path,
            )
    finally:
        if remove_image and image_tag:
            subprocess.run(
                ["docker", "image", "rm", "-f", image_tag],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    status = "PASS" if result_returncode == 0 else ("INTERRUPTED" if interrupted else "FAIL")
    print()
    print(f"QUICK RESULT: {status}")
    print(f"  agent:     {agent}")
    print(f"  reward:    {reward}")
    print(f"  evidence:  {trial.trial_dir}")
    print(f"  summary:   {markdown_summary_path}")
    if archive_path is not None:
        print(f"  archive:   {archive_path}")
    return result_returncode


def validate_amd64_dockerfile(path: Path) -> list[str]:
    """Return architecture errors for a Dockerfile used by a Modal task.

    Modal builds and runs Linux/amd64 images. Requiring a literal platform on
    every FROM line makes the intended target reviewable and prevents a
    workstation's arm64 default from leaking into a task image.
    """
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"{path}: cannot read Dockerfile: {exc}"]

    from_lines = []
    for line_number, line in enumerate(lines, 1):
        match = DOCKERFILE_FROM_RE.match(line)
        if match:
            from_lines.append((line_number, match.group("platform")))

    if not from_lines:
        return [f"{path}: no FROM instruction was found"]

    for line_number, platform in from_lines:
        if platform != MODAL_PLATFORM:
            actual = platform or "unspecified"
            errors.append(
                f"{path}:{line_number}: FROM must use "
                f"--platform={MODAL_PLATFORM} (found {actual})"
            )
    return errors


def _manifest_platform(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    platform = value.get("platform") or value.get("Platform")
    if isinstance(platform, dict):
        operating_system = platform.get("os") or platform.get("OS")
        architecture = platform.get("architecture") or platform.get("Architecture")
        if operating_system or architecture:
            return str(operating_system) if operating_system else None, str(architecture) if architecture else None
    operating_system = value.get("os") or value.get("OS")
    architecture = value.get("architecture") or value.get("Architecture")
    if operating_system or architecture:
        return str(operating_system) if operating_system else None, str(architecture) if architecture else None
    for key in ("Descriptor", "descriptor"):
        nested = value.get(key)
        if nested is not None:
            found = _manifest_platform(nested)
            if found != (None, None):
                return found
    return None, None


def _contains_manifest_index(value: object) -> bool:
    if isinstance(value, dict):
        if "manifests" in value or "Manifests" in value:
            return True
        descriptor = value.get("Descriptor") or value.get("descriptor")
        if isinstance(descriptor, dict):
            media_type = str(descriptor.get("mediaType", "")).lower()
            if "image.index" in media_type or "manifest.list" in media_type:
                return True
        return any(_contains_manifest_index(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_manifest_index(item) for item in value)
    return False


def validate_prebuilt_image(image: str) -> list[str]:
    """Reject an OCI index/multi-arch prebuilt image before Modal sees it."""
    docker = shutil.which("docker")
    if docker is None:
        return [
            "prebuilt environment image "
            f"{image!r} cannot be checked: Docker CLI is not installed; "
            f"inspect it with `docker manifest inspect --verbose {image}` "
            f"and prove it is a single {MODAL_PLATFORM} image"
        ]

    try:
        completed = subprocess.run(
            [docker, "manifest", "inspect", "--verbose", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"prebuilt image {image!r} could not be inspected: {exc}"]
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return [
            f"prebuilt image {image!r} could not be inspected; "
            f"Modal must receive a single {MODAL_PLATFORM} image ({detail})"
        ]

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return [f"prebuilt image {image!r} returned invalid manifest JSON: {exc}"]

    if _contains_manifest_index(payload):
        return [
            f"prebuilt image {image!r} is an OCI manifest list/index; "
            f"publish a single {MODAL_PLATFORM} image instead"
        ]

    records = payload if isinstance(payload, list) else [payload]
    platforms: list[tuple[str | None, str | None]] = []
    for record in records:
        if isinstance(record, dict):
            platform = _manifest_platform(record)
            if platform != (None, None):
                platforms.append(platform)

    if not platforms:
        return [
            f"prebuilt image {image!r} has no inspectable OS/architecture; "
            f"Modal requires a single {MODAL_PLATFORM} image"
        ]
    if any(platform != ("linux", "amd64") for platform in platforms):
        formatted = ", ".join(f"{os_name}/{arch}" for os_name, arch in platforms)
        return [
            f"prebuilt image {image!r} is not a single {MODAL_PLATFORM} image "
            f"(manifest reports {formatted})"
        ]
    if len(platforms) != 1:
        return [
            f"prebuilt image {image!r} returned multiple platform records; "
            f"Modal accepts one {MODAL_PLATFORM} image, not an OCI index"
        ]
    return []


def validate_modal_task_policy(
    tasks: list[Path],
    backend: str,
    *,
    expected_network_mode: str = DEFAULT_NETWORK_MODE,
) -> None:
    """Validate the network and Modal image contract for a task snapshot."""
    if backend != "modal":
        raise SystemExit(
            "error: harbor_runner.py is intentionally Modal-only; "
            "use --env modal"
        )

    errors: list[str] = []
    for task in tasks:
        config_path = task / "task.toml"
        config = load_toml(config_path)
        environment = config.get("environment")
        if not isinstance(environment, dict):
            errors.append(f"{config_path}: missing [environment] table")
            continue
        if environment.get("network_mode") != expected_network_mode:
            policy = (
                "Oracle and agent execution policy"
                if expected_network_mode == "public"
                else "requested offline policy"
            )
            errors.append(
                f'{config_path}: [environment].network_mode must be "{expected_network_mode}" '
                f"for the {policy}"
            )

        environment_dir = task / "environment"
        environment_image = environment.get("docker_image")
        environment_dockerfile = environment_dir / "Dockerfile"
        if environment_image:
            if not isinstance(environment_image, str):
                errors.append(f"{config_path}: environment.docker_image must be a string")
            else:
                errors.extend(validate_prebuilt_image(environment_image))
        elif environment_dockerfile.is_file():
            errors.extend(validate_amd64_dockerfile(environment_dockerfile))
        else:
            errors.append(
                f"{task}: needs environment/Dockerfile or a checked prebuilt "
                "environment.docker_image"
            )

        verifier_dockerfile = task / "tests" / "Dockerfile"
        if verifier_dockerfile.is_file():
            errors.extend(validate_amd64_dockerfile(verifier_dockerfile))

    if errors:
        formatted = "\n".join(f"  - {error}" for error in errors)
        raise SystemExit(f"error: Modal preflight failed:\n{formatted}")


def merge_modal_secret_kwargs(args: argparse.Namespace) -> None:
    """Add Modal Secret names to Harbor's environment kwargs.

    Harbor's Modal adapter accepts `secrets=["name", ...]`. The values are
    resolved by Modal; no API-key value is read or placed in this command.
    """
    if not args.modal_secret:
        return
    if any(item.split("=", 1)[0].strip() == "secrets" for item in args.environment_kwarg):
        raise SystemExit(
            "error: use either --modal-secret or an explicit secrets=... "
            "--environment-kwarg, not both"
        )
    args.environment_kwarg.append(
        "secrets=" + json.dumps(args.modal_secret, separators=(",", ":"))
    )


def merge_modal_run_kwargs(args: argparse.Namespace) -> None:
    """Pin every Harbor job in this run to its own Modal App."""
    if any(item.split("=", 1)[0].strip() == "app_name" for item in args.environment_kwarg):
        raise SystemExit(
            "error: app_name is managed by harbor_runner.py; do not override it with "
            "--environment-kwarg"
        )
    args.environment_kwarg.append(
        "app_name=" + json.dumps(args.modal_app_name)
    )


def redacted_command(command: list[str]) -> str:
    """Format a command without placing credential values in local logs."""
    secret_flags = {
        "--agent-env",
        "--verifier-env",
        "--environment-kwarg",
        "--agent-kwarg",
    }
    rendered: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        rendered.append(token)
        if token in secret_flags and index + 1 < len(command):
            rendered.append("<redacted>")
            index += 2
            continue
        index += 1
    return " ".join(rendered)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def build_job_specs(
    task_root: Path,
    num_tasks: int,
    agent_specs: list[AgentSpec],
    args: argparse.Namespace,
) -> list[JobSpec]:
    specs: list[JobSpec] = []
    jobs_dir = args.jobs_dir.resolve()
    for spec in agent_specs:
        n_concurrent = args.n_concurrent or spec.n_concurrent
        job_name = slug(f"{args.run_id}-{spec.label}")
        job_dir = jobs_dir / job_name
        if args.resume:
            # `harbor jobs resume` re-reads the existing job's config (backend,
            # agent, attempts, etc.) and only runs the trials that aren't done.
            # Verify flags against `harbor jobs resume --help` for your version.
            command = ["harbor", "jobs", "resume", "--job-path", str(job_dir)]
        else:
            command = [
                "harbor", "run",
                "--path", str(task_root),
                "--agent", spec.agent,
                "--model", spec.model,
                "--n-attempts", str(args.repeats),
                "--n-concurrent", str(n_concurrent),
                "--env", args.env,
                "--jobs-dir", str(jobs_dir),
                "--job-name", job_name,
                "--agent-timeout-multiplier", str(args.agent_timeout_multiplier),
                "--timeout-multiplier", str(args.timeout_multiplier),
                "--yes",
            ]
            if args.force_build:
                command.append("--force-build")
            if args.no_delete:
                command.append("--no-delete")
            for env_file in args.env_file:
                command.extend(["--env-file", str(env_file)])
            for item in args.agent_env:
                command.extend(["--agent-env", item])
            for item in args.verifier_env:
                command.extend(["--verifier-env", item])
            for item in args.environment_kwarg:
                command.extend(["--environment-kwarg", item])
            for item in args.agent_kwarg:
                command.extend(["--agent-kwarg", item])
            for artifact in args.artifact:
                command.extend(["--artifact", artifact])

        specs.append(
            JobSpec(
                task_root=task_root,
                num_tasks=num_tasks,
                agent=spec.agent,
                model=spec.model,
                label=spec.label,
                n_concurrent=n_concurrent,
                repeats=args.repeats,
                job_name=job_name,
                jobs_dir=jobs_dir,
                job_dir=job_dir,
                command=command,
                runner_log=jobs_dir / f"{job_name}.runner.log",
                resume=args.resume,
                completion_grace_sec=args.completion_grace_sec,
                progress_interval_sec=args.progress_interval_sec,
            )
        )
    return specs


def build_oracle_sort_job_spec(
    task_root: Path,
    num_tasks: int,
    args: argparse.Namespace,
) -> OracleSortJobSpec:
    jobs_dir = args.jobs_dir.resolve()
    job_name = slug(f"{args.run_id}-oracle")
    job_dir = jobs_dir / job_name
    if args.resume:
        command = ["harbor", "jobs", "resume", "--job-path", str(job_dir)]
    else:
        command = [
            "harbor",
            "run",
            "--path", str(task_root),
            "--agent", "oracle",
            "--n-attempts", "1",
            "--n-concurrent", str(args.n_concurrent or args.oracle_concurrency),
            "--env", args.env,
            "--jobs-dir", str(jobs_dir),
            "--job-name", job_name,
            "--timeout-multiplier", str(args.timeout_multiplier),
            "--yes",
        ]
        if args.force_build:
            command.append("--force-build")
        if args.no_delete:
            command.append("--no-delete")
        for env_file in args.env_file:
            command.extend(["--env-file", str(env_file)])
        for item in args.verifier_env:
            command.extend(["--verifier-env", item])
        for item in args.environment_kwarg:
            command.extend(["--environment-kwarg", item])
        for artifact in args.artifact:
            command.extend(["--artifact", artifact])

    return OracleSortJobSpec(
        task_root=task_root,
        num_tasks=num_tasks,
        job_name=job_name,
        jobs_dir=jobs_dir,
        job_dir=job_dir,
        command=command,
        runner_log=jobs_dir / f"{job_name}.runner.log",
        resume=args.resume,
        completion_grace_sec=args.completion_grace_sec,
        progress_interval_sec=args.progress_interval_sec,
    )


def completed_trial_result_count(job_dir: Path, expected: int) -> int | None:
    result_paths = sorted(job_dir.glob("*/result.json"))
    if len(result_paths) < expected:
        return None

    completed = 0
    for result_path in result_paths:
        try:
            result = load_json(result_path)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return None
        if not result.get("finished_at"):
            return None
        completed += 1
    return completed


def summarize_job_progress(
    job_dir: Path,
    expected_trials: int,
    repeats: int,
) -> JobProgress:
    result_paths = sorted(job_dir.glob("*/result.json"))
    finished_trials = 0
    passed_trials = 0
    failed_trials = 0
    errored_trials = 0
    finished_by_task: dict[str, int] = {}

    for result_path in result_paths:
        try:
            trial_result = load_json(result_path)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            continue
        if not trial_result.get("finished_at"):
            continue

        finished_trials += 1
        reward = trial_reward(trial_result)
        errored = exception_type(trial_result) is not None
        if reward is not None and reward > 0:
            passed_trials += 1
        else:
            failed_trials += 1
        if errored:
            errored_trials += 1

        task_path = task_path_from_trial_result(trial_result, result_path)
        task_key = (
            str(task_path) if task_path is not None else result_path.parent.name
        )
        finished_by_task[task_key] = finished_by_task.get(task_key, 0) + 1

    complete_tasks = sum(
        1 for count in finished_by_task.values() if count >= repeats
    )
    return JobProgress(
        expected_trials=expected_trials,
        result_files=len(result_paths),
        finished_trials=finished_trials,
        passed_trials=passed_trials,
        failed_trials=failed_trials,
        errored_trials=errored_trials,
        complete_tasks=complete_tasks,
        total_tasks_seen=len(finished_by_task),
    )


def format_job_progress(spec: JobSpec, progress: JobProgress) -> str:
    percent = (
        100.0 * progress.finished_trials / progress.expected_trials
        if progress.expected_trials
        else 100.0
    )
    return (
        f"progress: {spec.label} "
        f"{progress.finished_trials}/{progress.expected_trials} trials "
        f"({percent:.1f}%), "
        f"tasks complete {progress.complete_tasks}/{spec.num_tasks}, "
        f"pass/fail {progress.passed_trials}/{progress.failed_trials}, "
        f"errors {progress.errored_trials}"
    )


class ProgressReporter:
    """Render a stable, ordered scoreboard for concurrent agent jobs."""

    def __init__(self, specs: list[JobSpec], stream: object | None = None) -> None:
        self._specs = tuple(specs)
        self._latest: dict[str, str] = {}
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def report(self, spec: JobSpec, line: str) -> None:
        with self._lock:
            self._latest[spec.job_name] = line
            self._render()

    def complete(self, spec: JobSpec) -> None:
        with self._lock:
            self._latest[spec.job_name] = "completed; collecting the final job result"
            self._render()

    def _render(self) -> None:
        console = runner_console(self._stream)
        if console is not None:
            scoreboard = Table.grid(padding=(0, 1))
            scoreboard.add_column()
            for spec in self._specs:
                line = self._latest.get(spec.job_name, f"waiting for {spec.label} to start")
                scoreboard.add_row(_rich_text(f"{spec.label}: {line}"))
            console.print(Panel(scoreboard, title="Agent progress", border_style="cyan"))
            return

        print("agent progress update:", file=self._stream, flush=True)
        for spec in self._specs:
            line = self._latest.get(spec.job_name, f"waiting for {spec.label} to start")
            print(f"  {spec.label}: {line}", file=self._stream, flush=True)
        print("=============", file=self._stream, flush=True)


def run_job(
    spec: JobSpec,
    progress_reporter: ProgressReporter | None = None,
) -> JobResult:
    spec.jobs_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    env = os.environ.copy()
    mode = "a" if spec.resume else "w"
    returncode = 2
    expected_trials = spec.num_tasks * spec.repeats
    completed_since: float | None = None
    next_progress_at = (
        started + spec.progress_interval_sec
        if spec.progress_interval_sec > 0
        else started
    )
    with spec.runner_log.open(mode, encoding="utf-8") as log:
        log.write(f"\n$ {redacted_command(spec.command)}\n\n")
        log.flush()
        process = subprocess.Popen(
            spec.command,
            cwd=spec.task_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        start_line = (
            f"agent job started: {spec.label} ({spec.agent}, {spec.model}); "
            f"expecting {expected_trials} trial(s)"
        )
        if progress_reporter is None:
            print(start_line, flush=True)
        else:
            progress_reporter.report(spec, start_line)
        with PROCESS_LOCK:
            RUNNING_PROCESSES[spec.job_name] = process
        try:
            while True:
                try:
                    returncode = process.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    now = time.time()
                    if (
                        spec.progress_interval_sec > 0
                        and now >= next_progress_at
                    ):
                        progress = summarize_job_progress(
                            spec.job_dir,
                            expected_trials,
                            spec.repeats,
                        )
                        line = (
                            f"{format_job_progress(spec, progress)}, "
                            f"elapsed {format_elapsed(now - started)}"
                        )
                        if progress_reporter is None:
                            print(line, flush=True)
                            print("=============", flush=True)
                        else:
                            progress_reporter.report(spec, line)
                        log.write(line + "\n")
                        log.flush()
                        next_progress_at = now + spec.progress_interval_sec

                    completed = completed_trial_result_count(
                        spec.job_dir, expected_trials
                    )
                    if completed is None:
                        completed_since = None
                        continue

                    if completed_since is None:
                        completed_since = now
                        log.write(
                            "\ncompletion escape hatch: "
                            f"{completed}/{expected_trials} trial results are finished; "
                            f"waiting {spec.completion_grace_sec:.1f}s for Harbor to exit\n"
                        )
                        log.flush()
                        continue

                    if now - completed_since < spec.completion_grace_sec:
                        continue

                    log.write(
                        "\ncompletion escape hatch: Harbor is still running after "
                        f"{spec.completion_grace_sec:.1f}s with all trial results "
                        "finished; terminating local Harbor process and marking "
                        "job successful\n"
                    )
                    log.flush()
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        log.write(
                            "completion escape hatch: Harbor did not terminate; "
                            "killing local Harbor process\n"
                        )
                        log.flush()
                        process.kill()
                        process.wait()
                    returncode = 0
                    break
        finally:
            with PROCESS_LOCK:
                RUNNING_PROCESSES.pop(spec.job_name, None)
            if progress_reporter is not None:
                progress_reporter.complete(spec)
    elapsed = time.time() - started
    return JobResult(
        agent=spec.agent,
        model=spec.model,
        label=spec.label,
        job_name=spec.job_name,
        n_trials_expected=spec.num_tasks * spec.repeats,
        returncode=returncode,
        elapsed_sec=round(elapsed, 3),
        job_dir=str(spec.job_dir),
        runner_log=str(spec.runner_log),
        resumed=spec.resume,
    )


def run_oracle_sort_job(
    spec: OracleSortJobSpec,
    on_poll: Callable[[], None] | None = None,
) -> int:
    spec.jobs_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    mode = "a" if spec.resume else "w"
    returncode = 2
    completed_since: float | None = None
    started = time.time()
    next_progress_at = started
    progress_enabled = spec.progress_interval_sec > 0
    progress_display = OracleProgressDisplay()
    with spec.runner_log.open(mode, encoding="utf-8") as log:
        log.write(f"\n$ {redacted_command(spec.command)}\n\n")
        log.flush()
        process = subprocess.Popen(
            spec.command,
            cwd=spec.task_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        print(
            f"Oracle job started: {spec.job_name}; expecting {spec.num_tasks} result(s)",
            flush=True,
        )
        if progress_enabled:
            progress_display.update(format_oracle_progress(spec, started))
        with PROCESS_LOCK:
            RUNNING_PROCESSES[spec.job_name] = process
        try:
            while True:
                try:
                    returncode = process.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    now = time.time()
                    if progress_enabled:
                        progress_display.prepare_for_external_output()
                    if on_poll is not None:
                        try:
                            on_poll()
                        except Exception as exc:
                            log.write(f"incremental sort hook failed: {exc!r}\n")
                            log.flush()
                    progress_line: str | None = None
                    if progress_enabled and progress_display.is_tty:
                        progress_line = format_oracle_progress(spec, started)
                        progress_display.update(progress_line)
                    if (
                        spec.progress_interval_sec > 0
                        and now >= next_progress_at
                    ):
                        line = progress_line or format_oracle_progress(spec, started)
                        if not progress_display.is_tty:
                            print(line, flush=True)
                        log.write(line + "\n")
                        log.flush()
                        next_progress_at = now + spec.progress_interval_sec
                    completed = completed_trial_result_count(
                        spec.job_dir, spec.num_tasks
                    )
                    if completed is None:
                        completed_since = None
                        continue

                    now = time.time()
                    if completed_since is None:
                        completed_since = now
                        log.write(
                            "\ncompletion escape hatch: "
                            f"{completed}/{spec.num_tasks} trial results are finished; "
                            f"waiting {spec.completion_grace_sec:.1f}s for Harbor to exit\n"
                        )
                        log.flush()
                        continue

                    if now - completed_since < spec.completion_grace_sec:
                        continue

                    log.write(
                        "\ncompletion escape hatch: Harbor is still running after "
                        f"{spec.completion_grace_sec:.1f}s with all trial results "
                        "finished; terminating local Harbor process and marking "
                        "job successful\n"
                    )
                    log.flush()
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        log.write(
                            "completion escape hatch: Harbor did not terminate; "
                            "killing local Harbor process\n"
                        )
                        log.flush()
                        process.kill()
                        process.wait()
                    returncode = 0
                    break
        finally:
            with PROCESS_LOCK:
                RUNNING_PROCESSES.pop(spec.job_name, None)
            progress_display.finish()
    return returncode


def format_oracle_progress(spec: OracleSortJobSpec, started_at: float) -> str:
    result_paths = sorted(spec.job_dir.glob("*/result.json"))
    finished = 0
    passed = 0
    exceptions = 0
    for result_path in result_paths:
        try:
            result = load_json(result_path)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            continue
        if not result.get("finished_at"):
            continue
        finished += 1
        reward = trial_reward(result)
        if reward is not None and reward > 0:
            passed += 1
        if exception_type(result) is not None:
            exceptions += 1
    failed = max(0, finished - passed)
    expected = spec.num_tasks
    percent = 100.0 * finished / expected if expected else 100.0
    return (
        f"Oracle progress: {finished}/{expected} result(s) finished "
        f"({percent:.1f}%), result files {len(result_paths)}, "
        f"rewarded {passed}, not rewarded {failed}, exceptions {exceptions}, "
        f"elapsed {format_elapsed(time.time() - started_at)}"
    )


class OracleProgressDisplay:
    """Show a live Oracle spinner on terminals without polluting redirected logs."""

    def __init__(self, stream: object | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        isatty = getattr(self.stream, "isatty", None)
        self.is_tty = bool(isatty()) if callable(isatty) else False
        self.frame = 0
        self.active = False

    def update(self, progress_line: str) -> None:
        if not self.is_tty:
            return
        frame = ORACLE_SPINNER_FRAMES[self.frame % len(ORACLE_SPINNER_FRAMES)]
        self.frame += 1
        detail = progress_line.removeprefix("Oracle ")
        print(
            f"\r\033[KOracle [{frame}] {detail}",
            file=self.stream,
            end="",
            flush=True,
        )
        self.active = True

    def prepare_for_external_output(self) -> None:
        if not self.active:
            return
        self._clear_line()
        self.active = False

    def finish(self) -> None:
        if not self.active:
            return
        self._clear_line()
        print("", file=self.stream, flush=True)
        self.active = False

    def _clear_line(self) -> None:
        print("\r\033[K", file=self.stream, end="", flush=True)


def job_result_from_spec(spec: JobSpec, returncode: int, elapsed_sec: float = 0.0) -> JobResult:
    return JobResult(
        agent=spec.agent,
        model=spec.model,
        label=spec.label,
        job_name=spec.job_name,
        n_trials_expected=spec.num_tasks * spec.repeats,
        returncode=returncode,
        elapsed_sec=round(elapsed_sec, 3),
        job_dir=str(spec.job_dir),
        runner_log=str(spec.runner_log),
        resumed=spec.resume,
    )


def terminate_running_jobs() -> None:
    with PROCESS_LOCK:
        running = list(RUNNING_PROCESSES.items())
    for job_name, process in running:
        if process.poll() is None:
            print(f"interrupt: terminating {job_name}", flush=True)
            process.terminate()
    deadline = time.time() + 15
    for job_name, process in running:
        if process.poll() is not None:
            continue
        timeout = max(0.0, deadline - time.time())
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"interrupt: killing {job_name}", flush=True)
            process.kill()


def stop_modal_app_via_cli(app_name: str) -> bool:
    modal = shutil.which("modal")
    if modal is None:
        return False
    try:
        completed = subprocess.run(
            [modal, "app", "stop", "--yes", app_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"modal shutdown: CLI failed for {app_name}: {exc!r}", flush=True)
        return False
    if completed.returncode == 0:
        return True
    detail = completed.stderr.strip() or completed.stdout.strip()
    print(f"modal shutdown: CLI could not stop {app_name}: {detail}", flush=True)
    return False


def stop_modal_app_via_sdk(app_name: str) -> bool:
    """Use the SDK when the Modal CLI is unavailable in the authoring image."""
    try:
        from modal.experimental import stop_app

        stop_app(app_name)
        return True
    except Exception as exc:
        print(f"modal shutdown: SDK could not stop {app_name}: {exc!r}", flush=True)
        return False


def shutdown_modal_app(enabled: bool, app_name: str | None) -> None:
    """Stop only this run's Modal App and therefore all of its containers."""
    global SHUTDOWN_MODAL_COMPLETED
    if SHUTDOWN_MODAL_COMPLETED:
        return
    if not enabled:
        return
    if not app_name:
        # Never fall back to the shared Harbor default. That would be unsafe in
        # a multi-user Modal workspace.
        print("modal shutdown: no owned app name is available; nothing was stopped", flush=True)
        SHUTDOWN_MODAL_COMPLETED = True
        return

    print(f"modal shutdown: stopping owned app and containers {app_name}", flush=True)
    if stop_modal_app_via_cli(app_name) or stop_modal_app_via_sdk(app_name):
        SHUTDOWN_MODAL_COMPLETED = True


def cleanup_modal_for_args(args: argparse.Namespace) -> None:
    shutdown_modal_app(
        should_shutdown_modal(args),
        getattr(args, "modal_app_name", None),
    )


def should_shutdown_modal(args: argparse.Namespace) -> bool:
    return bool(
        not getattr(args, "quick", False)
        and args.shutdown_modal
        and args.env == "modal"
    )


def run_all(specs: list[JobSpec], local_concurrency: int) -> tuple[list[JobResult], bool]:
    results_by_job: dict[str, JobResult] = {}
    interrupted = False
    progress_reporter = ProgressReporter(specs)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=local_concurrency)
    future_to_spec = {
        pool.submit(run_job, spec, progress_reporter): spec for spec in specs
    }
    try:
        for index, future in enumerate(concurrent.futures.as_completed(future_to_spec), 1):
            spec = future_to_spec[future]
            try:
                result = future.result()
            except Exception as exc:
                result = job_result_from_spec(spec, returncode=2)
                spec.jobs_dir.mkdir(parents=True, exist_ok=True)
                spec.runner_log.write_text(
                    "runner exception:\n"
                    f"{traceback.format_exc()}\n",
                    encoding="utf-8",
                )
                progress_reporter.report(
                    spec,
                    f"exception; details written to {spec.runner_log}",
                )
            results_by_job[spec.job_name] = result
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupt: stopping local Harbor jobs and archiving completed tasks", flush=True)
        terminate_running_jobs()
        concurrent.futures.wait(future_to_spec, timeout=20)
        for future, spec in future_to_spec.items():
            if spec.job_name in results_by_job:
                continue
            if future.done():
                try:
                    results_by_job[spec.job_name] = future.result()
                except Exception:
                    results_by_job[spec.job_name] = job_result_from_spec(spec, returncode=2)
            else:
                future.cancel()
                results_by_job[spec.job_name] = job_result_from_spec(spec, returncode=-130)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    results = [
        results_by_job.get(spec.job_name, job_result_from_spec(spec, returncode=-130))
        for spec in specs
    ]
    for index, (spec, result) in enumerate(zip(specs, results), 1):
        status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
        print(
            f"[{index}/{len(specs)}] {status}: {spec.label} "
            f"({spec.repeats} attempts for one task, "
            f"-n {spec.n_concurrent}) {result.elapsed_sec:.1f}s",
            flush=True,
        )
    return results, interrupted


def write_summary(
    jobs_dir: Path, task_root: Path, run_id: str, results: list[JobResult]
) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = jobs_dir / f"{run_id}.summary.json"
    payload = {
        "run_id": run_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "task_root": str(task_root),
        "results": [asdict(result) for result in results],
    }
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return summary_path


def task_path_from_trial_result(result: dict, result_path: Path) -> Path | None:
    task_id = result.get("task_id")
    if isinstance(task_id, dict) and task_id.get("path"):
        return Path(task_id["path"]).resolve()

    config = result.get("config")
    if isinstance(config, dict):
        task = config.get("task")
        if isinstance(task, dict) and task.get("path"):
            return Path(task["path"]).resolve()

    try:
        config = load_json(result_path.parent / "config.json")
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None
    task = config.get("task")
    if isinstance(task, dict) and task.get("path"):
        return Path(task["path"]).resolve()
    return None


def canonical_task_path(
    task_path: Path,
    requested_tasks: list[Path] | tuple[Path, ...] | None,
) -> Path:
    """Map Harbor snapshot paths back to the task path supplied to the runner.

    Harbor records the task path it actually executed. Normal runs use an
    immutable Oracle or agent snapshot, so that path intentionally differs
    from the author's task directory. This runner currently accepts one task;
    when that is the case, the supplied task is the unambiguous archive target.
    Name matching remains useful for callers that provide several task paths.
    """
    resolved = task_path.resolve()
    if not requested_tasks:
        return resolved

    candidates = tuple(task.resolve() for task in requested_tasks)
    if resolved in candidates:
        return resolved
    named = tuple(task for task in candidates if task.name == resolved.name)
    if len(named) == 1:
        return named[0]
    if len(candidates) == 1:
        return candidates[0]
    return resolved


def collect_trial_archives(
    results: list[JobResult],
    tasks: list[Path] | tuple[Path, ...] | None = None,
) -> dict[Path, list[TrialArchive]]:
    archives_by_task: dict[Path, list[TrialArchive]] = {}
    result_by_job = {result.job_name: result for result in results}
    for result in results:
        job_dir = Path(result.job_dir)
        if not job_dir.is_dir():
            continue
        for result_path in sorted(job_dir.glob("*/result.json")):
            try:
                trial_result = load_json(result_path)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SystemExit(f"error: failed to read trial result {result_path}: {exc}")

            trial_task_path = task_path_from_trial_result(trial_result, result_path)
            if trial_task_path is None:
                raise SystemExit(f"error: could not find task path for {result_path}")
            task_path = canonical_task_path(trial_task_path, tasks)

            archive = TrialArchive(
                job_name=result.job_name,
                agent=result_by_job[result.job_name].agent,
                label=result_by_job[result.job_name].label,
                model=result_by_job[result.job_name].model,
                job_dir=job_dir,
                runner_log=Path(result.runner_log),
                trial_dir=result_path.parent,
                result_path=result_path,
                task_path=task_path,
                finished=bool(trial_result.get("finished_at")),
                reward=trial_reward(trial_result),
                exception_type=exception_type(trial_result),
            )
            archives_by_task.setdefault(task_path, []).append(archive)
    return archives_by_task


def trial_reward(trial_result: dict) -> float | None:
    verifier_result = trial_result.get("verifier_result")
    if not isinstance(verifier_result, dict):
        return None
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, dict):
        return None
    reward = rewards.get("reward")
    if isinstance(reward, (int, float)):
        return float(reward)
    return None


def exception_type(trial_result: dict) -> str | None:
    exception_info = trial_result.get("exception_info")
    if not isinstance(exception_info, dict):
        return None
    value = exception_info.get("exception_type")
    return str(value) if value else "Exception"


def collect_oracle_trial_results(
    job_dir: Path,
    tasks: list[Path] | tuple[Path, ...] | None = None,
) -> dict[Path, OracleTrialResult]:
    results: dict[Path, OracleTrialResult] = {}
    for result_path in sorted(job_dir.glob("*/result.json")):
        try:
            trial_result = load_json(result_path)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"error: failed to read trial result {result_path}: {exc}")

        trial_task_path = task_path_from_trial_result(trial_result, result_path)
        if trial_task_path is None:
            raise SystemExit(f"error: could not find task path for {result_path}")
        task_path = canonical_task_path(trial_task_path, tasks)

        candidate = OracleTrialResult(
            task_path=task_path,
            finished=bool(trial_result.get("finished_at")),
            reward=trial_reward(trial_result),
            exception_type=exception_type(trial_result),
            result_path=result_path,
        )
        existing = results.get(task_path)
        if existing is None:
            results[task_path] = candidate
            continue

        existing_reward = existing.reward if existing.reward is not None else float("-inf")
        candidate_reward = candidate.reward if candidate.reward is not None else float("-inf")
        if (candidate.finished, candidate_reward) > (existing.finished, existing_reward):
            results[task_path] = candidate
    return results


def job_dir_has_oracle_agent(job_dir: Path) -> bool:
    try:
        config = load_json(job_dir / "config.json")
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return False
    agents = config.get("agents")
    if not isinstance(agents, list):
        return False
    return any(isinstance(agent, dict) and agent.get("name") == "oracle" for agent in agents)


def collect_latest_oracle_archives(
    jobs_dir: Path,
    tasks: list[Path] | tuple[Path, ...] | None = None,
) -> dict[Path, OracleArchive]:
    archives: dict[Path, OracleArchive] = {}
    if not jobs_dir.is_dir():
        return archives

    for job_dir in sorted(path for path in jobs_dir.iterdir() if path.is_dir()):
        if not job_dir_has_oracle_agent(job_dir):
            continue
        for result_path in sorted(job_dir.glob("*/result.json")):
            try:
                trial_result = load_json(result_path)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SystemExit(f"error: failed to read oracle result {result_path}: {exc}")

            trial_task_path = task_path_from_trial_result(trial_result, result_path)
            if trial_task_path is None:
                raise SystemExit(f"error: could not find task path for {result_path}")
            task_path = canonical_task_path(trial_task_path, tasks)

            candidate = OracleArchive(
                task_path=task_path,
                job_dir=job_dir,
                trial_dir=result_path.parent,
                result_path=result_path,
                finished=bool(trial_result.get("finished_at")),
                reward=trial_reward(trial_result),
                exception_type=exception_type(trial_result),
                mtime=result_path.stat().st_mtime,
            )
            existing = archives.get(task_path)
            if existing is None or candidate.mtime > existing.mtime:
                archives[task_path] = candidate
    return archives


def move_task_dir(source: Path, destination: Path, overwrite: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not overwrite:
            raise FileExistsError(f"destination already exists: {destination}")
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.move(str(source), str(destination))


def sort_finished_oracle_tasks(
    *,
    tasks: list[Path],
    job: OracleSortJobSpec,
    pass_dir: Path,
    fail_dir: Path,
    pass_threshold: float,
    overwrite: bool,
    moved_names: set[str],
) -> list[OracleSortMoveResult]:
    """Move every task whose oracle trial has *finished* into its pass/fail bucket.

    Designed to be called repeatedly while the Harbor job is still running (from
    run_oracle_sort_job's poll loop). Only tasks with a finished trial that have
    not already been moved are touched, so in-flight trials for other tasks are
    never disturbed. Task names moved here are recorded in ``moved_names`` so the
    final sweep in sort_oracle_tasks skips them.
    """
    trial_results = collect_oracle_trial_results(job.job_dir, tasks)
    trial_results_by_name = {path.name: result for path, result in trial_results.items()}
    moved: list[OracleSortMoveResult] = []
    for task in tasks:
        if task.name in moved_names:
            continue
        task_path = task.resolve()
        trial = trial_results.get(task_path) or trial_results_by_name.get(task.name)
        if trial is None or not trial.finished:
            continue
        reward = trial.reward
        passed = reward is not None and reward >= pass_threshold
        destination = (pass_dir if passed else fail_dir).resolve() / task.name
        status = "passed" if passed else "failed"
        try:
            move_task_dir(task_path, destination, overwrite)
        except Exception as exc:
            moved_names.add(task.name)
            moved.append(
                OracleSortMoveResult(
                    task=task.name,
                    status="move_failed",
                    reward=reward,
                    source=str(task_path),
                    destination=None,
                    result_path=str(trial.result_path),
                    error=repr(exc),
                )
            )
            continue
        moved_names.add(task.name)
        moved.append(
            OracleSortMoveResult(
                task=task.name,
                status=status,
                reward=reward,
                source=str(task_path),
                destination=str(destination),
                result_path=str(trial.result_path),
                error=None,
            )
        )
        print(
            f"incremental sort: {status}: {task.name} reward={reward} -> {destination}",
            flush=True,
        )
    return moved


def sort_oracle_tasks(
    *,
    tasks: list[Path],
    job: OracleSortJobSpec,
    pass_dir: Path,
    fail_dir: Path,
    pass_threshold: float,
    overwrite: bool,
    moved_names: set[str] | None = None,
) -> list[OracleSortMoveResult]:
    moved_names = moved_names if moved_names is not None else set()
    trial_results = collect_oracle_trial_results(job.job_dir, tasks)
    trial_results_by_name = {path.name: result for path, result in trial_results.items()}
    results: list[OracleSortMoveResult] = []
    for task in tasks:
        if task.name in moved_names:
            continue
        task_path = task.resolve()
        trial = trial_results.get(task_path) or trial_results_by_name.get(task.name)
        reward = trial.reward if trial is not None else None
        passed = (
            trial is not None
            and trial.finished
            and reward is not None
            and reward >= pass_threshold
        )
        destination_root = pass_dir if passed else fail_dir
        destination = destination_root.resolve() / task.name
        status = "passed" if passed else "failed"
        error = None
        if trial is None:
            error = "no trial result found for task"
        elif not trial.finished:
            error = f"trial did not finish: {trial.result_path}"

        try:
            move_task_dir(task_path, destination, overwrite)
        except Exception as exc:
            results.append(
                OracleSortMoveResult(
                    task=task.name,
                    status="move_failed",
                    reward=reward,
                    source=str(task_path),
                    destination=None,
                    result_path=str(trial.result_path) if trial else None,
                    error=repr(exc),
                )
            )
            continue

        results.append(
            OracleSortMoveResult(
                task=task.name,
                status=status,
                reward=reward,
                source=str(task_path),
                destination=str(destination),
                result_path=str(trial.result_path) if trial else None,
                error=error,
            )
        )
    return results


def write_oracle_sort_summary(
    jobs_dir: Path,
    run_id: str,
    task_root: Path,
    results: list[OracleSortMoveResult],
) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = jobs_dir / f"{run_id}.oracle-sort-summary.json"
    payload = {
        "run_id": run_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "task_root": str(task_root),
        "results": [asdict(result) for result in results],
    }
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return summary_path


def copy_file_if_exists(src: Path, dst: Path) -> None:
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def copy_run_job_metadata(archives: list[TrialArchive], run_archive_dir: Path) -> None:
    copied: set[str] = set()
    for archive in archives:
        if archive.job_name in copied:
            continue
        copied.add(archive.job_name)
        dst_job_dir = run_archive_dir / "jobs" / archive.job_name
        for filename in ("config.json", "result.json", "job.log", "lock.json"):
            copy_file_if_exists(archive.job_dir / filename, dst_job_dir / filename)
        copy_file_if_exists(
            archive.runner_log,
            run_archive_dir / "runner-logs" / f"{archive.job_name}.runner.log",
        )


def copy_latest_oracle_archive(
    *,
    task_path: Path,
    task_name: str,
    trajectories_dir: Path,
    latest_oracles: dict[Path, OracleArchive],
    latest_oracles_by_name: dict[str, OracleArchive],
) -> bool:
    oracle = latest_oracles.get(task_path) or latest_oracles_by_name.get(task_name)
    if oracle is None:
        return False

    oracle_dir = trajectories_dir / "oracle"
    dst_trial_dir = oracle_dir / oracle.trial_dir.name
    oracle_dir.mkdir(parents=True, exist_ok=True)
    if not dst_trial_dir.exists():
        shutil.copytree(oracle.trial_dir, dst_trial_dir, symlinks=True)
    manifest = {
        "job": oracle.job_dir.name,
        "trial": oracle.trial_dir.name,
        "task_path": str(oracle.task_path),
        "result_path": str(oracle.result_path),
        "finished": oracle.finished,
        "reward": oracle.reward,
        "exception_type": oracle.exception_type,
    }
    (oracle_dir / "oracle_archive.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return True


def copy_trajectory_summary(
    *,
    summary_path: Path,
    markdown_summary_path: Path | None,
    trajectories_dir: Path,
) -> None:
    trajectories_dir.mkdir(parents=True, exist_ok=True)
    if markdown_summary_path is not None:
        copy_file_if_exists(markdown_summary_path, trajectories_dir / "summary.md")
    else:
        copy_file_if_exists(summary_path, trajectories_dir / "summary.json")


def archive_task_files(task_path: Path, task_archive_dir: Path) -> None:
    task_archive_task_dir = task_archive_dir / "task"
    source_root = Path("completed_uploaded").resolve()
    source_task_path = task_path
    try:
        source_task_path.relative_to(source_root)
        source_is_completed_upload = True
    except ValueError:
        source_is_completed_upload = False

    source_root_candidate = source_root / task_path.name
    if not source_is_completed_upload and source_root_candidate.exists():
        source_task_path = source_root_candidate
        source_is_completed_upload = True

    if not source_task_path.exists():
        print(
            f"archive: source task missing for {task_path.name}; "
            f"trajectories are archived at {task_archive_dir}"
        )
    elif task_archive_task_dir.exists():
        print(
            f"archive: source task already archived for {task_path.name}; "
            f"leaving {source_task_path} in place"
        )
    elif not source_is_completed_upload:
        shutil.copytree(
            source_task_path,
            task_archive_task_dir,
            symlinks=True,
        )
        print(
            f"archive: copied task files for {task_path.name} -> "
            f"{task_archive_task_dir}"
        )
    else:
        shutil.move(str(source_task_path), str(task_archive_task_dir))


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def format_metric(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def build_metric_cell(archives: list[TrialArchive]) -> str:
    if not archives:
        return "-"
    finished = [archive for archive in archives if archive.finished]
    rewards = [archive.reward for archive in finished if archive.reward is not None]
    passes = sum(1 for reward in rewards if reward > 0)
    mean = sum(rewards) / len(rewards) if rewards else None
    errored = sum(1 for archive in finished if archive.exception_type is not None)
    cell = f"{passes}/{len(finished)} pass, mean {format_metric(mean)}"
    if errored:
        cell += f", {errored} errors"
    return cell


def build_oracle_metric_cell(
    oracle: OracleArchive | None,
    pass_threshold: float,
) -> str:
    if oracle is None:
        return "-"
    reward = format_metric(oracle.reward)
    if oracle.exception_type is not None:
        return f"exception, reward {reward}"
    if not oracle.finished:
        return f"incomplete, reward {reward}"
    verdict = (
        "pass"
        if oracle.reward is not None and oracle.reward >= pass_threshold
        else "fail"
    )
    return f"{verdict}, reward {reward}"


def write_markdown_summary(
    *,
    jobs_dir: Path,
    tasks: list[Path],
    results: list[JobResult],
    run_id: str,
    oracle_pass_threshold: float = 1.0,
) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = jobs_dir / f"{run_id}.summary.md"
    archives_by_task = collect_trial_archives(results, tasks)
    oracle_archives = collect_latest_oracle_archives(jobs_dir, tasks)
    archives_by_task_name: dict[str, list[TrialArchive]] = {}
    for task_path, archives in archives_by_task.items():
        archives_by_task_name.setdefault(task_path.name, []).extend(archives)
    oracle_archives_by_name: dict[str, OracleArchive] = {}
    for task_path, oracle in oracle_archives.items():
        oracle_archives_by_name.setdefault(task_path.name, oracle)
    task_set = sorted({task.resolve() for task in tasks})
    agent_names = [result.agent for result in results]

    lines = [
        f"# Harbor Run Summary: {run_id}",
        "",
        f"- Updated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- Tasks: {len(task_set)}",
        f"- Oracle results: {len(oracle_archives)}",
        f"- Agent jobs: {len(results)}",
        "",
        "| Task | Oracle | "
        + " | ".join(markdown_escape(name) for name in agent_names)
        + " | Status |",
        "| --- | --- | "
        + " | ".join("---" for _ in agent_names)
        + " | --- |",
    ]

    completed = 0
    for task in task_set:
        archives = archives_by_task.get(task) or archives_by_task_name.get(task.name, [])
        oracle = oracle_archives.get(task) or oracle_archives_by_name.get(task.name)
        cells = [build_oracle_metric_cell(oracle, oracle_pass_threshold)]
        complete = bool(
            oracle is not None
            and oracle.finished
            and oracle.exception_type is None
            and oracle.reward is not None
            and oracle.reward >= oracle_pass_threshold
        )
        for result in results:
            job_archives = [
                archive for archive in archives if archive.job_name == result.job_name
            ]
            expected = result.n_trials_expected // len(task_set) if task_set else 0
            if (
                result.returncode != 0
                or len(job_archives) < expected
                or any(
                    not archive.finished or archive.exception_type is not None
                    for archive in job_archives
                )
            ):
                complete = False
            cells.append(build_metric_cell(job_archives))
        if complete:
            completed += 1
        status = "complete" if complete else "partial"
        lines.append(
            "| "
            + markdown_escape(task.name)
            + " | "
            + " | ".join(markdown_escape(cell) for cell in cells)
            + " | "
            + status
            + " |"
        )

    lines.extend(
        [
            "",
            f"Completed tasks: {completed}/{len(task_set)}",
            "",
        ]
    )
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def archive_completed_task_runs(
    *,
    tasks: list[Path],
    results: list[JobResult],
    summary_path: Path,
    markdown_summary_path: Path | None = None,
    run_id: str,
    destination_root: Path,
    oracle_pass_threshold: float = 1.0,
) -> list[Path]:
    destination_root = destination_root.resolve()
    task_set = {task.resolve() for task in tasks}
    archives_by_task = collect_trial_archives(results, tasks)
    archives_by_task_name: dict[str, list[TrialArchive]] = {}
    for task_path, archives in archives_by_task.items():
        archives_by_task_name.setdefault(task_path.name, []).extend(archives)
    latest_oracles = collect_latest_oracle_archives(summary_path.parent, tasks)
    latest_oracles_by_name: dict[str, OracleArchive] = {}
    for oracle in latest_oracles.values():
        existing = latest_oracles_by_name.get(oracle.task_path.name)
        if existing is None or oracle.mtime > existing.mtime:
            latest_oracles_by_name[oracle.task_path.name] = oracle

    task_names = [task.name for task in task_set]
    if len(task_names) != len(set(task_names)):
        raise SystemExit(
            "error: cannot archive by folder name when task folder names are not unique"
        )

    failed_job_names = sorted(
        result.job_name for result in results if result.returncode != 0
    )
    expected_trials_by_job = {
        result.job_name: result.n_trials_expected // len(task_set)
        for result in results
        if len(task_set) > 0
    }
    task_states: list[TaskArchiveState] = []
    for task_path in sorted(task_set):
        archives = archives_by_task.get(task_path) or archives_by_task_name.get(
            task_path.name, []
        )
        job_names = {result.job_name for result in results}
        archived_job_names = {archive.job_name for archive in archives}
        missing_jobs = sorted(job_names - archived_job_names)
        unfinished = [archive for archive in archives if not archive.finished]
        exception_archives = [
            archive for archive in archives if archive.exception_type is not None
        ]
        short_jobs = []
        finished_counts_by_job: dict[str, int] = {}
        for job_name in sorted(job_names):
            expected = expected_trials_by_job.get(job_name, 0)
            actual = sum(
                1
                for archive in archives
                if archive.job_name == job_name and archive.finished
            )
            finished_counts_by_job[job_name] = actual
            if actual < expected:
                short_jobs.append(f"{job_name}: {actual}/{expected}")

        task_states.append(
            TaskArchiveState(
                task_path=task_path,
                archives=archives,
                oracle=latest_oracles.get(task_path)
                or latest_oracles_by_name.get(task_path.name),
                missing_jobs=missing_jobs,
                unfinished=unfinished,
                exception_archives=exception_archives,
                short_jobs=short_jobs,
                finished_counts_by_job=finished_counts_by_job,
            )
        )

    successful_archive = bool(task_states) and not failed_job_names and all(
        not state.missing_jobs
        and not state.unfinished
        and not state.exception_archives
        and not state.short_jobs
        and state.oracle is not None
        and state.oracle.finished
        and state.oracle.exception_type is None
        and state.oracle.reward is not None
        and state.oracle.reward >= oracle_pass_threshold
        for state in task_states
    )

    if successful_archive:
        clear_trajectories_dir(destination_root)
        destination_root.mkdir(parents=True, exist_ok=True)
        if markdown_summary_path is not None:
            copy_file_if_exists(markdown_summary_path, destination_root / "summary.md")
        else:
            copy_file_if_exists(summary_path, destination_root / "summary.json")

        oracle_copied = 0
        for state in task_states:
            for archive in state.archives:
                dst_trial_dir = (
                    destination_root / slug(archive.agent) / archive.trial_dir.name
                )
                if dst_trial_dir.exists():
                    continue
                shutil.copytree(
                    archive.trial_dir,
                    dst_trial_dir,
                    symlinks=True,
                )
            if copy_latest_oracle_archive(
                task_path=state.task_path,
                task_name=state.task_path.name,
                trajectories_dir=destination_root,
                latest_oracles=latest_oracles,
                latest_oracles_by_name=latest_oracles_by_name,
            ):
                oracle_copied += 1

        print(f"archive: wrote successful trajectories -> {destination_root}")
        if oracle_copied:
            print(f"archive: copied latest oracle trajectories for {oracle_copied} tasks")
        return [destination_root]

    # Preserve incomplete evidence under a run-specific directory. A partial
    # run must not clear a previous successful direct trajectory archive.
    moved: list[Path] = []
    run_archive_dir = destination_root / run_id
    run_archive_dir.mkdir(parents=True, exist_ok=True)
    if markdown_summary_path is not None:
        copy_file_if_exists(markdown_summary_path, run_archive_dir / "summary.md")
    else:
        copy_file_if_exists(summary_path, run_archive_dir / "summary.json")
    copy_run_job_metadata(
        [archive for archives in archives_by_task.values() for archive in archives],
        run_archive_dir,
    )

    incomplete: list[dict[str, object]] = []
    oracle_copied = 0
    for state in task_states:
        task_path = state.task_path
        task_archive_dir = run_archive_dir / task_path.name
        task_archive_existed = task_archive_dir.exists()
        trajectories_dir = task_archive_dir / "trajectories"
        copy_trajectory_summary(
            summary_path=summary_path,
            markdown_summary_path=markdown_summary_path,
            trajectories_dir=trajectories_dir,
        )
        if (
            state.missing_jobs
            or state.unfinished
            or state.short_jobs
            or failed_job_names
            or state.exception_archives
        ):
            finished_archives = [archive for archive in state.archives if archive.finished]
            incomplete.append(
                {
                    "task": task_path.name,
                    "missing_jobs": state.missing_jobs,
                    "failed_jobs": failed_job_names,
                    "unfinished_trials": [
                        str(archive.result_path) for archive in state.unfinished
                    ],
                    "exception_trials": [
                        str(archive.result_path) for archive in state.exception_archives
                    ],
                    "short_jobs": state.short_jobs,
                    "finished_counts_by_job": state.finished_counts_by_job,
                }
            )
            if finished_archives:
                if task_archive_existed:
                    print(
                        f"archive: resuming existing partial trajectory archive: "
                        f"{task_archive_dir}"
                    )
                trajectories_dir.mkdir(parents=True, exist_ok=True)
                for archive in finished_archives:
                    dst_trial_dir = (
                        trajectories_dir / slug(archive.agent) / archive.trial_dir.name
                    )
                    if dst_trial_dir.exists():
                        continue
                    shutil.copytree(
                        archive.trial_dir,
                        dst_trial_dir,
                        symlinks=True,
                    )
                if copy_latest_oracle_archive(
                    task_path=task_path,
                    task_name=task_path.name,
                    trajectories_dir=trajectories_dir,
                    latest_oracles=latest_oracles,
                    latest_oracles_by_name=latest_oracles_by_name,
                ):
                    oracle_copied += 1
                archive_task_files(task_path, task_archive_dir)
            print(
                f"archive: partial {task_path.name}; "
                f"missing_jobs={state.missing_jobs}, "
                f"failed_jobs={failed_job_names}, "
                f"exceptions={len(state.exception_archives)}, "
                f"unfinished={len(state.unfinished)}, short_jobs={state.short_jobs}"
            )
            continue

    if incomplete:
        incomplete_path = run_archive_dir / "incomplete_tasks.json"
        incomplete_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "expected_trials_per_model": {
                        result.label: result.n_trials_expected // len(task_set)
                        for result in results
                        if len(task_set) > 0
                    },
                    "tasks": incomplete,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"archive: incomplete task report -> {incomplete_path}")
    if oracle_copied:
        print(f"archive: copied latest oracle trajectories for {oracle_copied} tasks")

    return moved


def load_results_or_specs(summary_path: Path, specs: list[JobSpec]) -> list[JobResult]:
    if summary_path.is_file():
        payload = load_json(summary_path)
        raw_results = payload.get("results")
        if isinstance(raw_results, list):
            return [JobResult(**item) for item in raw_results]
    return [job_result_from_spec(spec, returncode=0) for spec in specs]


def evaluate_oracle_gate(
    tasks: list[Path],
    job: OracleSortJobSpec,
    pass_threshold: float,
) -> tuple[bool, list[dict[str, object]]]:
    """Evaluate one Oracle trial per task without trusting Harbor's exit code."""
    oracle_results = collect_oracle_trial_results(job.job_dir, tasks)
    by_name: dict[str, list[OracleTrialResult]] = {}
    for result in oracle_results.values():
        by_name.setdefault(result.task_path.name, []).append(result)

    details: list[dict[str, object]] = []
    for task in tasks:
        candidate = oracle_results.get(task.resolve())
        if candidate is None and len(tasks) == 1 and len(oracle_results) == 1:
            candidate = next(iter(oracle_results.values()))
        if candidate is None:
            matches = by_name.get(task.name, [])
            if len(matches) == 1:
                candidate = matches[0]

        reward = candidate.reward if candidate is not None else None
        finished = candidate.finished if candidate is not None else False
        exception = candidate.exception_type if candidate is not None else None
        passed = bool(
            finished
            and exception is None
            and reward is not None
            and reward >= pass_threshold
        )
        details.append(
            {
                "task": str(task.resolve()),
                "finished": finished,
                "reward": reward,
                "passed": passed,
                "result_path": str(candidate.result_path) if candidate else None,
                "exception_type": exception,
                "error": None if candidate else "no Oracle trial result found",
            }
        )
    return bool(details) and all(bool(item["passed"]) for item in details), details


def write_oracle_gate_summary(
    jobs_dir: Path,
    run_id: str,
    task_root: Path,
    oracle_job: OracleSortJobSpec,
    pass_threshold: float,
    passed: bool,
    details: list[dict[str, object]],
) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = jobs_dir / f"{run_id}.oracle-gate.json"
    payload = {
        "run_id": run_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "task_root": str(task_root),
        "job_name": oracle_job.job_name,
        "job_dir": str(oracle_job.job_dir),
        "pass_threshold": pass_threshold,
        "passed": passed,
        "tasks": details,
    }
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return summary_path


def main(argv: list[str]) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("task"),
        help="The single Harbor task directory (default: ./task).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=None,
        help=(
            "Attempts for this task, passed to harbor --n-attempts "
            f"(default: {REMOTE_DEFAULT_REPEATS} for remote, {DEFAULT_REPEATS} for local)."
        ),
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Run exactly one host-local trial with an installed Claude Code or "
            "Codex CLI, then verify it in local Docker; never contacts Workbench "
            "or Modal."
        ),
    )
    parser.add_argument(
        "--quick-agent",
        "--quick-runner",
        dest="quick_agent",
        choices=QUICK_AGENT_CHOICES,
        default="auto",
        help=(
            "Local CLI for --quick: auto, claude, claude-code, or codex "
            "(default: auto; Codex is preferred when both are installed)."
        ),
    )
    parser.add_argument(
        "--quick-timeout-sec",
        type=float,
        default=None,
        help=(
            "Host-agent timeout for --quick; by default use [agent].timeout_sec "
            "times --agent-timeout-multiplier."
        ),
    )
    parser.add_argument(
        "--quick-image-tag",
        help="Choose the local Docker image tag used by --quick.",
    )
    parser.add_argument(
        "--quick-keep-image",
        action="store_true",
        help="Keep the local Docker image built for --quick.",
    )
    parser.add_argument(
        "--quick-keep-container",
        action="store_true",
        help="Keep the local --quick verifier container for debugging.",
    )
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="AGENT:MODEL[:LABEL[:N_CONCURRENT]]",
        help="Override default agent presets. Repeat for multiple agents.",
    )
    parser.add_argument(
        "--env",
        default="modal",
        help="Harbor execution backend (must be modal; default: modal).",
    )
    parser.add_argument(
        "--remote",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Submit this task to Workbench (default); use --no-remote for local "
            "Modal jobs."
        ),
    )
    parser.add_argument(
        "--service-url",
        default=os.environ.get("WORKBENCH_HARBOR_SERVICE_URL"),
        help="Workbench Harbor API base URL (the /v1 suffix is added when omitted).",
    )
    parser.add_argument(
        "--workbench-token",
        default=os.environ.get("WORKBENCH_RUNNER_TOKEN"),
        help="Workbench Firebase ID token or scoped runner token; may also come from WORKBENCH_RUNNER_TOKEN.",
    )
    parser.add_argument(
        "--cancel-on-interrupt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Request remote cancellation on Ctrl-C (default); use "
            "--no-cancel-on-interrupt to leave the server run running."
        ),
    )
    parser.add_argument(
        "--remote-poll-min",
        type=float,
        default=1.0,
        help="Minimum remote status poll delay in seconds (default: 1).",
    )
    parser.add_argument(
        "--remote-poll-max",
        type=float,
        default=30.0,
        help="Maximum remote status poll delay in seconds (default: 30).",
    )
    parser.add_argument(
        "--remote-progress-interval-sec",
        type=float,
        default=REMOTE_DEFAULT_PROGRESS_INTERVAL_SECONDS,
        help=(
            "Refresh the live remote progress table at least this often while "
            "the service has not changed state (default: 30)."
        ),
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path("harbor-jobs"),
        help="Directory for Harbor job output and runner logs (default: ./harbor-jobs).",
    )
    parser.add_argument(
        "--run-id",
        default=default_run_id(),
        help=(
            "Identifier shared by this run's per-agent jobs. The default includes "
            "a random suffix; reuse the printed ID with --resume."
        ),
    )
    parser.add_argument(
        "--n-concurrent",
        type=int,
        default=None,
        help="Override Harbor concurrency for the Oracle gate and all agent jobs.",
    )
    parser.add_argument(
        "--default-concurrency",
        type=int,
        default=None,
        help=(
            "Concurrency for --run agents that do not specify one "
            f"(default: {REMOTE_DEFAULT_CONCURRENCY} for remote, "
            f"{DEFAULT_CONCURRENCY} for local)."
        ),
    )
    parser.add_argument(
        "--local-concurrency",
        type=int,
        default=None,
        help="Number of harbor jobs to run at once locally (default: one per agent).",
    )
    parser.add_argument(
        "--completion-grace-sec",
        type=float,
        default=300.0,
        help=(
            "After all expected per-trial result.json files have finished_at, "
            "wait this many seconds for Harbor to exit before terminating the "
            "local Harbor process and treating the job as successful "
            "(default: 300)."
        ),
    )
    parser.add_argument(
        "--progress-interval-sec",
        type=float,
        default=LOCAL_DEFAULT_PROGRESS_INTERVAL_SECONDS,
        help=(
            "While local Oracle, agent, or --quick agent jobs are running, "
            "print non-interactive progress at this interval; interactive "
            "--quick status refreshes at 8 FPS; set <= 0 to disable periodic "
            "local progress (default: 30)."
        ),
    )
    parser.add_argument(
        "--agent-timeout-multiplier",
        type=float,
        default=2.0,
        help="Passed to harbor --agent-timeout-multiplier (default: 2.0; tasks run long).",
    )
    parser.add_argument(
        "--timeout-multiplier",
        type=float,
        default=1.0,
        help="Passed to harbor --timeout-multiplier (default: 1.0).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume existing jobs for --run-id via `harbor jobs resume` instead "
            "of clearing the jobs directory and starting new ones."
        ),
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        type=Path,
        help="Pass an env file through to harbor. May be repeated.",
    )
    parser.add_argument(
        "--agent-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass KEY=VALUE to harbor --agent-env. May be repeated.",
    )
    parser.add_argument(
        "--verifier-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass KEY=VALUE to harbor --verifier-env. May be repeated.",
    )
    parser.add_argument(
        "--environment-kwarg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass KEY=VALUE to harbor --environment-kwarg. May be repeated.",
    )
    parser.add_argument(
        "--modal-secret",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Mount a named Modal Secret into each sandbox. Repeat for multiple "
            "secrets; values stay in Modal and are never written to task files."
        ),
    )
    parser.add_argument(
        "--agent-kwarg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass KEY=VALUE to harbor --agent-kwarg. May be repeated.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Pass an artifact path to harbor --artifact. May be repeated.",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Pass --force-build to harbor.",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="Pass --no-delete to harbor so remote environments are retained (this is costly).",
    )
    parser.add_argument(
        "--shutdown-modal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Stop this run's owned Modal App after archive/finalization, including "
            "Ctrl-C and SIGTERM cleanup (default: enabled for --env modal)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the harbor commands without running them.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help=(
            "Build the task environment image and run solution/verification locally "
            "in a Docker container that follows the task's network_mode; do not "
            "start Harbor or Modal jobs."
        ),
    )
    parser.add_argument(
        "--smoke-no-cache",
        action="store_true",
        help="Build the local smoke-test image without Docker's build cache.",
    )
    parser.add_argument(
        "--smoke-keep-container",
        action="store_true",
        help="Retain the local smoke-test container for debugging.",
    )
    parser.add_argument(
        "--smoke-keep-image",
        action="store_true",
        help="Retain the locally built smoke-test image for debugging.",
    )
    parser.add_argument(
        "--smoke-image-tag",
        help="Override the local smoke-test image tag.",
    )
    parser.add_argument(
        "--smoke-logs-dir",
        type=Path,
        help="Where to write local smoke-test logs (default: task/.runner-logs/<run>).",
    )
    parser.add_argument(
        "--smoke-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set an environment variable for the local smoke test; may be repeated.",
    )
    parser.add_argument(
        "--smoke-env-file",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="Read local smoke-test environment variables from a KEY=VALUE file; may be repeated.",
    )
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help=(
            "Do not start Harbor jobs. Scan existing job directories for --run-id, "
            "archive completed task runs, and move completed tasks without clearing them."
        ),
    )
    parser.add_argument(
        "--oracle-sort",
        action="store_true",
        help=(
            "Run the single task with the Harbor oracle on Modal, then move the "
            "task to --oracle-pass-dir or --oracle-fail-dir. Uses one attempt."
        ),
    )
    parser.add_argument(
        "--oracle-concurrency",
        type=int,
        default=1,
        help=(
            "Default Harbor --n-concurrent for the Oracle gate when "
            "--n-concurrent is omitted (default: 1)."
        ),
    )
    parser.add_argument(
        "--oracle-pass-dir",
        type=Path,
        default=Path("ready_to_upload"),
        help="Destination for oracle-passing tasks in --oracle-sort mode.",
    )
    parser.add_argument(
        "--oracle-fail-dir",
        type=Path,
        default=Path("failed_oracle"),
        help="Destination for oracle-failing tasks in --oracle-sort mode.",
    )
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=1.0,
        help="Minimum Oracle reward required before agent jobs may start (default: 1.0).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow --oracle-sort to replace existing task directories at destinations.",
    )
    parser.add_argument(
        "--archive-completed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Archive trajectory output; successful runs replace the direct folders "
            "and partial runs stay under a run ID (default: enabled)."
        ),
    )
    parser.add_argument(
        "--completed-trajectories-dir",
        type=Path,
        default=Path("trajectories"),
        help=(
            "Directory for the current oracle/agent trajectory folders and summary "
            "(default: ./trajectories)."
        ),
    )
    parser.add_argument(
        "--task-snapshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Create separate immutable Oracle and agent snapshots before invoking "
            'Harbor; both snapshots use network_mode = "public" '
            "(default: enabled)."
        ),
    )
    args = parser.parse_args(argv)
    if args.repeats is None:
        args.repeats = REMOTE_DEFAULT_REPEATS if args.remote else DEFAULT_REPEATS
    if args.default_concurrency is None:
        args.default_concurrency = (
            REMOTE_DEFAULT_CONCURRENCY if args.remote else DEFAULT_CONCURRENCY
        )
    global MODAL_APP_NAME, MODAL_CLEANUP_ARMED, SHUTDOWN_MODAL_COMPLETED, SHUTDOWN_MODAL_ON_INTERRUPT
    MODAL_APP_NAME = None
    MODAL_CLEANUP_ARMED = False
    SHUTDOWN_MODAL_COMPLETED = False
    SHUTDOWN_MODAL_ON_INTERRUPT = should_shutdown_modal(args)

    validate_run_id(args.run_id)
    if args.repeats < 1:
        raise SystemExit("error: --repeats must be >= 1")
    if args.n_concurrent is not None and args.n_concurrent < 1:
        raise SystemExit("error: --n-concurrent must be >= 1")
    if args.oracle_concurrency < 1:
        raise SystemExit("error: --oracle-concurrency must be >= 1")
    if args.default_concurrency < 1:
        raise SystemExit("error: --default-concurrency must be >= 1")
    if not args.remote and any(not name.strip() for name in args.modal_secret):
        raise SystemExit("error: --modal-secret names must not be empty")

    if not args.remote:
        for env_file in args.env_file:
            if not env_file.is_file():
                raise SystemExit(f"error: --env-file does not exist: {env_file}")
        for env_file in args.smoke_env_file:
            if not env_file.is_file():
                raise SystemExit(f"error: --smoke-env-file does not exist: {env_file}")
        for item in args.agent_env:
            parse_key_value(item, "--agent-env")
        for item in args.verifier_env:
            parse_key_value(item, "--verifier-env")
        for item in args.environment_kwarg:
            parse_key_value(item, "--environment-kwarg")
        for item in args.agent_kwarg:
            parse_key_value(item, "--agent-kwarg")

    task_root = resolve_single_task(args.path)

    if args.quick:
        # Quick mode is intentionally handled before the Modal preflight,
        # Harbor executable check, snapshots, and run manifest ownership. Its
        # contract is a host-installed CLI plus a local Docker verifier.
        return run_quick_trial(task_root, args)

    if args.remote:
        if args.smoke_test:
            print("remote input error: --remote cannot be combined with --smoke-test", file=sys.stderr)
            return 2
        return run_remote(task_root, args)

    tasks = [task_root]
    validate_modal_task_policy(tasks, args.env)

    if args.smoke_test:
        if args.resume or args.archive_only or args.oracle_sort or args.dry_run:
            raise SystemExit(
                "error: --smoke-test cannot be combined with --resume, "
                "--archive-only, --oracle-sort, or --dry-run"
            )
        return run_local_smoke(task_root, args)

    if not args.task_snapshot and not args.dry_run and not args.resume and not args.archive_only:
        raise SystemExit(
            "error: --no-task-snapshot is not supported for Harbor execution; "
            "the Oracle and agent jobs require separate immutable snapshots"
        )

    require_executable("harbor")

    num_tasks = 1
    jobs_dir = args.jobs_dir.resolve()
    if not args.resume and not args.archive_only and not args.dry_run:
        clear_harbor_jobs_dir(jobs_dir)
    manifest_path, modal_app_name = resolve_modal_run_identity(
        jobs_dir,
        args.run_id,
        resume=args.resume,
        archive_only=args.archive_only,
        dry_run=args.dry_run,
    )
    args.modal_run_manifest = manifest_path
    args.modal_app_name = modal_app_name
    MODAL_APP_NAME = modal_app_name
    merge_modal_secret_kwargs(args)
    merge_modal_run_kwargs(args)
    oracle_task_root = task_root
    agent_task_root = task_root
    oracle_snapshot_root: Path | None = None
    agent_snapshot_root: Path | None = None

    should_snapshot = (
        args.task_snapshot
        and not args.resume
        and not args.archive_only
    )
    if should_snapshot:
        jobs_dir.mkdir(parents=True, exist_ok=True)
        oracle_snapshot_root = jobs_dir / f"{args.run_id}.oracle-task-snapshot"
        oracle_task_root = oracle_snapshot_root
        if not args.oracle_sort:
            agent_snapshot_root = jobs_dir / f"{args.run_id}.agent-task-snapshot"
            agent_task_root = agent_snapshot_root
        if not args.dry_run:
            oracle_task_root = snapshot_task_root(
                task_root,
                jobs_dir,
                args.run_id,
                snapshot_label="oracle-task-snapshot",
                network_mode=DEFAULT_NETWORK_MODE,
            )
            oracle_snapshot_root = oracle_task_root
            validate_modal_task_policy(
                [oracle_task_root],
                args.env,
                expected_network_mode=DEFAULT_NETWORK_MODE,
            )
            if not args.oracle_sort:
                agent_task_root = snapshot_task_root(
                    oracle_task_root,
                    jobs_dir,
                    args.run_id,
                    snapshot_label="agent-task-snapshot",
                    network_mode=DEFAULT_NETWORK_MODE,
                )
                agent_snapshot_root = agent_task_root
                validate_modal_task_policy(
                    [agent_task_root],
                    args.env,
                    expected_network_mode=DEFAULT_NETWORK_MODE,
                )

    if args.oracle_sort:
        job = build_oracle_sort_job_spec(oracle_task_root, num_tasks, args)
        action = "archive-only" if args.archive_only else ("resume" if args.resume else "run")
        oracle_overview = [
            f"run-id:      {args.run_id}",
            f"action:      oracle-sort {action}",
            f"backend:     {args.env}",
            f"modal app:   {args.modal_app_name}",
            f"run manifest:{args.modal_run_manifest}",
            f"task root:   {task_root}",
        ]
        if oracle_snapshot_root is not None:
            oracle_overview.append(
                f"Oracle snapshot: {oracle_snapshot_root} "
                f"(network_mode={DEFAULT_NETWORK_MODE})"
            )
        oracle_overview.extend(
            [
                f"tasks:       {num_tasks}",
                f"concurrency: {args.n_concurrent or args.oracle_concurrency}",
                f"threshold:   reward >= {args.pass_threshold}",
                f"pass dir:    {args.oracle_pass_dir.resolve()}",
                f"fail dir:    {args.oracle_fail_dir.resolve()}",
                f"job:         {job.job_dir}",
                f"log:         {job.runner_log}",
            ]
        )
        print_runner_panel("Oracle sort", oracle_overview)
        print_runner_panel("Command", [redacted_command(job.command)], border_style="dim")
        if args.dry_run:
            return 0

        MODAL_CLEANUP_ARMED = True
        jobs_dir.mkdir(parents=True, exist_ok=True)
        args.oracle_pass_dir.mkdir(parents=True, exist_ok=True)
        args.oracle_fail_dir.mkdir(parents=True, exist_ok=True)

        moved_names: set[str] = set()
        incremental_results: list[OracleSortMoveResult] = []

        if args.archive_only:
            if not job.job_dir.is_dir():
                raise SystemExit(f"error: oracle job directory does not exist: {job.job_dir}")
            returncode = 0
        else:
            def incremental_sort() -> None:
                incremental_results.extend(
                    sort_finished_oracle_tasks(
                        tasks=tasks,
                        job=job,
                        pass_dir=args.oracle_pass_dir,
                        fail_dir=args.oracle_fail_dir,
                        pass_threshold=args.pass_threshold,
                        overwrite=args.overwrite,
                        moved_names=moved_names,
                    )
                )

            returncode = run_oracle_sort_job(job, on_poll=incremental_sort)
            if returncode != 0:
                cleanup_modal_for_args(args)
                raise SystemExit(
                    f"error: oracle Harbor job exited with {returncode}; "
                    f"see runner log: {job.runner_log}"
                )

        final_results = sort_oracle_tasks(
            tasks=tasks,
            job=job,
            pass_dir=args.oracle_pass_dir,
            fail_dir=args.oracle_fail_dir,
            pass_threshold=args.pass_threshold,
            overwrite=args.overwrite,
            moved_names=moved_names,
        )
        move_results = incremental_results + final_results
        summary_path = write_oracle_sort_summary(
            args.jobs_dir.resolve(), args.run_id, task_root, move_results
        )
        counts = {
            status: sum(1 for result in move_results if result.status == status)
            for status in sorted({result.status for result in move_results})
        }
        for index, result in enumerate(move_results, 1):
            print(
                f"[{index}/{len(move_results)}] {result.status}: "
                f"{result.task} reward={result.reward} -> {result.destination}"
            )
        print()
        print(f"summary: {summary_path}")
        print(f"counts:  {counts}")
        cleanup_modal_for_args(args)
        return 0 if all(result.status in {"passed", "failed"} for result in move_results) else 1

    agent_specs = (
        [parse_agent_spec(item, args.default_concurrency) for item in args.run]
        if args.run
        else [AgentSpec(*item) for item in DEFAULT_RUNS]
    )

    local_concurrency = args.local_concurrency or len(agent_specs)
    if local_concurrency < 1:
        raise SystemExit("error: --local-concurrency must be >= 1")
    if args.completion_grace_sec < 0:
        raise SystemExit("error: --completion-grace-sec must be >= 0")

    specs = build_job_specs(agent_task_root, num_tasks, agent_specs, args)
    oracle_job = build_oracle_sort_job_spec(oracle_task_root, num_tasks, args)

    action = "archive-only" if args.archive_only else ("resume" if args.resume else "run")
    run_overview = [
        f"run-id:    {args.run_id}",
        f"action:    {action}",
        f"backend:   {args.env}",
        f"modal app: {args.modal_app_name}",
        f"manifest:  {args.modal_run_manifest}",
        f"task root: {task_root}",
    ]
    if oracle_snapshot_root is not None:
        run_overview.append(
            f"Oracle snapshot: {oracle_snapshot_root} (network_mode={DEFAULT_NETWORK_MODE})"
        )
    if agent_snapshot_root is not None:
        run_overview.append(
            f"agent snapshot:  {agent_snapshot_root} (network_mode={DEFAULT_NETWORK_MODE})"
        )
    run_overview.extend(
        [
            f"tasks:     {num_tasks}",
            f"attempts:  {args.repeats}",
            (
                f"agents:    {len(agent_specs)}  "
                f"(jobs: {len(specs)}, local concurrency: {local_concurrency})"
            ),
            f"oracle:    {oracle_job.job_dir}",
        ]
    )
    for spec in specs:
        run_overview.append(
            f"  - {spec.label:24s} -n {spec.n_concurrent:<4d} "
            f"-> {spec.num_tasks * spec.repeats} trials  [{spec.job_dir}]"
        )
    print_runner_panel("Harbor run", run_overview)
    if args.dry_run:
        commands = [
            "# Oracle gate",
            redacted_command(oracle_job.command),
            "# Agent jobs (run only after Oracle passes)",
        ]
        commands.extend(redacted_command(spec.command) for spec in specs)
        print_runner_panel("Dry run commands", commands, border_style="dim")
        return 0

    MODAL_CLEANUP_ARMED = True
    if args.archive_only:
        summary_path = args.jobs_dir.resolve() / f"{args.run_id}.summary.json"
        results = load_results_or_specs(summary_path, specs)
        if not summary_path.is_file():
            summary_path = write_summary(
                args.jobs_dir.resolve(), task_root, args.run_id, results
            )
        markdown_summary_path = write_markdown_summary(
            jobs_dir=args.jobs_dir.resolve(),
            tasks=tasks,
            results=results,
            run_id=args.run_id,
            oracle_pass_threshold=args.pass_threshold,
        )
        print(f"markdown:  {markdown_summary_path}")
        trajectory_archive_path: Path | None = None
        if args.archive_completed:
            try:
                moved = archive_completed_task_runs(
                    tasks=tasks,
                    results=results,
                    summary_path=summary_path,
                    markdown_summary_path=markdown_summary_path,
                    run_id=args.run_id,
                    destination_root=args.completed_trajectories_dir,
                    oracle_pass_threshold=args.pass_threshold,
                )
                trajectory_archive_path = (
                    moved[0]
                    if moved
                    else args.completed_trajectories_dir.resolve() / args.run_id
                )
                print(f"trajectory archive: {trajectory_archive_path}")
                print(f"archived trajectory directories: {len(moved)}")
            finally:
                cleanup_modal_for_args(args)
        else:
            cleanup_modal_for_args(args)
        summary_document = (
            trajectory_archive_path / "summary.md"
            if trajectory_archive_path is not None
            and (trajectory_archive_path / "summary.md").is_file()
            else markdown_summary_path
        )
        print(f"run result: summary document: {summary_document}")
        return 0

    print()
    print("Oracle gate: starting one Oracle job before the three agent jobs")
    oracle_returncode = run_oracle_sort_job(oracle_job)
    oracle_evaluated, oracle_details = evaluate_oracle_gate(
        tasks=tasks,
        job=oracle_job,
        pass_threshold=args.pass_threshold,
    )
    oracle_passed = oracle_returncode == 0 and oracle_evaluated
    oracle_summary_path = write_oracle_gate_summary(
        jobs_dir=args.jobs_dir.resolve(),
        run_id=args.run_id,
        task_root=task_root,
        oracle_job=oracle_job,
        pass_threshold=args.pass_threshold,
        passed=oracle_passed,
        details=oracle_details,
    )
    for detail in oracle_details:
        print(
            f"  Oracle: {Path(str(detail['task'])).name} "
            f"reward={detail['reward']} "
            f"finished={detail['finished']} passed={detail['passed']} "
            f"exception={detail['exception_type']}"
        )
    print(f"Oracle gate summary: {oracle_summary_path}")
    if not oracle_passed:
        if oracle_returncode != 0:
            print(
                f"Oracle gate FAILED: Harbor exited with {oracle_returncode}; "
                f"see {oracle_job.runner_log}"
            )
        else:
            print(
                "Oracle gate FAILED: every task must finish with "
                f"reward >= {args.pass_threshold}; agent jobs were not started"
            )
        if oracle_returncode != 0:
            print(f"run result: exception details: {oracle_job.runner_log}")
        else:
            print(f"run result: Oracle summary: {oracle_summary_path}")
        cleanup_modal_for_args(args)
        return 1

    print("Oracle gate PASSED: starting the three agent jobs")
    results, interrupted = run_all(specs, local_concurrency)
    summary_path = write_summary(
        args.jobs_dir.resolve(), task_root, args.run_id, results
    )
    markdown_summary_path = write_markdown_summary(
        jobs_dir=args.jobs_dir.resolve(),
        tasks=tasks,
        results=results,
        run_id=args.run_id,
        oracle_pass_threshold=args.pass_threshold,
    )

    failed = [r for r in results if r.returncode != 0]
    print()
    print(f"completed: {len(results) - len(failed)} ok, {len(failed)} failed")
    print(f"summary:   {summary_path}")
    print(f"markdown:  {markdown_summary_path}")
    if failed:
        print(f"failed jobs (resume with: --run-id {args.run_id} --resume):")
        for r in failed:
            print(f"  - {r.label}: exit {r.returncode}  {r.runner_log}")
    trajectory_archive_path: Path | None = None
    if args.archive_completed:
        try:
            moved = archive_completed_task_runs(
                tasks=tasks,
                results=results,
                summary_path=summary_path,
                markdown_summary_path=markdown_summary_path,
                run_id=args.run_id,
                destination_root=args.completed_trajectories_dir,
                oracle_pass_threshold=args.pass_threshold,
            )
            trajectory_archive_path = (
                moved[0]
                if moved
                else args.completed_trajectories_dir.resolve() / args.run_id
            )
            print(f"trajectory archive: {trajectory_archive_path}")
            print(f"archived trajectory directories: {len(moved)}")
        finally:
            cleanup_modal_for_args(args)
    else:
        cleanup_modal_for_args(args)
    if failed:
        print(f"run result: failure/exception details: {failed[0].runner_log}")
    else:
        summary_document = (
            trajectory_archive_path / "summary.md"
            if trajectory_archive_path is not None
            and (trajectory_archive_path / "summary.md").is_file()
            else markdown_summary_path
        )
        print(f"run result: summary document: {summary_document}")
    if interrupted:
        return 130
    if failed:
        return 1
    return 0


def handle_sigterm(_signum: int, _frame: object) -> None:
    """Route container/process termination through the cleanup finally block."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    exit_code = 1
    try:
        exit_code = main(sys.argv[1:])
    except KeyboardInterrupt:
        print("\ninterrupt: stopping local Harbor jobs", flush=True)
        exit_code = 130
    finally:
        terminate_running_jobs()
        shutdown_modal_app(
            SHUTDOWN_MODAL_ON_INTERRUPT and MODAL_CLEANUP_ARMED,
            MODAL_APP_NAME,
        )
    sys.exit(exit_code)
