---
name: task-fixer
description: Prepare a Harbor task folder to run its Oracle by repairing metadata,
  offline Docker configuration, required build-support files, executable
  entrypoints, and canonical paths without editing the instruction, solution, or
  verifier logic. Use when a task needs to be made Oracle-ready or its
  environment and metadata need to be repaired.
---

# Task Fixer

Prepare one Harbor task to run its Oracle. The Oracle-ready state means that the
task metadata, build contexts, runtime image configuration, local inputs, and
paths are internally consistent. It does not mean that the Oracle has passed;
the Oracle is run by the task runner after this skill finishes.

## Scope

The task-fixer may edit only task metadata and environment/build-support files:

- `task.toml` and task-local Docker/build metadata;
- `environment/Dockerfile` and support files used by that Dockerfile;
- an existing task-local `README.md` when factual reviewer notes need repair;
  the file is optional and must not be created solely for scaffold compliance;
- `tests/data/` and, only when explicitly requested for local two-image testing,
  an existing `tests/Dockerfile`; the verifier implementation remains read-only;
- task-local runtime input data that must be vendored for an offline build.

Read `instruction.md`, the solution directory, and the verifier directory to
discover their contracts and dependencies, but never edit them. In particular,
never edit `instruction.md`, `solution/solve.py` (or any other solution file),
`tests/test.sh`, verifier Python files, test thresholds, fixtures, or expected
outputs. The content of existing solution and verifier entrypoints is
read-only, but changing the executable bit on an existing required shell
entrypoint is allowed. Do not create placeholder solution or test
implementations. If a required repair would need scientific or verifier content
changed, report the blocker for the task author.

## Deployment constraints (Google Nexus sandbox)

Submitted tasks are validated by Google's sandboxed execution service, not by
the open-source Harbor runtime. Most `task.toml` parameters are ignored there.
Treat every constraint below as an Oracle-readiness gate, not as a reason to
weaken the task contract.

**Task name namespace.** `[task].name` must use the `org/name` form, for
example `independent/lpxh-screen-triage`. Harbor reads a bare name such as
`lpxh-screen-triage` as a dataset path, finds no valid child tasks under it, and
aborts the run before the Oracle trial starts with
`ValueError: Either datasets or tasks must be provided.` Nothing downstream
runs: the Oracle records `EXCEPTION` with no reward, every agent trial stays
queued, and the private Oracle artifact holds only the Harbor traceback. The
exception is non-retryable, so a fresh bundle must be uploaded rather than the
run retried. Keep the task's existing name as the segment after the slash and
add the missing namespace; do not rename the task to work around the error.

**Network.** Set `[environment].network_mode = "public"`, which is what the
submission sandbox reads. Public network is the client policy for both the
Oracle and the agent trials: it lets the reference solution and the agent query
scientific databases and tools over HTTP. It is not a licence to fetch the
toolchain. Python libraries and code stay vendored in the image, and no build
or run step may install a package, so keep existing HTTP calls to scientific
services and remove or replace run-time installs.

**One container.** The sandbox runs a single container and ignores
`[verifier].environment_mode`; it never builds `tests/Dockerfile`. Everything
`tests/test.sh` imports or invokes must be installed in the runtime image, and
nothing may depend on a builder stage in `tests/Dockerfile`. Docker Compose,
multi-service topologies, and multi-step evaluation are unsupported.

**Layout the sandbox expects.** The submitted archive is extracted with
`environment/` → `/app/`, `tests/` → `/tests/`, and `solution/` → `/solution/`;
`/app/verify` is symlinked to `/tests/`. `solution/solve.sh` runs with the
working directory set to `/app` and must be executable, as must
`tests/test.sh`. The extractor searches at most two levels deep, so keep
nesting shallow. Because extraction is additive to the image, anything the task
reads through `WORKSPACE_DIR`/`DATA_DIR` must already be baked into the image —
do not assume the archive provides it.

**Fail → pass transition.** Validation runs `tests/test.sh` on the unmodified
environment, where it must fail, then runs `solution/solve.sh`, after which it
must pass. A verifier that passes before the solution is applied fails
validation as trivially solvable. A verifier that cannot fail — because it
skips when outputs are missing, defaults to a passing reward, or asserts
nothing — is the same defect.

**Idempotent verifier.** `test.sh` may be executed several times. It must reset
or tolerate any state it creates so repeated runs give the same verdict.

