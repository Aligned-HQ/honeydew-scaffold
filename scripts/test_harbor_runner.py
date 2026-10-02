#!/usr/bin/env python3
"""Dependency-free regression checks for the vendored Harbor runner."""
from __future__ import annotations

import re
import sys
import io
import contextlib
import json
import os
import tarfile
import tempfile
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harbor_runner


def check_run_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-runner-test-") as raw:
        jobs_dir = Path(raw) / "jobs"
        manifest, app_name = harbor_runner.resolve_modal_run_identity(
            jobs_dir,
            "same-run",
            resume=False,
            archive_only=False,
            dry_run=False,
        )
        assert manifest.is_file()
        assert app_name.startswith("honeydew-same-run-")
        assert len(app_name) <= 64

        try:
            harbor_runner.resolve_modal_run_identity(
                jobs_dir,
                "same-run",
                resume=False,
                archive_only=False,
                dry_run=False,
            )
        except SystemExit as exc:
            assert "already claimed" in str(exc)
        else:
            raise AssertionError("a second live process reused the run ID")

        _, resumed_app_name = harbor_runner.resolve_modal_run_identity(
            jobs_dir,
            "same-run",
            resume=True,
            archive_only=False,
            dry_run=False,
        )
        assert resumed_app_name == app_name


def check_names_are_valid_and_unique() -> None:
    run_ids = [harbor_runner.default_run_id() for _ in range(100)]
    assert len(set(run_ids)) == len(run_ids)
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) for value in run_ids)
    app_names = [harbor_runner.make_modal_app_name("same-run", value) for value in run_ids]
    assert len(set(app_names)) == len(app_names)
    assert all(len(value) <= 64 for value in app_names)


def check_cleanup_is_targeted() -> None:
    cli_calls: list[str] = []
    sdk_calls: list[str] = []
    original_stop = harbor_runner.stop_modal_app_via_cli
    original_sdk_stop = harbor_runner.stop_modal_app_via_sdk
    original_completed = harbor_runner.SHUTDOWN_MODAL_COMPLETED
    harbor_runner.stop_modal_app_via_cli = lambda app_name: cli_calls.append(app_name) or False
    harbor_runner.stop_modal_app_via_sdk = lambda app_name: sdk_calls.append(app_name) or True
    harbor_runner.SHUTDOWN_MODAL_COMPLETED = False
    try:
        harbor_runner.shutdown_modal_app(True, "honeydew-owned-run")
    finally:
        harbor_runner.stop_modal_app_via_cli = original_stop
        harbor_runner.stop_modal_app_via_sdk = original_sdk_stop
        harbor_runner.SHUTDOWN_MODAL_COMPLETED = original_completed
    assert cli_calls == ["honeydew-owned-run"]
    assert sdk_calls == ["honeydew-owned-run"]


def check_jobs_dir_is_cleared_for_new_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-jobs-cleanup-test-") as raw:
        jobs_dir = Path(raw) / "harbor-jobs"
        jobs_dir.mkdir()
        (jobs_dir / "old-run.log").write_text("old", encoding="utf-8")
        nested = jobs_dir / "old-job"
        nested.mkdir()
        (nested / "result.json").write_text("{}", encoding="utf-8")
        symlink = jobs_dir / "old-link"
        symlink.symlink_to(jobs_dir / "old-run.log")

        harbor_runner.clear_harbor_jobs_dir(jobs_dir)

        assert list(jobs_dir.iterdir()) == []


def check_snapshot_results_map_to_original_task() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-archive-mapping-test-") as raw:
        root = Path(raw)
        original_task = root / "task"
        original_task.mkdir()
        jobs_dir = root / "harbor-jobs"
        job_dir = jobs_dir / "run-claude-opus"
        trial_dir = job_dir / "run.agent__trial"
        trial_dir.mkdir(parents=True)
        snapshot = jobs_dir / "run.agent-task-snapshot"
        result_path = trial_dir / "result.json"
        result_path.write_text(
            json.dumps(
                {
                    "task_id": {"path": str(snapshot)},
                    "finished_at": "2026-07-22T21:19:06Z",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                }
            ),
            encoding="utf-8",
        )
        result = harbor_runner.JobResult(
            agent="claude-code",
            model="anthropic/claude-opus-5",
            label="claude-opus",
            job_name="run-claude-opus",
            n_trials_expected=1,
            returncode=0,
            elapsed_sec=1.0,
            job_dir=str(job_dir),
            runner_log=str(jobs_dir / "run-claude-opus.runner.log"),
            resumed=False,
        )

        archives = harbor_runner.collect_trial_archives([result], [original_task])

        assert list(archives) == [original_task.resolve()]
        assert len(archives[original_task.resolve()]) == 1
        assert archives[original_task.resolve()][0].agent == "claude-code"


def check_successful_archive_uses_agent_names_and_oracle() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-trajectory-archive-test-") as raw:
        # Resolve first: archive_completed_task_runs() returns resolved paths, and on
        # macOS the temp root is under the /var -> /private/var symlink.
        root = Path(raw).resolve()
        original_task = root / "task"
        original_task.mkdir()
        (original_task / "instruction.md").write_text("task", encoding="utf-8")
        jobs_dir = root / "harbor-jobs"
        run_id = "archive-test"
        snapshot = jobs_dir / f"{run_id}.agent-task-snapshot"
        oracle_snapshot = jobs_dir / f"{run_id}.oracle-task-snapshot"
        agent_specs = [
            ("claude-code", "claude-opus", "anthropic/claude-opus-5"),
            ("codex", "codex-gpt-6-astra", "openai/gpt-6-astra"),
            ("antigravity", "gemini-3-8-flash", "google/gemini-3.8-flash"),
        ]
        results = []
        for agent, label, model in agent_specs:
            job_name = f"{run_id}-{label}"
            job_dir = jobs_dir / job_name
            trial_dir = job_dir / f"{run_id}.agent__{agent}"
            trial_dir.mkdir(parents=True)
            (trial_dir / "result.json").write_text(
                json.dumps(
                    {
                        "task_id": {"path": str(snapshot)},
                        "finished_at": "2026-07-22T21:19:06Z",
                        "verifier_result": {"rewards": {"reward": 1.0}},
                    }
                ),
                encoding="utf-8",
            )
            (trial_dir / "exception.txt").write_text(
                f"{agent} exception evidence\n",
                encoding="utf-8",
            )
            results.append(
                harbor_runner.JobResult(
                    agent=agent,
                    model=model,
                    label=label,
                    job_name=job_name,
                    n_trials_expected=1,
                    returncode=0,
                    elapsed_sec=1.0,
                    job_dir=str(job_dir),
                    runner_log=str(jobs_dir / f"{job_name}.runner.log"),
                    resumed=False,
                )
            )

        oracle_dir = jobs_dir / f"{run_id}-oracle"
        oracle_trial_dir = oracle_dir / f"{run_id}.oracle__trial"
        oracle_trial_dir.mkdir(parents=True)
        (oracle_dir / "config.json").write_text(
            json.dumps({"agents": [{"name": "oracle"}]}),
            encoding="utf-8",
        )
        (oracle_trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_id": {"path": str(oracle_snapshot)},
                    "finished_at": "2026-07-22T21:19:06Z",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                }
            ),
            encoding="utf-8",
        )
        (oracle_trial_dir / "exception.txt").write_text(
            "oracle exception evidence\n",
            encoding="utf-8",
        )

        summary_path = harbor_runner.write_summary(jobs_dir, original_task, run_id, results)
        markdown_path = harbor_runner.write_markdown_summary(
            jobs_dir=jobs_dir,
            tasks=[original_task],
            results=results,
            run_id=run_id,
        )
        destination = root / "trajectories"
        destination.mkdir()
        (destination / "stale-run.marker").write_text("old", encoding="utf-8")
        moved = harbor_runner.archive_completed_task_runs(
            tasks=[original_task],
            results=results,
            summary_path=summary_path,
            markdown_summary_path=markdown_path,
            run_id=run_id,
            destination_root=destination,
        )

        trajectory_root = destination
        assert moved == [destination]
        assert all((trajectory_root / agent).is_dir() for agent, _, _ in agent_specs)
        assert (trajectory_root / "oracle").is_dir()
        assert all(
            (trajectory_root / agent / f"{run_id}.agent__{agent}" / "exception.txt").is_file()
            for agent, _, _ in agent_specs
        )
        assert (
            trajectory_root / "oracle" / f"{run_id}.oracle__trial" / "exception.txt"
        ).is_file()
        assert not (trajectory_root / "stale-run.marker").exists()
        trajectory_summary = trajectory_root / "summary.md"
        assert trajectory_summary.is_file()
        summary_text = trajectory_summary.read_text(encoding="utf-8")
        assert "| Task | Oracle | claude-code | codex | antigravity | Status |" in summary_text
        assert "| task | pass, reward 1 |" in summary_text

        retained = trajectory_root / "previous-success.marker"
        retained.write_text("keep", encoding="utf-8")
        partial_result = replace(results[0], n_trials_expected=2)
        harbor_runner.archive_completed_task_runs(
            tasks=[original_task],
            results=[partial_result],
            summary_path=summary_path,
            markdown_summary_path=markdown_path,
            run_id="partial-follow-up",
            destination_root=destination,
        )
        assert retained.is_file()
        assert (trajectory_root / "partial-follow-up").is_dir()


