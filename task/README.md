# Scientific task maintainer notes

This bundle is a runnable smoke task for checking the Harbor task layout and
container wiring. It is intentionally not a publishable benchmark: the
scientific contract is a small descriptive summary and must be adapted to a
real research workflow before an agent campaign.

## Current smoke contract

- `environment/data/input.csv` is a five-row CSV with `sample_id` and numeric
  `value` columns.
- `solution/solve.py` validates the visible CSV and writes the population
  summary to `/workspace/output/result.json`.
- `tests/test_outputs.py` independently recomputes the summary from the
  verifier copy at `tests/data/input.csv`, checks the exact key set, and checks
  finite numeric values.
- The runtime solution uses only the Python standard library. The runtime image
  also includes the pinned `pytest==8.4.1` and `pytest-json-ctrf==0.3.5`
  dependency set from the Linux/amd64 wheelhouse in `tests/wheels/` without
  contacting an index. The optional `tests/Dockerfile` is only a local
  two-image fixture and is not used by submission.

The two CSV files are deliberately identical public fixtures for this smoke
task. A finished task should document the provenance and transformation of its
public inputs here, and keep any verifier-only reference material under
`tests/data/`.

## Container contract

The runtime Dockerfile builds explicitly for Linux/amd64, copies only
`environment/data/`, creates the non-root `agent` user, and has no networked
package or agent-bootstrap step. The optional verifier Dockerfile is retained
only for local two-image tests; it copies its existing test files and data,
installs its vendored wheels with `--no-index --find-links`, and creates the
verifier log and output directories.
The task metadata sets `network_mode = "public"`, the client default for the
Oracle and the agent trials, so both can query scientific databases and tools
over HTTP. The image still vendors every Python dependency: the network is for
the science, not for installing the toolchain.

The environment image also installs the verifier's wheels. The Nexus sandbox
runs a single container and ignores `[verifier].environment_mode`, so anything
`tests/test.sh` imports must already exist in the environment image; the
verifier Dockerfile is only used for local two-image runs.

## Maintainer notes for a real task

When adapting this bundle, preserve the canonical paths while documenting the
actual workflow, data provenance, dependency rationale, scientific method
choices, and tolerance calibration. Keep the agent contract in `instruction.md`,
the reference workflow in `solution/`, and independent machine-checkable
assertions in `tests/`. Do not copy the solution or hidden verifier material
into the runtime image.

## Local commands

From the repository root:

```bash
./harbor_runner.py task --no-remote --smoke-test
```

For a fast authoring check with an already-installed host agent, run one local
trial without Workbench or Modal:

```bash
./harbor_runner.py task --quick --quick-agent codex   # or claude
```

Quick evidence is kept under `harbor-jobs/` and
`trajectories/quick/<run-id>/`; it is separate from the three-agent campaign
archive.

To verify the vendored verifier dependencies without an index:

```bash
python3 .agents/skills/task-fixer/scripts/vendor_offline_dependencies.py \
  --task task --out task/tests/wheels --verify
```

For a Harbor task, use the task directory rather than the project wrapper:

```bash
./harbor_runner.py task --no-remote --dry-run
```

After a successful multi-agent run, the vendored runner writes its Oracle,
agent trajectories, and combined summary directly under the project-level
`trajectories/` folder. Run the trajectory review described in the root
`README.md` against that folder.
