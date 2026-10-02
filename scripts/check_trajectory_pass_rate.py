#!/usr/bin/env python3
"""Validate the per-agent difficulty gate and report advisory agent pass rates."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path


AGENTS = ("claude", "codex", "gemini")
AGENT_DIRECTORIES = {
    "claude": ("claude-code", "claude"),
    "codex": ("codex",),
    # The Gemini slot runs through the Antigravity CLI; older runs used gemini-cli.
    "gemini": ("antigravity", "gemini-cli", "gemini"),
}
# Substrings that identify an agent in a summary column header or label.
AGENT_ALIASES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "gemini": ("gemini", "antigravity"),
}
DISPLAY_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
}
COUNT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")
# The hard difficulty gate: each agent must fail at least this many trials of
# the four it runs. Passing more than that means the task is too easy to submit.
REQUIRED_FAILURES = 2


@dataclass(frozen=True)
class Counts:
    passed: int
    trials: int


class PassRateError(Exception):
    """A malformed or inconsistent trajectory pass-rate input."""


def table_cells(line: str) -> list[str] | None:
    if "|" not in line:
        return None
    return [part.strip() for part in line.strip().strip("|").split("|")]


def classify(value: object) -> str | None:
    text = str(value).lower()
    if "oracle" in text:
        return "oracle"
    for agent in AGENTS:
        if any(alias in text for alias in AGENT_ALIASES[agent]):
            return agent
    return None


def parse_count(value: str, source: str, allow_empty: bool = False) -> Counts:
    match = COUNT_RE.match(value)
    if match is None:
        raise PassRateError(f"{source} has an invalid pass count: {value!r}")
    passed, trials = (int(match.group(index)) for index in (1, 2))
    if trials < (0 if allow_empty else 1) or passed > trials:
        raise PassRateError(f"{source} has an invalid pass count: {value!r}")
    return Counts(passed, trials)


def parse_markdown_summary(path: Path) -> dict[str, Counts]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PassRateError(f"could not read trajectory summary {path}: {exc}") from exc

    lines = text.splitlines()
    header_index: int | None = None
    # One agent may have several columns, one per model (for example two Codex
    # models after a model rollover); their counts are summed.
    columns: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        cells = table_cells(line)
        if cells is None or len(cells) < 2:
            continue
        if cells[0].lower() != "task" or cells[1].lower() != "oracle":
            continue
        header_index = index
        for column, cell in enumerate(cells[2:], 2):
            agent = classify(cell)
            if agent in AGENTS:
                columns.setdefault(agent, []).append(column)
        break

    if header_index is None:
        raise PassRateError("summary.md is missing the Task/Oracle result table")
    missing = [agent for agent in AGENTS if agent not in columns]
    if missing:
        raise PassRateError(
            "summary.md is missing agent columns: " + ", ".join(missing)
        )

    totals = {agent: [0, 0] for agent in AGENTS}
    rows = 0
    for line in lines[header_index + 1 :]:
        cells = table_cells(line)
        if cells is None or len(cells) <= max(max(found) for found in columns.values()):
            continue
        task_cell = cells[0].strip()
        oracle_cell = cells[1].strip()
        if not task_cell or task_cell == "---" or not oracle_cell:
            continue
        rows += 1
        for agent, agent_columns in columns.items():
            for column in agent_columns:
                counts = parse_count(
                    cells[column],
                    f"summary {agent} pass count for task {task_cell!r}",
                    allow_empty=len(agent_columns) > 1,
                )
                totals[agent][0] += counts.passed
                totals[agent][1] += counts.trials

    if not rows or any(trials == 0 for _, trials in totals.values()):
        raise PassRateError("summary.md does not contain complete agent pass counts")
    return {
        agent: Counts(passed, trials)
        for agent, (passed, trials) in totals.items()
    }


def json_object(path: Path, description: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PassRateError(f"could not read {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PassRateError(f"{description} is not a JSON object: {path}")
    return value


def reward_from_result(result: dict[str, object]) -> float | None:
    verifier_result = result.get("verifier_result")
    if not isinstance(verifier_result, dict):
        return None
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, dict):
        return None
    reward = rewards.get("reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        return None
    return float(reward)


def reward_from_file(path: Path) -> float:
    try:
        value = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise PassRateError(f"could not read trajectory reward {path}: {exc}") from exc
    if not math.isfinite(value):
        raise PassRateError(f"trajectory reward is not finite: {path}")
    return value


def direct_agent_dir(trajectories_dir: Path, agent: str) -> Path:
    candidates = [
        trajectories_dir / name
        for name in AGENT_DIRECTORIES[agent]
        if (trajectories_dir / name).is_dir()
    ]
    if not candidates:
        raise PassRateError(
            f"trajectories/ is missing the {DISPLAY_NAMES[agent]} agent directory"
        )
    if len(candidates) > 1:
        raise PassRateError(
            f"trajectories/ contains multiple directories for {DISPLAY_NAMES[agent]}: "
            + ", ".join(str(path) for path in candidates)
        )
    return candidates[0]


def direct_trial_counts(trajectories_dir: Path) -> dict[str, Counts]:
    counts: dict[str, Counts] = {}
    for agent in AGENTS:
        agent_dir = direct_agent_dir(trajectories_dir, agent)
        trial_dirs = [
            path
            for path in sorted(agent_dir.iterdir())
            if path.is_dir()
            and (
                (path / "result.json").is_file()
                or (path / "verifier/reward.txt").is_file()
            )
        ]
        if not trial_dirs:
            raise PassRateError(
                f"trajectories/{agent_dir.name}/ does not contain any trial evidence"
            )

        passed = 0
        for trial_dir in trial_dirs:
            result_path = trial_dir / "result.json"
            reward_path = trial_dir / "verifier/reward.txt"
            result: dict[str, object] | None = None
            if result_path.is_file():
                try:
                    result = json_object(result_path, "trajectory result")
                except PassRateError:
                    if not reward_path.is_file():
                        raise

            if result is not None and not result.get("finished_at"):
                raise PassRateError(
                    f"trajectory trial is unfinished: {trial_dir}"
                )

            reward = reward_from_result(result) if result is not None else None
            if reward is None:
                if not reward_path.is_file():
                    raise PassRateError(
                        f"trajectory trial is missing a verifier reward: {trial_dir}"
                    )
                reward = reward_from_file(reward_path)
            if reward > 0:
                passed += 1
        counts[agent] = Counts(passed, len(trial_dirs))
    return counts


def validate_trajectory_reference(trajectories_dir: Path, reference: object) -> None:
    if not isinstance(reference, str) or not reference.strip():
        raise PassRateError("trajectory summary contains an invalid trajectory_ref")
    relative = reference.split("#", 1)[-1]
    if relative.startswith("trajectories/"):
        relative = relative.removeprefix("trajectories/")
    if not relative or relative.startswith("/"):
        raise PassRateError(
            "trajectory summary contains an unsafe trajectory_ref: "
            f"{reference!r}"
        )
    root = trajectories_dir.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PassRateError(
            f"trajectory summary contains an out-of-tree trajectory_ref: {reference!r}"
        ) from exc
    if not candidate.is_dir():
        raise PassRateError(
            f"trajectory summary references missing trajectory evidence: {candidate}"
        )


def summary_json_counts(path: Path, trajectories_dir: Path) -> dict[str, Counts]:
    summary = json_object(path, "trajectory summary")
    trials = summary.get("trials")
    if not isinstance(trials, list) or not trials:
        raise PassRateError("trajectories/summary.json does not contain agent trials")

    totals = {agent: [0, 0] for agent in AGENTS}
    for index, trial in enumerate(trials, 1):
        if not isinstance(trial, dict):
            raise PassRateError(f"trajectory summary trial {index} is not an object")
        phase = str(trial.get("phase") or "agent").lower()
        agent = classify(trial.get("agent_id") or trial.get("agent") or "")
        if phase == "oracle" or agent == "oracle":
            continue
        if agent not in AGENTS:
            raise PassRateError(
                f"trajectory summary trial {index} has an unknown agent: "
                f"{trial.get('agent_id')!r}"
            )
        if not trial.get("finished_at"):
            raise PassRateError(f"trajectory summary trial {index} is unfinished")
        verdict = str(trial.get("verdict") or "").upper()
        if verdict not in {"PASS", "FAIL", "EXCEPTION"}:
            raise PassRateError(
                f"trajectory summary trial {index} has an incomplete verdict: {verdict!r}"
            )
        reward = trial.get("reward")
        if not isinstance(reward, (int, float)) or isinstance(reward, bool):
            raise PassRateError(
                f"trajectory summary trial {index} is missing a numeric reward"
            )
        if isinstance(trial.get("trajectory_ref"), str):
            validate_trajectory_reference(trajectories_dir, trial["trajectory_ref"])
        totals[agent][0] += int(float(reward) > 0)
        totals[agent][1] += 1

    missing = [agent for agent in AGENTS if totals[agent][1] == 0]
    if missing:
        raise PassRateError(
            "trajectories/summary.json is missing agent trials: " + ", ".join(missing)
        )
    return {
        agent: Counts(passed, trials)
        for agent, (passed, trials) in totals.items()
    }


def counts_mismatch(
    left: dict[str, Counts], right: dict[str, Counts], left_name: str, right_name: str
) -> str | None:
    mismatches = [
        f"{agent}: {left_name} {format_counts(left[agent])}, "
        f"{right_name} {format_counts(right[agent])}"
        for agent in AGENTS
        if left[agent] != right[agent]
    ]
    return "; ".join(mismatches) if mismatches else None


def collect_trajectory_counts(trajectories_dir: Path) -> dict[str, Counts]:
    if not trajectories_dir.is_dir():
        raise PassRateError(f"trajectories directory is missing: {trajectories_dir}")

    summary_json = trajectories_dir / "summary.json"
    summary_md = trajectories_dir / "summary.md"
    if summary_json.is_file():
        summary_counts = summary_json_counts(summary_json, trajectories_dir)
        has_agent_directory = any(
            (trajectories_dir / directory).is_dir()
            for directories in AGENT_DIRECTORIES.values()
            for directory in directories
        )
        counts = (
            direct_trial_counts(trajectories_dir)
            if has_agent_directory
            else summary_counts
        )
        if counts != summary_counts:
            mismatch = counts_mismatch(
                counts, summary_counts, "trajectory files", "summary.json"
            )
            raise PassRateError(
                "trajectory evidence disagrees with summary.json: "
                + (mismatch or "unknown mismatch")
            )
        if summary_md.is_file():
            markdown_counts = parse_markdown_summary(summary_md)
            mismatch = counts_mismatch(
                counts, markdown_counts, "summary.json", "summary.md"
            )
            if mismatch:
                raise PassRateError(
                    "trajectory summaries disagree: " + mismatch
                )
        return counts

    direct_counts: dict[str, Counts] | None = None
    has_agent_directory = any(
        (trajectories_dir / directory).is_dir()
        for directories in AGENT_DIRECTORIES.values()
        for directory in directories
    )
    if has_agent_directory:
        direct_counts = direct_trial_counts(trajectories_dir)

    if summary_md.is_file():
        markdown_counts = parse_markdown_summary(summary_md)
        if direct_counts is not None:
            mismatch = counts_mismatch(
                direct_counts, markdown_counts, "trajectory files", "summary.md"
            )
            if mismatch:
                raise PassRateError(
                    "trajectory evidence disagrees with summary.md: " + mismatch
                )
        return markdown_counts

    if direct_counts is not None:
        return direct_counts
    raise PassRateError(
        "trajectories/ must contain summary.json, summary.md, or direct agent trial evidence"
    )


def format_counts(counts: Counts) -> str:
    return f"{counts.passed}/{counts.trials}"


def failures(counts: Counts) -> int:
    return counts.trials - counts.passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trajectories",
        "--trajectory-root",
        dest="trajectories",
        type=Path,
        required=True,
        help="archived trajectory directory (default layout: ./trajectories)",
    )
    args = parser.parse_args()

    try:
        counts = collect_trajectory_counts(args.trajectories)
    except PassRateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rates = [counts[agent].passed / counts[agent].trials for agent in AGENTS]
    average = sum(rates) / len(rates)
    rendered = ", ".join(
        f"{DISPLAY_NAMES[agent]} {format_counts(counts[agent])} "
        f"({counts[agent].passed / counts[agent].trials * 100:.1f}%)"
        for agent in AGENTS
    )

    short = [
        (agent, failures(counts[agent]))
        for agent in AGENTS
        if failures(counts[agent]) < REQUIRED_FAILURES
    ]
    if short:
        detail = "; ".join(
            f"{DISPLAY_NAMES[agent]} failed {count}/{counts[agent].trials} trial(s)"
            for agent, count in short
        )
        print(
            "ERROR: HARD SUBMISSION FAILURE: every agent must fail at least "
            f"{REQUIRED_FAILURES} trials, but {detail}. The task is too easy "
            "for the benchmark; do not submit it. Increase the genuine "
            "scientific difficulty and rerun all three agents. The overall "
            "average pass-rate check is advisory only (trajectories report "
            f"{average * 100:.1f}%: {rendered}).",
            file=sys.stderr,
        )
        return 1

    if average >= 0.5:
        print(
            "WARNING: task may be too easy: the overall Claude/Codex/Gemini "
            "pass rate is "
            f"{average * 100:.1f}% (the < 50% target is advisory only; every "
            f"agent met the {REQUIRED_FAILURES}-failure gate: {rendered}).",
            file=sys.stderr,
        )

    print(
        "Trajectory pass-rate check (archived trajectories): "
        f"{rendered}; average {average * 100:.1f}%; every agent failed at "
        f"least {REQUIRED_FAILURES} trials."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