def check_remote_archive_promotion_uses_direct_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-remote-archive-test-") as raw:
        root = Path(raw)
        destination = root / "trajectories"
        archive = destination / "hr_remote-run"
        source = archive / "example-task" / "trajectories"
        (source / "oracle" / "trial-0").mkdir(parents=True)
        (source / "claude-code" / "trial-0").mkdir(parents=True)
        (source / "summary.md").write_text("# remote summary\n", encoding="utf-8")
        (destination / ".hr_remote-run.sha256").write_text("sha256:test\n", encoding="utf-8")
        (destination / "stale-output").mkdir(parents=True)
        (destination / "stale-output" / "old.txt").write_text("old", encoding="utf-8")

        promoted = harbor_runner.promote_remote_trajectory_archive(archive, destination)

        assert promoted == destination.resolve()
        assert (destination / "summary.md").is_file()
        assert (destination / "oracle" / "trial-0").is_dir()
        assert (destination / "claude-code" / "trial-0").is_dir()
        assert not archive.exists()
        assert not (destination / ".hr_remote-run.sha256").exists()
        assert not (destination / "stale-output").exists()


def check_remote_partial_archive_matches_local_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-remote-partial-archive-test-") as raw:
        root = Path(raw)
        destination = root / "trajectories"
        archive = destination / "hr_remote-run"
        source = archive / "example-task" / "trajectories"
        (source / "oracle" / "trial-0").mkdir(parents=True)
        (source / "codex" / "trial-0").mkdir(parents=True)
        (source / "oracle" / "trial-0" / "trajectory.json").write_text(
            "{\"oracle\": true}\n",
            encoding="utf-8",
        )
        (source / "codex" / "trial-0" / "trajectory.json").write_text(
            "{\"agent\": true}\n",
            encoding="utf-8",
        )
        (source / "summary.md").write_text("# partial remote summary\n", encoding="utf-8")
        (destination / ".hr_remote-run.sha256").write_text("sha256:test\n", encoding="utf-8")
        (destination / "previous-success").mkdir(parents=True)

        preserved = harbor_runner.preserve_remote_trajectory_archive(
            archive,
            destination,
            "example-task",
        )

        assert preserved == archive.resolve()
        assert (archive / "summary.md").is_file()
        assert (archive / "example-task" / "trajectories" / "oracle").is_dir()
        assert (archive / "example-task" / "trajectories" / "codex").is_dir()
        assert (archive / "example-task" / "trajectories" / "oracle" / "trial-0" / "trajectory.json").is_file()
        assert (archive / "example-task" / "trajectories" / "codex" / "trial-0" / "trajectory.json").is_file()
        assert (destination / "previous-success").is_dir()
        assert not (destination / ".hr_remote-run.sha256").exists()


def check_remote_oracle_exception_evidence_is_preserved() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-remote-oracle-exception-test-") as raw:
        root = Path(raw)
        destination = root / "trajectories"
        archive = destination / "hr_remote-run"
        archive.mkdir(parents=True)
        (archive / "oracle-exception.json").write_text(
            '{"exception_type":"MissingReward"}\n',
            encoding="utf-8",
        )
        (destination / ".hr_remote-run.sha256").write_text("sha256:test\n", encoding="utf-8")

        preserved = harbor_runner.preserve_remote_trajectory_archive(
            archive,
            destination,
            "example-task",
        )

        assert preserved == archive.resolve()
        assert (archive / "oracle-exception.json").is_file()
        assert not (destination / ".hr_remote-run.sha256").exists()


def check_remote_error_without_agent_trials_keeps_compact_evidence() -> None:
    status = {
        "state": "ERROR",
        "terminal_reason": "EXECUTION_ERROR",
        "error": {
            "type": "TypeError",
            "message": "Body is unusable: Body has already been read",
        },
    }
    results = {
        "oracle": {"verdict": "PASS", "reward": 1},
        "summary": {
            "agent_trials_expected": 9,
            "agent_trials_finished": 0,
            "exception_count": 0,
        },
        "trials": [],
    }
    assert harbor_runner.remote_error_has_no_agent_trials(status, results)
    with tempfile.TemporaryDirectory(prefix="honeydew-remote-error-evidence-test-") as raw:
        destination = harbor_runner.write_remote_error_evidence(
            Path(raw) / "trajectories",
            "hr_remote-error",
            status,
            results,
        )
        assert sorted(path.name for path in destination.iterdir()) == [
            "oracle_exception.txt",
            "remote-results.json",
            "remote-status.json",
            "summary.md",
        ]
        assert "archive was not downloaded" in (destination / "summary.md").read_text(encoding="utf-8")
        assert "Body is unusable: Body has already been read" in (
            destination / "oracle_exception.txt"
        ).read_text(encoding="utf-8")


