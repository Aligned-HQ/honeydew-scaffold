#!/usr/bin/env python3
"""Regression checks for trajectory-based, non-blocking submission packaging."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def make_fixture(root: Path, rewards: dict[str, list[int]]) -> None:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("package-submission.sh", "check_trajectory_pass_rate.py"):
        shutil.copy2(SCRIPT_DIR / name, scripts / name)
    (scripts / "package-submission.sh").chmod(0o755)

    (root / "task").mkdir()
    (root / "skill-reports").mkdir()
    trajectories = root / "trajectories"
    trajectories.mkdir()
    (trajectories / "README.md").write_text("trajectory fixture\n", encoding="utf-8")

    trials: list[dict[str, object]] = []
    for agent, agent_rewards in rewards.items():
        agent_dir = trajectories / agent
        agent_dir.mkdir()
        for attempt, reward in enumerate(agent_rewards, 1):
            trial_name = f"trial-{attempt}"
            trial_dir = agent_dir / trial_name
            (trial_dir / "verifier").mkdir(parents=True)
            (trial_dir / "verifier/reward.txt").write_text(
                f"{reward}\n", encoding="utf-8"
            )
            trials.append(
                {
                    "trial_id": f"{agent}-{attempt}",
                    "phase": "agent",
                    "agent_id": agent,
                    "verdict": "PASS" if reward else "FAIL",
                    "reward": reward,
                    "finished_at": "2026-07-24T00:00:00Z",
                    "trajectory_ref": f"fixture#trajectories/{agent}/{trial_name}",
                }
            )

    (trajectories / "summary.json").write_text(
        json.dumps({"trials": trials}, indent=2) + "\n", encoding="utf-8"
    )


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(root / "scripts/check_trajectory_pass_rate.py"),
            "--trajectories",
            str(root / "trajectories"),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def check_checker_uses_trajectories() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-trajectory-check-") as raw:
        root = Path(raw)
        make_fixture(
            root,
            {
                "claude-code": [0, 0, 0, 0],
                "codex": [0, 0, 0, 0],
                "antigravity": [0, 0, 0, 0],
            },
        )
        result = run_checker(root)
        assert result.returncode == 0, result.stderr
        assert "archived trajectories" in result.stdout
        assert "every agent failed at least 2 trials" in result.stdout


def check_high_average_is_advisory_at_the_failure_gate() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-pass-rate-warning-") as raw:
        root = Path(raw)
        make_fixture(
            root,
            {
                "claude-code": [1, 1, 0, 0],
                "codex": [1, 1, 0, 0],
                "antigravity": [1, 1, 0, 0],
            },
        )
        result = run_checker(root)
        assert result.returncode == 0, result.stderr
        assert "WARNING: task may be too easy" in result.stderr
        assert "advisory only" in result.stderr
        assert "every agent met the 2-failure gate" in result.stderr


def check_packaging_reports_hard_failure_but_continues() -> None:
    with tempfile.TemporaryDirectory(prefix="honeydew-package-submission-") as raw:
        root = Path(raw)
        make_fixture(
            root,
            {
                "claude-code": [1, 1, 1, 0],
                "codex": [0, 1, 1, 0],
                "antigravity": [1, 1, 1, 1],
            },
        )
        result = subprocess.run(
            [str(root / "scripts/package-submission.sh")],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "\033[31mHARD FAILURE: do not submit this task" in result.stderr
        assert "Criteria details:" in result.stderr
        assert "HARD SUBMISSION FAILURE" in result.stderr
        assert "every agent must fail at least 2 trials" in result.stderr
        assert "Claude failed 1/4 trial(s)" in result.stderr
        assert "Gemini failed 0/4 trial(s)" in result.stderr
        # Codex cleared the gate, so it must not be named as a shortfall.
        assert "Codex failed" not in result.stderr
        assert "Claude 3/4" in result.stderr
        assert (root / "submission/task").is_dir()
        assert (root / "submission/trajectories").is_dir()
        assert (root / "submission/skill-reports").is_dir()
        assert not (root / "harbor-jobs").exists()


def check_summary_merges_duplicate_model_columns() -> None:
    """Two Codex models (e.g. GPT-5.6-Sol and GPT-6-Astra) share one agent total."""
    with tempfile.TemporaryDirectory(prefix="honeydew-package-submission-") as raw:
        trajectories = Path(raw) / "trajectories"
        trajectories.mkdir()
        (trajectories / "summary.md").write_text(
            "| Task | Oracle | claude-code | codex | codex-gpt-6-astra | antigravity | Status |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| example | pass, reward 1 | 1/4 pass | 0/0 pass | 2/4 pass | 0/4 pass | complete |\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT_DIR / "check_trajectory_pass_rate.py"),
                "--trajectories",
                str(trajectories),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Codex 2/4" in result.stdout, result.stdout


if __name__ == "__main__":
    check_checker_uses_trajectories()
    check_high_average_is_advisory_at_the_failure_gate()
    check_packaging_reports_hard_failure_but_continues()
    check_summary_merges_duplicate_model_columns()
    print("Submission packaging checks passed")