**Reward signal.** `test.sh` must write the reward before it exits.
`/logs/tests/reward.txt` and `/logs/verifier/reward.txt` are symlinked, so
either path works. Priority is `reward.txt` (contains `1`), then `reward.json`
(field `reward >= 1.0`), then the exit code. Prefer `reward.txt`.

**No runtime installs.** Pre-validation scans the archive for install commands
and rejects the task before it runs if `test.sh` or `solve.sh` contain
`pip install`, `apt-get install`, `curl ... | sh`, or similar. Every dependency
must be baked into the image at build time.

**Never stage anything under `/tmp` at build time.** The sandbox overlays
`/tmp` with a clean, empty tmpfs at startup, erasing whatever the image put
there. Put wheelhouses, caches, and vendored dependencies under `/opt/` or
`/app/` instead. This is one of the most common causes of "tests failed after
applying golden solution".

**Image.** Build and inspect single-platform `linux/amd64` images. Manifest
lists and multi-architecture indexes fail the pull with `MANIFEST_UNKNOWN`; the
submitted image must be pinned by SHA256 digest, not a tag such as `:latest`.
Keep the final image no larger than 2 GB (`2,000,000,000` bytes). Custom
`ENTRYPOINT`/`CMD` directives are ignored by the validation pipeline.