def check_remote_archive_extracts_evidence_without_task_files() -> None:
    run_id = "hr_remote-extract"
    with tempfile.TemporaryDirectory(prefix="honeydew-remote-evidence-extract-test-") as raw:
        root = Path(raw)
        source = root / run_id
        task_root = source / "example-task"
        trajectories = task_root / "trajectories" / "codex" / "trial-1"
        trajectories.mkdir(parents=True)
        (task_root / "task.toml").write_text(
            '[environment]\nnetwork_mode = "public"\n', encoding="utf-8"
        )
        (task_root / "instruction.md").write_text("private task prompt\n", encoding="utf-8")
        (task_root / "solution").mkdir()
        (task_root / "solution" / "solve.py").write_text("private solution\n", encoding="utf-8")
        (trajectories / "trajectory.json").write_text("{}\n", encoding="utf-8")
        oracle_exception = task_root / "oracle" / "trial-0" / "exception.txt"
        oracle_exception.parent.mkdir(parents=True)
        oracle_exception.write_text("oracle exception\n", encoding="utf-8")
        agent_exception = task_root / "agent-runs" / "codex" / "trial-1" / "exception.txt"
        agent_exception.parent.mkdir(parents=True)
        agent_exception.write_text("agent exception\n", encoding="utf-8")
        (task_root / "summary.md").write_text("summary\n", encoding="utf-8")

        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            archive.add(source, arcname=run_id)

        destination = root / "trajectories" / run_id
        count = harbor_runner.safe_remote_extract(
            archive_bytes.getvalue(),
            destination,
            run_id,
        )

        assert count > 1
        assert (destination / "example-task" / "trajectories" / "codex" / "trial-1" / "trajectory.json").is_file()
        assert (destination / "example-task" / "oracle" / "trial-0" / "exception.txt").read_text(encoding="utf-8") == "oracle exception\n"
        assert (destination / "example-task" / "agent-runs" / "codex" / "trial-1" / "exception.txt").read_text(encoding="utf-8") == "agent exception\n"
        assert (destination / "example-task" / "summary.md").is_file()
        assert not (destination / "example-task" / "task.toml").exists()
        assert not (destination / "example-task" / "instruction.md").exists()
        assert not (destination / "example-task" / "solution").exists()


def check_remote_archive_refuses_non_trajectory_manifest() -> None:
    original_urlopen = harbor_runner.urllib.request.urlopen
    called = False

    def fail_if_downloaded(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("the client attempted to download a non-trajectory archive")

    manifest = {
        "download_url": "https://storage.example/full-task.tar.gz",
        "sha256": "sha256:" + "a" * 64,
        "size_bytes": 123,
        "root_directory": "hr_remote-scope",
        "entry_count": 10,
        "archive_scope": "full-run",
    }
    harbor_runner.urllib.request.urlopen = fail_if_downloaded  # type: ignore[assignment]
    try:
        try:
            harbor_runner.download_remote_archive(
                Path("/tmp/harbor-runner-scope-test"),
                "hr_remote-scope",
                manifest,
            )
        except harbor_runner.RemoteClientError as error:
            assert error.code == "archive_scope"
        else:
            raise AssertionError("the client accepted a non-trajectory archive manifest")
    finally:
        harbor_runner.urllib.request.urlopen = original_urlopen
    assert not called


def check_remote_archive_flag_matches_local_behavior() -> None:
    original_request = harbor_runner.remote_json_request
    original_poll = harbor_runner.poll_remote_status
    requests: list[str] = []

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-no-archive-test-") as raw:
        root = Path(raw)
        task_root = root / "task"
        task_root.mkdir()
        (task_root / "task.toml").write_text("", encoding="utf-8")
        (task_root / "instruction.md").write_text("task\n", encoding="utf-8")
        for directory in ("solution", "tests", "environment"):
            (task_root / directory).mkdir()
        jobs_dir = root / "harbor-jobs"
        trajectories_dir = root / "trajectories"

        def fake_remote_json_request(
            method: str,
            url: str,
            token: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object], dict[str, str]]:
            requests.append(url)
            if method == "POST" and url.endswith("/runs"):
                return 201, {"run_id": "hr_123456789012", "state": "UPLOADING"}, {}
            if method == "POST" and url.endswith(":start"):
                return 200, {"state": "QUEUED"}, {}
            if method == "GET" and url.endswith("/results"):
                return 200, {"summary": {"exception_count": 0}}, {}
            raise AssertionError(f"unexpected remote request: {method} {url}")

        def fake_poll_remote_status(*args: object, **kwargs: object) -> dict[str, object]:
            return {"state": "COMPLETE", "terminal_reason": None}

        args = SimpleNamespace(
            env="modal",
            archive_only=False,
            oracle_sort=False,
            dry_run=False,
            env_file=[],
            agent_env=[],
            verifier_env=[],
            environment_kwarg=[],
            agent_kwarg=[],
            artifact=[],
            modal_secret=[],
            workbench_token="test-token",
            remote_poll_min=0.25,
            remote_poll_max=1.0,
            remote_progress_interval_sec=30.0,
            service_url="https://example.test/v1",
            run=[],
            n_concurrent=None,
            default_concurrency=3,
            repeats=1,
            jobs_dir=jobs_dir,
            run_id="remote-no-archive",
            resume=False,
            archive_completed=False,
            cancel_on_interrupt=True,
            pass_threshold=1.0,
            completed_trajectories_dir=trajectories_dir,
        )

        harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
        harbor_runner.poll_remote_status = fake_poll_remote_status  # type: ignore[assignment]
        try:
            assert harbor_runner.run_remote(task_root, args) == 0
        finally:
            harbor_runner.remote_json_request = original_request
            harbor_runner.poll_remote_status = original_poll

    assert not any(url.endswith("/trajectories") for url in requests)
    assert not trajectories_dir.exists()


def check_remote_start_error_preserves_service_message() -> None:
    original_request = harbor_runner.remote_json_request

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-start-error-test-") as raw:
        root = Path(raw)
        task_root = root / "task"
        task_root.mkdir()
        (task_root / "task.toml").write_text("", encoding="utf-8")
        (task_root / "instruction.md").write_text("task\n", encoding="utf-8")
        for directory in ("solution", "tests", "environment"):
            (task_root / directory).mkdir()
        jobs_dir = root / "harbor-jobs"

        def fake_remote_json_request(
            method: str,
            url: str,
            token: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object], dict[str, str]]:
            if method == "POST" and url.endswith("/runs"):
                return 201, {"run_id": "hr_123456789012", "state": "UPLOADING"}, {}
            if method == "POST" and url.endswith(":start"):
                raise harbor_runner.RemoteClientError(
                    422,
                    "archive_validation",
                    "The Harbor task bundle failed archive validation.",
                )
            raise AssertionError(f"unexpected remote request: {method} {url}")

        args = SimpleNamespace(
            env="modal",
            archive_only=False,
            oracle_sort=False,
            dry_run=False,
            env_file=[],
            agent_env=[],
            verifier_env=[],
            environment_kwarg=[],
            agent_kwarg=[],
            artifact=[],
            modal_secret=[],
            workbench_token="test-token",
            remote_poll_min=0.25,
            remote_poll_max=1.0,
            remote_progress_interval_sec=30.0,
            service_url="https://example.test/v1",
            run=[],
            n_concurrent=None,
            default_concurrency=3,
            repeats=1,
            jobs_dir=jobs_dir,
            run_id="remote-start-error",
            resume=False,
            archive_completed=False,
            cancel_on_interrupt=True,
            pass_threshold=1.0,
            completed_trajectories_dir=root / "trajectories",
        )

        harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
        error_output = io.StringIO()
        try:
            with contextlib.redirect_stderr(error_output):
                assert harbor_runner.run_remote(task_root, args) == 2
        finally:
            harbor_runner.remote_json_request = original_request

    assert error_output.getvalue().strip() == (
        "remote start rejected: The Harbor task bundle failed archive validation."
    )


def check_remote_oracle_exception_downloads_archive() -> None:
    original_request = harbor_runner.remote_json_request
    original_poll = harbor_runner.poll_remote_status
    original_download = harbor_runner.download_remote_archive
    requests: list[str] = []

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-oracle-run-test-") as raw:
        root = Path(raw)
        task_root = root / "task"
        task_root.mkdir()
        (task_root / "task.toml").write_text("", encoding="utf-8")
        (task_root / "instruction.md").write_text("task\n", encoding="utf-8")
        for directory in ("solution", "tests", "environment"):
            (task_root / directory).mkdir()
        jobs_dir = root / "harbor-jobs"
        trajectories_dir = root / "trajectories"
        run_id = "hr_123456789012"

        def fake_remote_json_request(
            method: str,
            url: str,
            token: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object], dict[str, str]]:
            requests.append(url)
            if method == "POST" and url.endswith("/runs"):
                return 201, {"run_id": run_id, "state": "UPLOADING"}, {}
            if method == "POST" and url.endswith(":start"):
                return 200, {"state": "QUEUED"}, {}
            if method == "GET" and url.endswith("/results"):
                return 200, {
                    "oracle": {
                        "verdict": "EXCEPTION",
                        "exception": {
                            "type": "HarborProcessError",
                            "message": "ValueError: Either datasets or tasks must be provided.",
                        },
                    },
                    "summary": {"exception_count": 1},
                }, {}
            if method == "GET" and url.endswith("/trajectories"):
                return 200, {
                    "size_bytes": 1,
                    "entry_count": 1,
                    "archive_scope": harbor_runner.REMOTE_TRAJECTORY_ARCHIVE_SCOPE,
                }, {}
            raise AssertionError(f"unexpected remote request: {method} {url}")

        def fake_poll_remote_status(*args: object, **kwargs: object) -> dict[str, object]:
            return {"state": "ORACLE_EXCEPTION", "terminal_reason": "ORACLE_EXCEPTION"}

        def fake_download_remote_archive(
            base_dir: Path,
            downloaded_run_id: str,
            manifest: dict[str, object],
        ) -> Path:
            archive = base_dir / downloaded_run_id
            archive.mkdir(parents=True)
            (archive / "oracle-exception.json").write_text(
                '{"exception_type":"MissingReward"}\n',
                encoding="utf-8",
            )
            return archive

        args = SimpleNamespace(
            env="modal",
            archive_only=False,
            oracle_sort=False,
            dry_run=False,
            env_file=[],
            agent_env=[],
            verifier_env=[],
            environment_kwarg=[],
            agent_kwarg=[],
            artifact=[],
            modal_secret=[],
            workbench_token="test-token",
            remote_poll_min=0.25,
            remote_poll_max=1.0,
            remote_progress_interval_sec=30.0,
            service_url="https://example.test/v1",
            run=[],
            n_concurrent=None,
            default_concurrency=3,
            repeats=1,
            jobs_dir=jobs_dir,
            run_id="remote-oracle-exception",
            resume=False,
            archive_completed=True,
            cancel_on_interrupt=True,
            pass_threshold=1.0,
            completed_trajectories_dir=trajectories_dir,
        )

        harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
        harbor_runner.poll_remote_status = fake_poll_remote_status  # type: ignore[assignment]
        harbor_runner.download_remote_archive = fake_download_remote_archive  # type: ignore[assignment]
        try:
            assert harbor_runner.run_remote(task_root, args) == 5
            assert (trajectories_dir / run_id / "oracle-exception.json").is_file()
            exception_text = (trajectories_dir / run_id / "oracle_exception.txt").read_text(encoding="utf-8")
            assert "ValueError: Either datasets or tasks must be provided." in exception_text
        finally:
            harbor_runner.remote_json_request = original_request
            harbor_runner.poll_remote_status = original_poll
            harbor_runner.download_remote_archive = original_download

    assert any(url.endswith("/trajectories") for url in requests)


def check_remote_agent_exception_replaces_direct_archive() -> None:
    original_request = harbor_runner.remote_json_request
    original_poll = harbor_runner.poll_remote_status
    original_download = harbor_runner.download_remote_archive

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-agent-exception-test-") as raw:
        root = Path(raw)
        task_root = root / "task"
        task_root.mkdir()
        (task_root / "task.toml").write_text("", encoding="utf-8")
        (task_root / "instruction.md").write_text("task\n", encoding="utf-8")
        for directory in ("solution", "tests", "environment"):
            (task_root / directory).mkdir()
        jobs_dir = root / "harbor-jobs"
        trajectories_dir = root / "trajectories"
        trajectories_dir.mkdir()
        (trajectories_dir / "stale-run.marker").write_text("old", encoding="utf-8")
        run_id = "hr_123456789012"

        def fake_remote_json_request(
            method: str,
            url: str,
            token: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object], dict[str, str]]:
            if method == "POST" and url.endswith("/runs"):
                return 201, {"run_id": run_id, "state": "UPLOADING"}, {}
            if method == "POST" and url.endswith(":start"):
                return 200, {"state": "QUEUED"}, {}
            if method == "GET" and url.endswith("/results"):
                return 200, {
                    "oracle": {"verdict": "PASS", "reward": 1},
                    "summary": {
                        "agent_trials_expected": 1,
                        "agent_trials_finished": 1,
                        "exception_count": 1,
                    },
                    "trials": [
                        {
                            "agent_id": "codex-gpt-6-astra",
                            "verdict": "EXCEPTION",
                            "exception": {
                                "type": "HarborProcessError",
                                "message": "agent process exited with status 1",
                            },
                        }
                    ],
                }, {}
            if method == "GET" and url.endswith("/trajectories"):
                return 200, {
                    "size_bytes": 1,
                    "entry_count": 1,
                    "archive_scope": harbor_runner.REMOTE_TRAJECTORY_ARCHIVE_SCOPE,
                }, {}
            raise AssertionError(f"unexpected remote request: {method} {url}")

        def fake_poll_remote_status(*args: object, **kwargs: object) -> dict[str, object]:
            return {"state": "COMPLETE", "terminal_reason": "AGENTS_COMPLETE"}

        def fake_download_remote_archive(
            base_dir: Path,
            downloaded_run_id: str,
            manifest: dict[str, object],
        ) -> Path:
            archive = base_dir / downloaded_run_id
            trial_dir = archive / "example-task" / "trajectories" / "codex" / "trial-0"
            trial_dir.mkdir(parents=True)
            (trial_dir / "exception.txt").write_text(
                "agent process exited with status 1\n",
                encoding="utf-8",
            )
            (trial_dir / "result.json").write_text("{}\n", encoding="utf-8")
            (archive / "example-task" / "trajectories" / "summary.md").write_text(
                "# remote partial summary\n",
                encoding="utf-8",
            )
            return archive

        args = SimpleNamespace(
            env="modal",
            archive_only=False,
            oracle_sort=False,
            dry_run=False,
            env_file=[],
            agent_env=[],
            verifier_env=[],
            environment_kwarg=[],
            agent_kwarg=[],
            artifact=[],
            modal_secret=[],
            workbench_token="test-token",
            remote_poll_min=0.25,
            remote_poll_max=1.0,
            remote_progress_interval_sec=30.0,
            service_url="https://example.test/v1",
            run=[],
            n_concurrent=None,
            default_concurrency=3,
            repeats=1,
            jobs_dir=jobs_dir,
            run_id="remote-agent-exception",
            resume=False,
            archive_completed=True,
            cancel_on_interrupt=True,
            pass_threshold=1.0,
            completed_trajectories_dir=trajectories_dir,
        )

        harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
        harbor_runner.poll_remote_status = fake_poll_remote_status  # type: ignore[assignment]
        harbor_runner.download_remote_archive = fake_download_remote_archive  # type: ignore[assignment]
        try:
            assert harbor_runner.run_remote(task_root, args) == 4
        finally:
            harbor_runner.remote_json_request = original_request
            harbor_runner.poll_remote_status = original_poll
            harbor_runner.download_remote_archive = original_download

        assert (trajectories_dir / "codex" / "trial-0" / "exception.txt").is_file()
        assert (trajectories_dir / "summary.md").is_file()
        assert not (trajectories_dir / "stale-run.marker").exists()
        assert not (trajectories_dir / run_id).exists()