**Ignored `task.toml` parameters.** Do not spend repairs on, or claim
correctness from, values the sandbox discards: `verifier.timeout_sec` (a fixed
1-hour system timeout applies), `environment.docker_image` (the image comes
from the API's `containerImageUri`), `environment.cpus`, `environment.gpu*`,
`environment.tpu*`, `environment.healthcheck*`, `environment.mcp_servers*`, and
`verifier.environment_mode`. `verifier.env` and `environment.env` are honored,
but a template without a default such as `${VAR_NAME}` is rejected; use
`${VAR_NAME:-default}`.

**Archive hygiene.** macOS resource-fork files (`._*`) in the archive cause a
`UnicodeDecodeError` during ingestion. Keep them, `.DS_Store`, caches, and
credentials out of the task.

Do not modify Harbor, the runner, or an agent bootstrap to work around these
constraints. An agent-installation limitation is outside this Oracle-only skill
and must be reported separately.

## Docker access and offline dependency bundles

This skill cannot grant itself access to a host Docker daemon. When it is run
through `scripts/run-task-fixer.sh` with Codex, the wrapper's default
`--docker-access auto` mode uses Codex's `danger-full-access` sandbox so the
skill can reach an already configured local Docker socket or context. An
author can make that explicit with:

```bash
./scripts/run-task-fixer.sh task --docker-access on
```

Use `--docker-access off` for a static-only run. Full access is a broad local
permission and is intended only for a trusted task checkout. It does not fix
the daemon, change socket permissions, or authorize an unapproved remote
context. If Docker is still denied, inspect the configured contexts and report
the exact host error while completing all static repairs.

Do not put binary packages in this Markdown skill file. The mirrored helper
`scripts/vendor_offline_dependencies.py` is the reusable vendoring mechanism.
Run it on the approved authoring machine or package mirror, not inside a task
container, so the image carries every library and no build or run step depends
on a package index. First derive the
actual imports and versions from the existing task and keep runtime and
verifier wheelhouses separate when their dependencies differ. For the common
Python 3.12 dependencies in this scaffold, examples are:

```bash
python3 .agents/skills/task-fixer/scripts/vendor_offline_dependencies.py \
  --task task --out task/environment/wheels \
  numpy==1.26.4 pandas==2.2.2

python3 .agents/skills/task-fixer/scripts/vendor_offline_dependencies.py \
  --task task --out task/tests/wheels \
  pytest==8.4.1
```

The helper downloads transitive binary wheels for Linux/amd64, writes a
pinned `requirements.txt`, and records hashes in
`wheelhouse-manifest.json`. Verify the bundle without contacting an index:

```bash
python3 .agents/skills/task-fixer/scripts/vendor_offline_dependencies.py \
  --task task --out task/environment/wheels --verify
```

Use an approved local index or pip configuration for the authoring-time
download. In each Dockerfile, copy the appropriate wheelhouse and install it
with `python -m pip install --no-cache-dir --no-index --find-links=/opt/wheels -r /opt/wheels/requirements.txt`; never put the install in `tests/test.sh` or
runtime execution. Prefer a builder stage when the wheelhouse is large so it
does not remain in the final image, then rebuild and measure the image.
Always include the wheelhouse in the task submission and rerun the helper's
`--verify` mode plus the strict scaffold checks after changing it.

If an approved wheel directory already exists on the authoring host, use it
without an index:

```bash
python3 .agents/skills/task-fixer/scripts/vendor_offline_dependencies.py \
  --task task --out task/environment/wheels \
  --find-links /approved/linux-amd64-wheels --no-index \
  numpy==1.26.4 pandas==2.2.2
```

## Required inputs and allowed changes

For the scaffold's submission layout, the target is the literal `task/`
directory. It is a fixed outer wrapper, not the task's logical name. Confirm the
task root before editing and work only inside it. A normal task contains:

- `task.toml`;
- `instruction.md`;
- `environment/Dockerfile`;
- the solution entrypoint and implementation named by the task (commonly
  `solution/solve.sh` and `solution/solve.py`);
- `tests/test.sh`, the verifier files it references, and any required test data;

The exact solution language and optional data files may vary. Repair missing
build-support files when their contents can be derived from the existing task:

- Do not create `task/README.md` solely to satisfy a layout check. If an
  existing task-local README is present, keep any reviewer notes factual and
  derived from the task metadata, data provenance, dependencies, and observed
  workflow. Do not put reviewer notes in `instruction.md`, and do not fill the
  README with generic TODOs or invented scientific claims.
- Do not create `tests/Dockerfile` for submission compliance. The submission
  sandbox runs the verifier in the runtime image. If an existing
  `tests/Dockerfile` is intentionally maintained for local two-image testing,
  keep it consistent with the actual test imports, referenced files, canonical
  variables, and approved local wheelhouse; it is optional and never the only
  place a verifier dependency may be installed.
- Create required `environment/data/` or `tests/data/` directories. Use a
  `.gitkeep` only for an intentionally empty directory; copy actual referenced
  fixtures when the verifier needs them. Never fabricate reference data.
- Set the executable bit on existing `solution/solve.sh`, `tests/test.sh`, and
  other existing task entrypoint shell scripts when required. Do not rewrite
  their contents.

If a required scientific input, solution/verifier implementation, or dependency
cannot be derived or supplied from approved local resources, return `FAIL` with
the exact missing path and remedy instead of inventing it.

Keep the filesystem path and the Harbor identity separate:

- Keep the outer directory named `task/`; the submission package and the skill
  report target depend on that exact path.
- Read and, when an in-scope metadata repair is needed, edit the logical name
  in `[task].name` in `task/task.toml`. The namespace rule applies to that
  value, not to the directory basename.
- If an author supplied a task-specific directory name, do not rename it as a
  metadata fix and do not make the final report target that directory. Restore
  the content under `task/` before the final fixer and review runs.

During the audit, record both the target path and `[task].name`. Normalize the
latter only when required by the deployment policy; never change the former to
satisfy a rubric phrase such as "meaningful task name."

## Workflow

1. **Audit the task read-only.**

   Read `task.toml`, `instruction.md`, the solution entrypoint and imports, the
   verifier entrypoint and imports, and every Dockerfile. Inventory:

   - the `[task].name` value and whether it carries an `org/` namespace;
   - the Harbor environment mode, runtime user, timeouts, resource settings, and
     declared artifacts;
   - input files and CLIs/packages needed by the solution and verifier;
   - output paths and environment variables used by the existing code;
   - executable lookup behavior under Harbor/Modal: whether the existing
     entrypoints invoke bare `python`, `python3`, `pytest`, or other CLIs, and
     whether dependencies are available only through a virtualenv or a
     Dockerfile `PATH` setting;
   - Docker build contexts, `COPY` sources, entrypoints, and required users;
   - network calls, host-specific paths, and files referenced but not present.

   This is a consistency audit only. Do not rewrite scientific logic or resolve
   a contract mismatch by changing the prompt or tests.

2. **Normalize task metadata.**

   Edit `task.toml` only as needed to make it valid and Oracle-compatible:

   - give `[task].name` a namespace when it lacks one, for example
     `name = "independent/lpxh-screen-triage"` instead of
     `name = "lpxh-screen-triage"`. Exactly one slash, lowercase, no spaces, and
     the original name preserved as the second segment. Without the namespace
     Harbor never starts the Oracle trial;
   - set `[environment].network_mode = "public"`, the default for the Oracle
     and the agent trials. Preserve the task's scientific contract, including
     its HTTP calls to scientific databases and tools, and do not let the
     public network replace a vendored library;
   - do not rely on `environment_mode`: the submission sandbox ignores it and
     runs one container, so the verifier's dependencies belong in the runtime
     image. An optional `tests/Dockerfile` may support local two-image runs, but
     it is never built at submission time and must not be the only place a
     verifier dependency is installed;
   - declare the files the existing solution produces and the verifier consumes
     in Harbor's supported artifact form, normally
     `artifacts = ["/workspace/output/<file>"]`;
   - keep any `env` value self-contained: `${VAR:-default}` is resolved at
     compile time, while a bare `${VAR}` is rejected;
   - remove host-specific paths such as `/Users/...` and `/Volumes/...` from
     metadata; use task/container paths instead;
   - leave the timeouts consistent with the fixed 1-hour system timeout, and do
     not invent CPU/GPU/healthcheck settings to satisfy a checklist — the
     sandbox fixes resources and ignores those fields;
   - never author the scientific metadata. `difficulty_explanation`,
     `solution_explanation`, `verification_explanation`, the task description,
     and the expert time estimate are the author's own words. Report a missing
     or weak one as a blocker instead of writing it;
   - retain intentional task values and report any value that cannot be inferred
     from the current task rather than inventing a scientific requirement.

   Do not change output names, schemas, tolerances, or required parameters just
   to make the metadata agree with a broken solution or verifier. Report that
   mismatch for the author to resolve.

3. **Correct the Docker environment.**

   Edit only Docker/build configuration and its support files. Make the runtime
   image capable of running the existing solution and Oracle without modifying
   the solution or verifier:

   - use paths relative to each Dockerfile's build context; for example,
     `environment/Dockerfile` should use `COPY data/ ...`, not
     `COPY environment/data/ ...`;
   - define and use canonical variables such as `WORKSPACE_DIR=/workspace`,
     `DATA_DIR=/workspace/data`, `OUTPUT_DIR=/workspace/output`,
     `SOLUTION_DIR=/solution`, `TESTS_DIR=/tests`, and
     `LOG_DIR=/logs/verifier` where the existing task contract needs them;
   - create required directories and give the configured runtime user access to
     them in the final image;
   - make every `FROM` line explicit: `FROM --platform=linux/amd64 ...`;
   - install the verifier's dependencies in the runtime image too. The
     submission sandbox runs the agent and the verifier in one container, so a
     package that exists only in the optional local verifier image passes locally and then
     fails on submission with `No module named pytest`;
   - never leave wheelhouses, caches, or vendored dependencies under `/tmp`.
     The sandbox replaces `/tmp` with an empty tmpfs at startup, so anything
     staged there at build time is gone before the task runs. Use `/opt/` or
     `/app/`;
   - do not depend on `ENTRYPOINT` or `CMD`; the validation pipeline controls
     container execution and ignores them;
   - classify every package install as Oracle/runtime, verifier, or
     agent-bootstrap-only. Remove bootstrap-only `apt-get` blocks when they are
     not needed by the Oracle; do not retain online `curl`, apt, or package
     setup merely for future agent installation;
   - repair required dependencies without network access when possible: first
     use the bundled `scripts/vendor_offline_dependencies.py` to populate a
     task-local wheelhouse from the approved authoring-time package source, then
     use `pip install --no-cache-dir --no-index --find-links=...`; otherwise use an approved
     local base image or approved local `.deb` bundle installed with `dpkg`.
     Copy only the needed bundle into the build context and remove caches
     afterward. Do not add `pip install`, `apt-get`, or other downloads to
     `tests/test.sh` or runtime execution. If the helper cannot obtain a wheel
     for a required package, report the package, target Python/platform, and
     exact approved-source remedy;
   - do not rely solely on Dockerfile `ENV PATH=...` or `VIRTUAL_ENV` for
     dependencies. Harbor/Modal can invoke a script with a minimal `env` map;
     in that case a bare `python3` or `pytest` may resolve to the base system
     interpreter instead of a copied virtualenv. When the existing solution or
     verifier entrypoint uses bare commands (and cannot be edited under this
     skill), install its required packages into the final image's default
     system interpreter from the vendored wheelhouse, for example with a
     final-stage `python -m pip install --no-cache-dir --no-index
     --find-links=/opt/wheels -r /opt/wheels/requirements.txt`, then remove
     the wheelhouse. A virtualenv may remain as an optimization, but it must
     not be the only place the packages exist unless the existing entrypoint
     explicitly selects it;
   - if no approved base or local package bundle can satisfy a required import
     or CLI, leave the task policy unchanged and report the dependency blocker;
   - copy every existing runtime input under `environment/data/` into the image,
     and explicitly copy verifier files and verifier data in separate mode;
   - use `bash /solution/solve.sh` when an executable bit cannot be relied on,
     but do not edit the script itself;
   - avoid absolute host paths, build-time downloads, run-time package
     installs, and hidden answer files. A run-time HTTP call to a scientific
     database or tool is part of the task contract, not a defect.

   If a path in the existing solution, instruction, or verifier is incompatible
   with the canonical container layout and cannot be corrected in metadata or a
   Dockerfile, report it as a blocker. Do not edit the file that contains it.

4. **Vendor only required local inputs.**

   Copy inputs or approved offline packages into task-local data directories when
   they are referenced by the existing task and their source is available. Keep
   runtime inputs in `environment/data/` and verifier-only data in `tests/data/`.
   Do not add reference answers, hidden solution files, caches, credentials, or
   unrelated repository content. If a referenced input is unavailable, report
   its exact expected path instead of fabricating it.

5. **Repair strict scaffold prerequisites.**

   Locate the project root and run its static validator when available, for
   example `python3 scripts/validate_scaffold.py --root <project-root>
   --strict`. Fix every in-scope error before treating the task as blocked:

   - do not create `task/README.md` solely to satisfy scaffold compliance; it is
     optional reviewer context;
   - create or repair `tests/data/` from existing verifier files and referenced
     fixtures; repair an existing `tests/Dockerfile` only when an explicit local
     two-image test requires it;
   - set executable bits on existing solution/verifier shell entrypoints;
   - add `FROM --platform=linux/amd64` to every Dockerfile stage and correct
     context-relative `COPY` paths;
   - remove networked dependency setup when it is replaceable by an approved
     local base or dependency bundle.

   Run the strict validator again after these repairs. A missing implementation,
   missing scientific input, unavailable approved dependency, or error that
   would require changing instruction/solution/test contents remains a blocker.
   Do not create empty files merely to silence a validator.

6. **Check Oracle prerequisites without running the task.**

   Validate the TOML, the `org/name` form of `[task].name`, required file set,
   Dockerfile context paths, `COPY` targets,
   environment-variable wiring, entrypoints, user permissions, artifact paths,
   and offline/network settings. When Docker and approved cached dependencies are
   available, build the affected images with:

   ```bash
   docker build --platform linux/amd64 -t <temporary-runtime-tag> environment
   ```

   If an optional local two-image configuration is explicitly being tested and
   `tests/Dockerfile` exists, build and inspect that image as a separate,
   non-submission check.

   Before declaring the image checks impossible, inspect `docker context show`,
   `docker context ls`, `docker info`, and `DOCKER_HOST`. If another already
   configured client-approved context is reachable, use it explicitly with
   `docker --context <name> ...`. Do not chmod a Docker socket, expose the
   socket to the task, use an unapproved remote daemon, or weaken the task
   policy. If no approved daemon is reachable, continue all static repairs and
   report the exact access error; mark the architecture and image-size checks
   `UNVERIFIED` rather than claiming the task or Dockerfile is at fault.

   Do not build a separate verifier image for the default shared-container path.
   If an optional local two-image configuration is explicitly being tested and
   `tests/Dockerfile` exists, inspect that image with
   `docker image inspect --format '{{.Size}}'` and fail the size gate if it
   exceeds `2000000000` bytes. A file-presence check inside a temporary
   container is allowed; do not execute `solution/solve.py`, the solution
   entrypoint, `tests/test.sh`, pytest, the Oracle, Harbor, or any agent
   trajectory from this skill.

   Also run a dependency-only smoke check in each affected image with
   `--network none` and a scrubbed environment, so the check does not inherit
   the image's `PATH` by accident. For example:

   ```bash
   docker run --rm --network none --entrypoint /usr/bin/env <image> \
     -i DEBIAN_FRONTEND=noninteractive /bin/sh -c \
     'command -v python3 && python3 -c "import <runtime-packages>"'
   ```

   If an optional local verifier image is being tested, also check `command -v pytest` (or every
   other CLI named by `tests/test.sh`) and import its required modules. The
   check must succeed using the interpreter selected by bare commands under
   the scrubbed environment; do not treat a normal-`PATH` virtualenv check as
   sufficient. If no separate image is used, perform these dependency checks
   in the runtime image. If a prior Oracle attempt exists, read its `agent/oracle.txt`,
   verifier stdout, exit code, and trial log before interpreting artifact
   errors. A best-effort download failure such as `path does not exist` is
   usually downstream of the producer or verifier failing; repair that primary
   failure rather than adding placeholder outputs or changing artifact names.

   Use `--network none` for any permitted container check. The task runs with
   `network_mode = "public"`, but the import and file-presence checks stay
   offline on purpose: that is how you prove the libraries are vendored rather
   than fetched. Remove temporary containers and images in a trap/cleanup path,
   including after interruption. If Docker or a vendored library is unavailable,
   report the check as unverified rather than installing it or claiming success.

7. **Audit the fail → pass transition statically.**

   Submission validation requires `tests/test.sh` to fail on the unmodified
   environment and to pass after `solution/solve.sh` runs. Running either script
   is out of scope for this skill, so check the conditions that make the
   transition possible by reading them:

   - the verifier must fail when the outputs do not exist yet. Flag a verifier
     that skips on missing files, catches its own assertion errors, writes a
     default passing reward, or asserts nothing substantive — that is a vacuous
     pass and fails validation as trivially solvable;
   - `test.sh` must write the reward on both branches, to
     `/logs/tests/reward.txt` or `/logs/verifier/reward.txt`, before exiting;
   - `test.sh` must be safe to run more than once: any file, database, or
     service state it creates has to be reset or handled on re-entry;
   - neither `test.sh` nor `solve.sh` may contain an install command;
   - `solve.sh` must tolerate `/app` as its working directory and must be
     executable, as must `test.sh`.

   Report any of these as a blocker rather than editing the verifier or the
   solution. The executable proof belongs to the project smoke test, which runs
   the solution and the verifier in one container.

## Failure handling

Return `FAIL` only after attempting the in-scope repairs. Fail when a required
implementation or scientific input is missing, metadata remains invalid — for
example a `[task].name` that still has no `org/` namespace — a
Docker build context or path remains broken, a required library cannot be
vendored, a run-time install remains, an image is not `linux/amd64`,
an image exceeds 2 GB, or a mismatch can only be fixed by editing instruction,
solution, or verifier content. Include the exact file and a concise remedy for
the author. A denied Docker socket is an external validation blocker: continue
static repairs and mark image architecture/size evidence `UNVERIFIED`.

Return `PASS` only when all in-scope repairs are complete and the Oracle
prerequisites were verified. A PASS means “ready to attempt the Oracle,” not
“the Oracle passed.”

## Output

Return only a concise final Markdown handoff beginning with `**Status:** PASS`
when the Oracle prerequisites are ready, or `**Status:** FAIL` when the task is
blocked or a required check could not be completed. Do not include planning,
tool transcripts, duplicated status sections, or token-usage text. Summarize
files changed, checks run, and remaining blockers. When run through
`scripts/run-task-fixer.sh`, the wrapper saves only this final handoff in
`skill-reports/task-fixer.md`.

## Guardrails

- Never edit `instruction.md`, any file under `solution/`, or any verifier/test
  implementation. This includes `solve.py`, `solve.sh`, `tests/test.sh`, test
  Python files, thresholds, fixtures, and expected outputs.
- File mode changes on existing required shell entrypoints are allowed; never
  change their contents. Editing an existing reviewer `README.md`, Dockerfiles,
  data directories, `.gitkeep`, and approved vendored dependency files is allowed
  only when the contents are derived from the task and genuinely required. Do
  not add a README solely to satisfy scaffold compliance.
- Never alter the scientific problem, output schema, tolerances, or verifier
  assertions to manufacture Oracle success.
- Never create placeholder solution/test implementations or fake scientific
  data merely to satisfy a required-file check.
- Keep all edits inside the one target task and limited to metadata, reviewer
  notes, Docker/build configuration, file modes, and required local input or
  dependency data.
- Do not introduce a new network dependency, secrets, hidden answer data,
  task-local agent skills, caches, or unrelated files. Existing HTTP access to
  a scientific database or tool stays as the author wrote it.
- Do not leave online apt, pip, curl, or package bootstrap commands when an
  approved offline base or local bundle can replace them. `network_mode` is
  `"public"` by policy, so it is never the fix for a failing build or a missing
  library: vendor the library instead.
- Never stage build artifacts under `/tmp`, and never leave a verifier
  dependency installed only in the optional local `tests/Dockerfile`.
- Never write the author's scientific metadata: the task description, the three
  explanation fields, and the expert time estimate stay in the author's voice.
- Clean up every temporary container and image created during validation.
- Cite every changed path and every unverified check in the final handoff.