def check_remote_service_error_skips_archive_download() -> None:
    original_request = harbor_runner.remote_json_request
    original_poll = harbor_runner.poll_remote_status
    requests: list[str] = []

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-service-error-test-") as raw:
        root = Path(raw)
        task_root = root / "task"
        task_root.mkdir()
        (task_root / "task.toml").write_text("", encoding="utf-8")
        (task_root / "instruction.md").write_text("task\n", encoding="utf-8")
        for directory in ("solution", "tests", "environment"):
            (task_root / directory).mkdir()
        jobs_dir = root / "harbor-jobs"
        trajectories_dir = root / "trajectories"
        run_id = "hr_123456789012"

        def fake_remote_json_request(
            method: str,
            url: str,
            token: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object], dict[str, str]]:
            requests.append(url)
            if method == "POST" and url.endswith("/runs"):
                return 201, {"run_id": run_id, "state": "UPLOADING"}, {}
            if method == "POST" and url.endswith(":start"):
                return 200, {"state": "QUEUED"}, {}
            if method == "GET" and url.endswith("/results"):
                return 200, {
                    "oracle": {"verdict": "PASS", "reward": 1},
                    "summary": {
                        "agent_trials_expected": 9,
                        "agent_trials_finished": 0,
                        "exception_count": 0,
                    },
                    "trials": [],
                }, {}
            raise AssertionError(f"unexpected remote request: {method} {url}")

        def fake_poll_remote_status(*args: object, **kwargs: object) -> dict[str, object]:
            return {
                "state": "ERROR",
                "terminal_reason": "EXECUTION_ERROR",
                "error": {
                    "type": "TypeError",
                    "message": "Body is unusable: Body has already been read",
                },
            }

        args = SimpleNamespace(
            env="modal",
            archive_only=False,
            oracle_sort=False,
            dry_run=False,
            env_file=[],
            agent_env=[],
            verifier_env=[],
            environment_kwarg=[],
            agent_kwarg=[],
            artifact=[],
            modal_secret=[],
            workbench_token="test-token",
            remote_poll_min=0.25,
            remote_poll_max=1.0,
            remote_progress_interval_sec=30.0,
            service_url="https://example.test/v1",
            run=[],
            n_concurrent=None,
            default_concurrency=3,
            repeats=1,
            jobs_dir=jobs_dir,
            run_id="remote-service-error",
            resume=False,
            archive_completed=True,
            cancel_on_interrupt=True,
            pass_threshold=1.0,
            completed_trajectories_dir=trajectories_dir,
        )

        harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
        harbor_runner.poll_remote_status = fake_poll_remote_status  # type: ignore[assignment]
        try:
            assert harbor_runner.run_remote(task_root, args) == 5
        finally:
            harbor_runner.remote_json_request = original_request
            harbor_runner.poll_remote_status = original_poll

        evidence = trajectories_dir / run_id
        assert (evidence / "remote-status.json").is_file()
        assert (evidence / "remote-results.json").is_file()
        assert not any(url.endswith("/trajectories") for url in requests)


def check_sigterm_enters_cleanup_path() -> None:
    try:
        harbor_runner.handle_sigterm(15, None)
    except KeyboardInterrupt:
        return
    raise AssertionError("SIGTERM handler did not enter the cleanup path")


def check_smoke_mode_wiring() -> None:
    task_root = Path(__file__).resolve().parents[1] / "task"
    project = harbor_runner.load_smoke_project(task_root)
    assert project.solution_dir.is_dir()
    assert project.tests_dir.is_dir()
    assert "OUTPUT_DIR" not in harbor_runner.smoke_project_env(project.task_toml)
    rendered = harbor_runner.redact_smoke_command(
        ["docker", "run", "-e", "API_KEY=not-for-logs", "image"]
    )
    assert "not-for-logs" not in rendered
    assert "<redacted>" in rendered


def check_public_network_snapshots() -> None:
    task_root = Path(__file__).resolve().parents[1] / "task"
    original_sleep = harbor_runner.time.sleep
    harbor_runner.time.sleep = lambda _seconds: None
    try:
        with tempfile.TemporaryDirectory(prefix="honeydew-snapshot-test-") as raw:
            jobs_dir = Path(raw) / "jobs"
            oracle = harbor_runner.snapshot_task_root(
                task_root,
                jobs_dir,
                "snapshot-test",
                snapshot_label="oracle-task-snapshot",
            )
            agent = harbor_runner.snapshot_task_root(
                oracle,
                jobs_dir,
                "snapshot-test",
                snapshot_label="agent-task-snapshot",
            )
            assert oracle != agent
            assert harbor_runner.DEFAULT_NETWORK_MODE == "public"
            assert harbor_runner.load_toml(oracle / "task.toml")["environment"]["network_mode"] == "public"
            assert harbor_runner.load_toml(agent / "task.toml")["environment"]["network_mode"] == "public"
            assert harbor_runner.task_network_mode(agent) == "public"
            assert harbor_runner.docker_network_args("public") == []
            assert harbor_runner.docker_network_args("no-network") == ["--network", "none"]
    finally:
        harbor_runner.time.sleep = original_sleep


def check_oracle_spinner() -> None:
    class TTYBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    output = TTYBuffer()
    display = harbor_runner.OracleProgressDisplay(output)
    display.update("Oracle progress: 0/1 result(s) finished (0.0%)")
    display.update("Oracle progress: 1/1 result(s) finished (100.0%)")
    display.finish()
    rendered = output.getvalue()
    assert "Oracle [|] progress: 0/1" in rendered
    assert "Oracle [/] progress: 1/1" in rendered
    assert "\033[K" in rendered


def check_agent_progress_order() -> None:
    specs = [
        SimpleNamespace(job_name="claude", label="claude-opus-5"),
        SimpleNamespace(job_name="codex", label="codex-gpt-6-astra"),
        SimpleNamespace(job_name="gemini", label="gemini-3-8-flash"),
    ]
    output = io.StringIO()
    reporter = harbor_runner.ProgressReporter(specs, output)
    reporter.report(specs[2], "gemini update")
    block = output.getvalue().split("=============", 1)[0]
    assert block.index("claude-opus-5:") < block.index("codex-gpt-6-astra:")
    assert block.index("codex-gpt-6-astra:") < block.index("gemini-3-8-flash:")
    assert "gemini-3-8-flash: gemini update" in block


def check_remote_defaults_load_dotenv() -> None:
    keys = ("WORKBENCH_HARBOR_SERVICE_URL", "WORKBENCH_RUNNER_TOKEN")
    missing = object()
    original_values = {key: os.environ.get(key, missing) for key in keys}
    original_cwd = Path.cwd()
    original_run_remote = harbor_runner.run_remote
    seen: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="honeydew-dotenv-test-") as raw:
        root = Path(raw)
        task_root = root / "task"
        task_root.mkdir()
        (task_root / "task.toml").write_text("", encoding="utf-8")
        (task_root / "instruction.md").write_text("task\n", encoding="utf-8")
        (root / ".env").write_text(
            "WORKBENCH_HARBOR_SERVICE_URL=\"https://example.test/v1\"\n"
            "export WORKBENCH_RUNNER_TOKEN=dotenv-token # local credential\n",
            encoding="utf-8",
        )

        def fake_run_remote(task: Path, args: object) -> int:
            seen["task"] = task
            seen["args"] = args
            return 0

        os.environ.pop("WORKBENCH_HARBOR_SERVICE_URL", None)
        os.environ.pop("WORKBENCH_RUNNER_TOKEN", None)
        harbor_runner.run_remote = fake_run_remote  # type: ignore[assignment]
        os.chdir(root)
        try:
            assert harbor_runner.main(["task"]) == 0
        finally:
            os.chdir(original_cwd)
            harbor_runner.run_remote = original_run_remote

    for key, value in original_values.items():
        if value is missing:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    args = seen["args"]
    assert seen["task"] == task_root.resolve()
    assert getattr(args, "service_url") == "https://example.test/v1"
    assert getattr(args, "workbench_token") == "dotenv-token"
    assert getattr(args, "cancel_on_interrupt") is True


def check_remote_bundle_wiring() -> None:
    task_root = Path(__file__).resolve().parents[1] / "task"
    archive, digest, size = harbor_runner.build_remote_task_bundle(task_root)
    assert archive.startswith(b"\x1f\x8b")
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
    assert size == len(archive)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = {member.name for member in tar.getmembers()}
    assert all(".git/" not in name for name in names)
    assert all("harbor-jobs/" not in name for name in names)
    assert "task/task.toml" in names
    assert "task/instruction.md" in names
    assert harbor_runner.remote_url("https://example.test/v1", "/v1/harbor/runs/hr_abc") == "https://example.test/v1/harbor/runs/hr_abc"


def check_remote_policy_wiring() -> None:
    args = SimpleNamespace(run=[], n_concurrent=5, default_concurrency=3, repeats=2)
    payload = harbor_runner.remote_agent_payload(args)
    gemini = next(item for item in payload if item["id"] == "gemini-3-8-flash")
    assert gemini["agent"] == "antigravity"
    assert gemini["model"] == "google/gemini-3.8-flash"
    assert all(item["concurrency"] == 5 for item in payload)
    assert sum(item["concurrency"] for item in payload) * args.repeats == 30
    execution = harbor_runner.remote_execution_payload(
        SimpleNamespace(repeats=2, pass_threshold=1.0),
        payload,
    )
    assert execution["execution_policy_id"] == "scientific-offline-v1"
    assert "network_mode" not in execution
    assert execution["agents"] == payload
    args.repeats = 3
    try:
        harbor_runner.remote_agent_payload(args)
    except harbor_runner.RemoteInputError as exc:
        assert "30-trial" in str(exc)
    else:
        raise AssertionError("remote policy allowed more than 30 total trials")


def check_default_remote_trial_count() -> None:
    args = SimpleNamespace(
        run=[],
        n_concurrent=None,
        default_concurrency=harbor_runner.REMOTE_DEFAULT_CONCURRENCY,
        repeats=harbor_runner.REMOTE_DEFAULT_REPEATS,
    )
    payload = harbor_runner.remote_agent_payload(args)
    assert len(payload) == 3
    assert all(item["concurrency"] == 4 for item in payload)
    # One attempt in four fan-out lanes produces four concurrent trials per agent.
    assert all(args.repeats * int(item["concurrency"]) == 4 for item in payload)
    # Keep the complete default campaign at twelve trials.
    assert args.repeats * sum(int(item["concurrency"]) for item in payload) == 12


def check_remote_progress_reporting() -> None:
    status = {
        "state": "AGENTS_RUNNING",
        "updated_at": "2026-07-22T12:34:56.000Z",
        "terminal_reason": None,
        "oracle": {"state": "PASS", "reward": 1.0, "exception": None},
        "agents": [
            {
                "id": "claude-opus",
                "agent": "claude-code",
                "state": "RUNNING",
                "expected_trials": 3,
                "finished_trials": 2,
                "pass_count": 1,
                "fail_count": 1,
                "exception_count": 0,
                "job_id": "hr_abc-claude-opus",
            },
            {
                "id": "codex-gpt-6-astra",
                "agent": "codex",
                "state": "QUEUED",
                "expected_trials": 3,
                "finished_trials": 0,
                "pass_count": 0,
                "fail_count": 0,
                "exception_count": 0,
            },
            {
                "id": "gemini-3-8-flash",
                "agent": "antigravity",
                "state": "QUEUED",
                "expected_trials": 3,
                "finished_trials": 0,
                "pass_count": 0,
                "fail_count": 0,
                "exception_count": 0,
            },
        ],
    }
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        signature = harbor_runner.print_remote_progress(
            status,
            None,
            elapsed_sec=65,
        )
        same_signature = harbor_runner.print_remote_progress(
            status,
            signature,
            elapsed_sec=95,
            force=True,
        )
    rendered = output.getvalue()
    assert signature == same_signature
    assert "remote state: AGENTS_RUNNING" in rendered
    assert "remote heartbeat:" not in rendered
    assert rendered.count("remote state: AGENTS_RUNNING") == 1
    assert "server updated: 2026-07-22T12:34:56.000Z" in rendered
    assert "Claude Code: RUNNING 2/3 pass=1 fail=1" in rendered
    assert "Codex: RUNNING 0/3 pass=0 fail=0" in rendered
    assert "Gemini: RUNNING 0/3 pass=0 fail=0" in rendered
    if harbor_runner.RICH_AVAILABLE:
        table_output = io.StringIO()
        console = harbor_runner.Console(file=table_output, force_terminal=False)
        console.print(harbor_runner.remote_progress_table(status, elapsed_sec=65))
        table_rendered = table_output.getvalue()
        assert "PASS" in table_rendered
        assert "FAIL" in table_rendered
        assert "STATUS" in table_rendered
        assert "Oracle" in table_rendered
        assert "Claude Code" in table_rendered
        assert "Codex" in table_rendered
        assert "Gemini" in table_rendered

        class LiveCapture:
            def __init__(self) -> None:
                self.updates: list[tuple[object, bool]] = []

            def update(self, renderable: object, *, refresh: bool = False) -> None:
                self.updates.append((renderable, refresh))

        live = LiveCapture()
        live_output = io.StringIO()
        with contextlib.redirect_stdout(live_output):
            harbor_runner.print_remote_progress(status, None, live=live, spinner_index=0)
            harbor_runner.print_remote_progress(status, signature, live=live, force=True, spinner_index=1)
        assert live_output.getvalue() == ""
        assert len(live.updates) == 2
        assert all(refresh for _renderable, refresh in live.updates)


def check_remote_poll_retries_network_error() -> None:
    original_request = harbor_runner.remote_json_request
    original_sleep = harbor_runner.time.sleep
    calls = 0

    def fake_remote_json_request(
        method: str,
        url: str,
        token: str,
        **kwargs: object,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise harbor_runner.RemoteClientError(
                0,
                "network",
                "Could not reach the Workbench Harbor service.",
                retryable=True,
            )
        return 200, {"state": "COMPLETE", "terminal_reason": "AGENTS_COMPLETE"}, {}

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-poll-retry-test-") as raw:
        state_path = Path(raw) / "service-run.json"
        harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
        harbor_runner.time.sleep = lambda _seconds: None
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                status = harbor_runner.poll_remote_status(
                    "https://example.test/v1",
                    "hr_retry-test",
                    "test-token",
                    minimum_delay=0.25,
                    maximum_delay=1.0,
                    progress_interval=30.0,
                    agent_order=None,
                    state_path=state_path,
                    state={},
                )
        finally:
            harbor_runner.remote_json_request = original_request
            harbor_runner.time.sleep = original_sleep

    assert calls == 2
    assert status["state"] == "COMPLETE"
    assert "remote poll network error; retrying" in output.getvalue()


def check_remote_request_retries_network_error() -> None:
    original_request = harbor_runner.remote_json_request
    original_sleep = harbor_runner.time.sleep
    calls = 0
    sleeps: list[float] = []

    def fake_remote_json_request(
        method: str,
        url: str,
        token: str,
        **kwargs: object,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise harbor_runner.RemoteClientError(
                0,
                "network",
                "Could not reach the Workbench Harbor service.",
                retryable=True,
            )
        return 200, {"ok": True}, {}

    harbor_runner.remote_json_request = fake_remote_json_request  # type: ignore[assignment]
    harbor_runner.time.sleep = sleeps.append
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status, body, _headers = harbor_runner.remote_json_request_with_retry(
                "GET",
                "https://example.test/v1/harbor/runs/hr_retry-test/results",
                "test-token",
                operation="remote results",
            )
    finally:
        harbor_runner.remote_json_request = original_request
        harbor_runner.time.sleep = original_sleep

    assert calls == 3
    assert sleeps == [1.0, 2.0]
    assert status == 200
    assert body == {"ok": True}
    assert "remote results temporarily unavailable" in output.getvalue()


def check_remote_archive_retries_network_error() -> None:
    original_download = harbor_runner._download_remote_archive_once
    original_sleep = harbor_runner.time.sleep
    attempts = 0

    with tempfile.TemporaryDirectory(prefix="honeydew-remote-archive-retry-test-") as raw:
        destination = Path(raw) / "hr_retry-test"
        destination.mkdir()

        def fake_download(
            base_dir: Path,
            run_id: str,
            manifest: dict[str, object],
        ) -> Path:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise harbor_runner.RemoteClientError(
                    0,
                    "archive_download",
                    "The trajectory archive download failed.",
                    retryable=True,
                )
            return destination

        harbor_runner._download_remote_archive_once = fake_download  # type: ignore[assignment]
        harbor_runner.time.sleep = lambda _seconds: None
        try:
            result = harbor_runner.download_remote_archive(Path(raw), "hr_retry-test", {})
        finally:
            harbor_runner._download_remote_archive_once = original_download
            harbor_runner.time.sleep = original_sleep

    assert attempts == 2
    assert result == destination


def check_remote_upload_retries_network_error() -> None:
    original_upload = harbor_runner._remote_upload_once
    original_sleep = harbor_runner.time.sleep
    attempts = 0

    def fake_upload(url: str, archive: bytes | Path, headers: dict[str, str]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise harbor_runner.RemoteClientError(
                0,
                "network",
                "The task bundle upload could not reach Storage.",
                retryable=True,
            )

    harbor_runner._remote_upload_once = fake_upload  # type: ignore[assignment]
    harbor_runner.time.sleep = lambda _seconds: None
    try:
        harbor_runner.remote_upload("https://storage.example/upload", b"bundle", {})
    finally:
        harbor_runner._remote_upload_once = original_upload
        harbor_runner.time.sleep = original_sleep

    assert attempts == 2


def check_remote_upload_progress() -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    original_urlopen = harbor_runner.urllib.request.urlopen
    seen: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float) -> Response:
        assert timeout == harbor_runner.REMOTE_TRANSFER_TIMEOUT_SECONDS
        seen["content_length"] = request.get_header("Content-length")
        body = request.data
        chunks: list[bytes] = []
        while True:
            chunk = body.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
        seen["body"] = b"".join(chunks)
        return Response()

    archive = b"bundle-data" * 5000
    output = io.StringIO()
    harbor_runner.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(output):
            harbor_runner.remote_upload("https://storage.example/upload", archive, {})
    finally:
        harbor_runner.urllib.request.urlopen = original_urlopen

    assert seen["content_length"] == str(len(archive))
    assert seen["body"] == archive
    rendered = output.getvalue()
    if harbor_runner.RICH_AVAILABLE:
        assert "Uploading task bundle" in rendered
        assert "100%" in rendered
    else:
        assert "remote upload: uploading" in rendered
        assert "100.0%" in rendered


def make_quick_task(root: Path) -> Path:
    task = root / "task"
    (task / "environment" / "data").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir(parents=True)
    (task / "instruction.md").write_text(
        "Write result.json under /workspace/output.\n", encoding="utf-8"
    )
    (task / "environment" / "data" / "input.txt").write_text("public\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text(
        "FROM --platform=linux/amd64 scratch\n", encoding="utf-8"
    )
    (task / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text("# fixture verifier\n", encoding="utf-8")
    (task / "solution" / "secret.txt").write_text("must not be staged\n", encoding="utf-8")
    (task / "tests" / "secret.txt").write_text("must not be staged\n", encoding="utf-8")
    (task / "task.toml").write_text(
        """[task]
name = "quick-fixture"

[agent]
timeout_sec = 10

[verifier]
timeout_sec = 10

[environment]
network_mode = "public"
""",
        encoding="utf-8",
    )
    return task


def check_quick_agent_resolution_and_commands() -> None:
    original_which = harbor_runner.shutil.which
    harbor_runner.shutil.which = lambda name: f"/bin/{name}" if name == "claude" else None
    try:
        assert harbor_runner.resolve_quick_agent("auto") == ("claude", "/bin/claude")
        command = harbor_runner.build_quick_agent_command(
            "claude",
            "/bin/claude",
            Path("/tmp/quick-workspace"),
            "solve it",
        )
    finally:
        harbor_runner.shutil.which = original_which
    assert command[:7] == [
        "/bin/claude",
        "--print",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
        "--allow-dangerously-skip-permissions",
        "--add-dir",
    ]
    assert command[-2:] == ["--", "solve it"]


def check_quick_workspace_is_public_only() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-quick-stage-") as raw:
        task = make_quick_task(Path(raw))
        workspace = Path(raw) / "workspace"
        harbor_runner.stage_quick_workspace(task, workspace)
        assert (workspace / "instruction.md").is_file()
        assert (workspace / "data" / "input.txt").read_text(encoding="utf-8") == "public\n"
        assert (workspace / "output").is_dir()
        assert not (workspace / "solution").exists()
        assert not (workspace / "tests").exists()


def check_quick_mode_runs_host_cli_and_local_verifier() -> None:
    class TTYCapture(io.StringIO):
        def isatty(self) -> bool:
            return True

    with tempfile.TemporaryDirectory(prefix="honeydew-quick-run-") as raw:
        root = Path(raw)
        task = make_quick_task(root)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake_claude = bin_dir / "claude"
        fake_claude.write_text(
            "#!/bin/sh\n"
            "mkdir -p \"$PWD/output\"\n"
            "printf '%s\\n' '{\"ok\": true}' > \"$PWD/output/result.json\"\n"
            "sleep 0.3\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "if not args or args[0] in {'build', 'rm', 'image'}:\n"
            "    raise SystemExit(0)\n"
            "if args[0] == 'run':\n"
            "    output = logs = ''\n"
            "    for index, value in enumerate(args[:-1]):\n"
            "        if value != '-v':\n"
            "            continue\n"
            "        source, target = args[index + 1].rsplit(':', 1)\n"
            "        if target == '/workspace/output': output = source\n"
            "        if target == '/logs': logs = source\n"
            "    os.makedirs(os.path.join(logs, 'verifier'), exist_ok=True)\n"
            "    reward = '1\\n' if os.path.isfile(os.path.join(output, 'result.json')) else '0\\n'\n"
            "    with open(os.path.join(logs, 'verifier', 'reward.txt'), 'w') as handle: handle.write(reward)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        fake_claude.chmod(0o755)
        fake_docker.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        jobs_dir = root / "harbor-jobs"
        trajectories_dir = root / "trajectories"
        output = TTYCapture()
        try:
            with contextlib.redirect_stdout(output):
                returncode = harbor_runner.main(
                    [
                        str(task),
                        "--quick",
                        "--quick-agent",
                        "claude",
                        "--quick-timeout-sec",
                        "30",
                        "--progress-interval-sec",
                        "0.1",
                        "--run-id",
                        "quick-fixture",
                        "--jobs-dir",
                        str(jobs_dir),
                        "--completed-trajectories-dir",
                        str(trajectories_dir),
                    ]
                )
        finally:
            os.environ["PATH"] = old_path

        assert returncode == 0
        rendered = output.getvalue()
        assert "Agent running..." in rendered
        assert rendered.count("Agent running...") >= 2
        assert "\r\033[2K" in rendered
        assert not list(jobs_dir.glob("*.modal-run.json"))
        job_dir = jobs_dir / "quick-fixture-quick-claude"
        trial_dir = job_dir / "quick-fixture.agent__claude"
        result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
        assert result["finished_at"]
        assert result["verifier_result"]["rewards"]["reward"] == 1.0
        assert result["timing"]["agent_elapsed_sec"] >= 0
        assert result["timing"]["verifier_elapsed_sec"] >= 0
        assert result["timing"]["total_elapsed_sec"] >= result["timing"]["agent_elapsed_sec"]
        assert (trial_dir / "verifier" / "reward.txt").read_text(encoding="utf-8").strip() == "1"
        assert (trial_dir / "artifacts" / "output" / "result.json").is_file()
        assert (trajectories_dir / "quick" / "quick-fixture" / "claude").is_dir()


def check_remote_summary_merges_models_of_one_agent() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-runner-test-") as raw:
        destination = Path(raw)
        harbor_runner.write_remote_results_summary(
            destination,
            "hr_123",
            "example-task",
            "COMPLETE",
            {
                "oracle": {"verdict": "PASS", "reward": 1},
                "trials": [
                    {"agent_id": "codex-gpt-5-6-sol", "verdict": "PASS", "reward": 1},
                    {"agent_id": "codex-gpt-6-astra", "verdict": "FAIL", "reward": 0},
                    {"agent_id": "codex-gpt-7-unknown", "verdict": "FAIL", "reward": 0},
                ],
            },
            ("claude-opus", "codex-gpt-6-astra", "gemini-3-8-flash"),
        )
        header = (destination / "summary.md").read_text(encoding="utf-8").splitlines()[2]
        assert header == "| Task | Oracle | claude-code | codex | antigravity | Status |", header
        row = (destination / "summary.md").read_text(encoding="utf-8").splitlines()[4]
        assert "| 1/3 pass," in row, row


if __name__ == "__main__":
    check_run_identity()
    check_names_are_valid_and_unique()
    check_cleanup_is_targeted()
    check_jobs_dir_is_cleared_for_new_runs()
    check_snapshot_results_map_to_original_task()
    check_successful_archive_uses_agent_names_and_oracle()
    check_remote_archive_promotion_uses_direct_layout()
    check_remote_partial_archive_matches_local_layout()
    check_remote_oracle_exception_evidence_is_preserved()
    check_remote_error_without_agent_trials_keeps_compact_evidence()
    check_remote_archive_extracts_evidence_without_task_files()
    check_remote_archive_refuses_non_trajectory_manifest()
    check_remote_archive_flag_matches_local_behavior()
    check_remote_start_error_preserves_service_message()
    check_remote_oracle_exception_downloads_archive()
    check_remote_agent_exception_replaces_direct_archive()
    check_remote_service_error_skips_archive_download()
    check_sigterm_enters_cleanup_path()
    check_smoke_mode_wiring()
    check_public_network_snapshots()
    check_oracle_spinner()
    check_agent_progress_order()
    check_remote_defaults_load_dotenv()
    check_remote_bundle_wiring()
    check_remote_policy_wiring()
    check_default_remote_trial_count()
    check_remote_progress_reporting()
    check_remote_summary_merges_models_of_one_agent()
    check_remote_poll_retries_network_error()
    check_remote_request_retries_network_error()
    check_remote_archive_retries_network_error()
    check_remote_upload_retries_network_error()
    check_remote_upload_progress()
    check_quick_agent_resolution_and_commands()
    check_quick_workspace_is_public_only()
    check_quick_mode_runs_host_cli_and_local_verifier()
    print("Harbor runner isolation checks passed")
