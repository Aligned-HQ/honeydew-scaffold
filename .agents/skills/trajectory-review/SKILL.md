---
name: trajectory-review
description: Review the latest Harbor 3-agent trajectory run for a task. Pass when the agents failed because their science or reasoning was wrong. Ignore agent/platform errors and contact failures against external scientific services, and flag as non-genuine any failure where the trajectory shows correct science or a defensible alternative method that the verifier rejected. Require the instruction to name the keys of a required output structure without revealing the method; otherwise do not demand a perfect instruction/verifier contract. Still fail structural task bugs and brittle verifiers. Use when asked for an easier trajectory review, permissive trajectory review, completed_trajectories review, harbor-jobs review, 3x agent run review, pass/fail trajectory review, or whether a task failure is acceptable under a domain-expert standard.
---

# Trajectory Review

Review a Harbor 3-agent run for a task. The goal is not to fix the task; it is
to decide whether the agents failed because their science or reasoning was
wrong. That is the only failure the benchmark wants. A reward of 0 that came
from anything else is noise, and your job is to say which is which.

Two things get mistaken for scientific failure. Separate both out before
judging:

- **Contact issues.** Tasks run with `network_mode = "public"` so the reference
  solution and the agent can query scientific databases and tools over HTTP. A
  trial that died on a DNS error, connection reset, timeout, rate limit, TLS
  error, 5xx, or an empty response from an external service never got to the
  science. Ignore it, exactly like a Modal startup failure.
- **Correct work the verifier rejected.** If the trajectory shows the agent did
  the science correctly, or followed a defensible alternative method that a
  researcher in the field would accept, the failure is not genuine no matter
  what the test asserted.

A verifier may require methods, threshold concepts, statistical checks,
numerical procedures, domain conventions, names, formats, or schemas that are
not spelled out line-by-line in `instruction.md` if a competent researcher in
the task's field could reasonably infer them from the problem, data,
references, and stated scientific objective. Do not fail a task only because
the verifier rewards such reasonable domain-inferable work.

Do not be a stickler for a perfect contract. Stop recommending that
`instruction.md` or the verifier spell out every detail so the two match
exactly; over-specifying the contract hands the agent the method and drains the
science out of the task. There is one contract requirement worth insisting on:
when the verifier requires a specific output structure, `instruction.md` must
name the keys, columns, and filenames it will be graded on — the shape, not the
method. See step 6.

Ignore trial failures caused by agent/platform problems that are not related to
the task itself. Examples include model policy refusals, unavailable agent
credentials, agent process startup crashes, Modal sandbox startup failures,
transient provider/API failures, agent CLI installation problems, or tooling
failures before the agent has a meaningful chance to inspect or solve the task.
Report these separately as ignored trials; do not count them as task failures.

Difficulty is not a defect. The submission checker has one hard difficulty
gate: every agent must fail at least two of its four trials. Count only genuine
scientific failures toward that gate; ignored trials and non-genuine failures do
not qualify an agent for it. If any of Claude, Codex, or Gemini fails fewer than
two trials on the science, report that hard gate failure and that the task is
likely too easy for the benchmark. The overall Claude/Codex/Gemini pass rate is
an advisory warning only; a rate at or above 50% suggests the task may be too
easy but does not fail the check by itself. Judge whether each failure was
scientific, non-genuine, structural, or noise; do not convert a low pass rate
into a task bug on its own.

## Submission paths and exception records

Use the scaffold's fixed roots for the final runs:

- `task-review` and `task-fixer` target `task/`;
- `trajectory-review` targets `trajectories/` and finds the latest run inside
  it.

The wrapper's `Target` field is a path contract, not the Harbor task's logical
identity. Keep `Target | task` for task reviews and `Target | trajectories` for
this review. The logical identity belongs in `[task].name` in
`task/task.toml`; cite that file when discussing the task name. Do not rename
`task/` to a slug and do not point the final wrapper run at a slug-named
directory. If the task was reviewed under such a directory, restore the
canonical layout and rerun the relevant skills before submission.

Treat `summary.json` as a per-trial evidence ledger, not as a requirement that
the exception count be zero. A finished trial may legitimately have
`verdict: EXCEPTION`, a numeric reward (normally `0`), and archived trajectory
evidence. Keep that record intact and inspect the trial before classifying it:

- an agent that spent meaningful time on an incorrect scientific path and then
  hit its timeout is a genuine scientific failure and is compatible with a
  passing trajectory review;
- an exception before meaningful task work, such as an agent startup failure,
  provider failure, or external-service contact failure, is ignored noise;
- correct science rejected by the verifier is non-genuine.

Do not turn an exception into `PASS`, delete it, or fabricate a zero-exception
summary merely to satisfy a soft check. Report the exception classification and
the evidence. The summary still must be internally consistent: every included
trial needs a finished status, a valid verdict and numeric reward, and any
trajectory reference must resolve to archived evidence.

## Inputs

- A set of completed trajectories.
- The canonical target is `trajectories/`. Typical completed run layout:
  `trajectories/summary.md`, `trajectories/oracle/`, and one direct folder per
  agent such as `trajectories/claude-code/`, `trajectories/codex/`, and
  `trajectories/antigravity/` (the Gemini agent).

## Workflow

1. **Find the latest relevant 3-agent run.**
   - Inspect the `trajectories/` folder

2. **Read the job-level summary first.**
   - Read `summary.md` when present.
   - Read each agent job `result.json` and note:
     - number of trials,
     - rewards,
     - errored trials,
     - exception stats,
     - pass@k when present.
   - Map failed trial ids from `reward_stats["0.0"]` and exception stats to
     their trial directories.

3. **Collect failure evidence from each failed trial.**
   - Read `trial.log`, `result.json`, `verifier/pytest.log`,
     `verifier/test-stdout.txt`, `verifier/reward.txt`, and artifact
     `manifest.json` when present.
   - Read only the relevant parts of agent trajectories:
     `agent/*.txt`, `agent/*.jsonl`, or `agent/trajectory.json`. Search for the
     output filenames, failed keys, exception messages, and final commands
     before loading large traces.
   - Read enough of the trajectory to see what the agent actually did
     scientifically, not just what the verifier printed. A failed assertion tells
     you the answer differed; only the trajectory tells you whether the reasoning
     behind it was wrong.
   - Grep the trial and agent logs for contact evidence before anything else:
     connection refused/reset, timeout, `Temporary failure in name resolution`,
     TLS/certificate errors, HTTP 429, HTTP 5xx, empty or truncated payloads
     from an external service, and retry storms against one host.
   - Compare failed trial artifacts with at least one passing trial artifact
     from the same run when available.

4. **Classify each failure.** The first two categories decide the review; work
   through them before reaching for any of the others.

   - **Genuine scientific failure**: the trajectory shows the agent's science or
     reasoning was wrong — wrong model selection, poor fit, nonphysical result,
     incorrect uncertainty, inadequate validation, a misread of the data, a
     shortcut that skipped the analysis, or an unsupported scientific claim.
     This is the outcome the task wants, and it is compatible with PASS.
   - **Non-genuine failure**: the trajectory shows the agent did the science
     correctly, or followed an alternative method a researcher in the field would
     accept, and the verifier still scored it 0. Typical shapes: the verifier
     accepts one implementation path, one estimator, one rounding, one ordering,
     or one key spelling; the tolerance is tighter than the method's own
     variability; the required structure was never stated (step 6). Say plainly
     that the agent's work was correct, quote the evidence, and do not count the
     trial toward the difficulty gate. The remedy is almost always to loosen the
     verifier so it accepts the alternative — not to add more specification to
     `instruction.md`.
   - **Structural task bug**: missing runtime data, Docker build/copy failure,
     missing dependencies, wrong user/permissions, missing output artifacts,
     reward-file problems, no trial result, task not loaded into `lock.json`,
     missing runtime or verifier-overlay files, or agent could not run the
     provided tools. Also structural, and each fatal at submission: a verifier
     dependency installed only in an optional local verifier image, since the
     submission sandbox runs one container; a dependency or cache staged under `/tmp` at build time, which
     the sandbox wipes with a fresh tmpfs; a run-time package install, which the
     task must not need because `network_mode = "public"` is for reaching
     scientific databases and tools over HTTP, not for fetching the toolchain;
     a verifier that passes without the agent having
     produced anything, which fails validation as trivially solvable; and a
     verifier whose second run disagrees with its first because it left state
     behind.
   - **Ignored agent/platform error**: an error unrelated to the task contract
     or scientific work, such as Modal/Harbor startup problems, agent CLI
     boot failures, missing provider credentials, transient API/provider
     outages, policy refusals, content-filter blocks, or agent runtime crashes
     before meaningful task work began. Exclude these from pass/fail evidence
     unless the logs show the task caused the error.
   - **Ignored contact issue**: the trial could not reach an external scientific
     database, API, or tool — DNS failure, connection refused or reset, timeout,
     rate limit, TLS error, 5xx, or an empty/partial response — and that is why
     the science is missing or wrong. Ignore it the same way, and never read it
     as the agent failing at the science. Two follow-ups: if the same host fails
     across most meaningful trials, report it as an advisory that the task leans
     on a fragile service and the record should be cached in
     `environment/data/`; if the agent then invented, hardcoded, or silently
     substituted the data it could not fetch, that part is a genuine scientific
     failure and should be classified as one.
   - **Reasonable domain-inferable method requirement**: verifier expects a
     method, model family, diagnostic, threshold concept, numerical procedure,
     validation check, or scientific convention that is not explicitly spelled
     out, but a researcher in the field could reasonably infer it from the
     prompt, data, references, and scientific goal. Treat this as compatible
     with PASS unless the implementation is brittle or overly narrow.
   - **Reasonable inferable clerical contract**: the verifier expects filenames,
     key names, column names, units, output shapes, or formats that are not
     explicitly specified but follow naturally from the instruction, examples,
     visible input data, task naming, or standard field conventions, and the
     agent had the context to infer them. Agent-side and compatible with PASS.
     If the science was right and the only defect is a name the instruction
     never gave and nothing implied, it is a non-genuine failure instead.
   - **Undisclosed output structure**: the verifier grades a structure —
     JSON keys, nesting, CSV headers, output filenames — that `instruction.md`
     never names and that the agent could not infer, so correct science scored 0
     on shape alone. Non-genuine. The remedy is to list the keys in the
     instruction (step 6), not to describe the method more fully.
   - **Prompt-test mismatch**: the verifier requires a hidden assumption,
     external source, exact algorithm, config value, or nonstandard convention
     that is not disclosed in `instruction.md`, visible data, or ordinary domain
     knowledge, and that the agent could not have inferred. Reserve this for
     requirements that were genuinely unknowable, not for wording you would have
     phrased differently.
   - **Tolerance failure**: values are scientifically reasonable and align with
     task wording, but tests use overly tight absolute/relative thresholds,
     brittle seeds, exact optimizer path expectations, or unstable ordering.
     Non-genuine: the verifier is measuring implementation luck, not science.
   - **Clerical failure**: missing or misnamed JSON keys, CSV headers, artifact
     filenames, units, boolean fields, or report fields where the scientific
     result is otherwise present. Agent-side and compatible with PASS when the
     contract was stated or plainly inferable; non-genuine when it was not.

5. **Use cross-agent evidence.**
   - Remove ignored agent/platform errors and contact issues from the
     denominator before judging pass/fail patterns. For example, if one agent
     has a Modal startup failure, one loses its trial to a database timeout, and
     two agents solve or scientifically fail the task, classify the task from
     the two meaningful trials.
   - If two agents pass and one fails, inspect whether the failing agent simply
     made a scientific mistake or whether tests reward one narrow formatting
     path.
   - If different agents fail on different formatting details while their
     numbers agree, that is the verifier being narrow, not three independent
     scientific failures.
   - If all meaningful trials fail similarly, strongly suspect structural bug,
     clear prompt-test mismatch, excessive tolerance, missing data, or an
     underspecified task. Still check whether the shared failure is a
     legitimate scientific mistake against a domain-inferable standard.
   - If all three pass except rare stochastic failures, check for brittle
     randomness or tolerance issues before calling the task robust.
   - Passing peer trials are evidence that the task can be solved, but they do
     not by themselves prove the failed trial is scientific; still compare the
     failure mode against the prompt and verifier.

6. **Check the output structure, and only the output structure.**

   Read `instruction.md`, `task.toml`, and `tests/test_outputs.py`. This step
   has one job: an agent that does the science right must be able to write its
   answer where the verifier will find it. It is not an audit of how completely
   the instruction describes the task.

   - When the verifier grades a specific structure, `instruction.md` must name
     it: the output filenames, the JSON keys (including nesting), the CSV
     headers, and the units. Those are the shape of the answer, and an agent
     cannot infer a spelling nobody gave it.
   - The structure must not give away the method. Keys should name what is
     reported, not how to compute it: `binding_affinity_kcal_per_mol` is a
     result, `docking_score_vina_exhaustiveness_16` is a recipe. If the schema
     hands the agent the approach, report it as an advisory difficulty risk —
     the task may be too easy — but do not fail the review over it.
   - Stop there. Do not recommend that `instruction.md` or the verifier be
     rewritten to pin down methods, algorithms, parameter choices, diagnostics,
     thresholds, or conventions so the two line up exactly. A researcher in the
     field is expected to infer those, and specifying them is how a task stops
     testing science.
   - Do not accept tests that grade prose wording, section names, keywords, word
     counts, tone, or report text instead of scientific evidence.
   - When the structure was stated or plainly inferable and the agent used
     something else, that is agent-side and compatible with PASS.

7. **Decide the disposition.**
   - **PASS trajectory review** when the agents failed because their science or
     reasoning was wrong. Wrong method for the question, wrong interpretation of
     the data, unvalidated or nonphysical results, a skipped analysis — all of
     that is the task working. PASS also covers enough agents passing with the
     rest failing agent-side, and a disputed expectation that is only a
     reasonable domain-inferable method or clerical requirement.
   - Ignore agent/platform errors and contact issues when deciding PASS or FAIL.
     List them in the report as ignored, not as failed task evidence.
   - **FAIL trajectory review** when the run does not show genuine scientific
     failure: structural task breakage, a verifier that rejects correct science
     or a defensible alternative, a brittle tolerance, an output structure the
     instruction never disclosed, or a requirement the agent could not have
     known. In short, fail the task for blocking correct science, not for an
     imperfect contract.
   - **INCONCLUSIVE** when full trial logs/artifacts are missing. State exactly
     which missing paths are needed.

## Output Format

Return the complete trajectory review as Markdown. Start with exactly one of
these status lines so the project wrapper can extract the report and update the
shared skill status file:

- `**Status:** PASS` when the failures are genuine scientific failures, or a
  disputed verifier expectation is reasonable for a domain researcher to infer,
  including clerical contracts.
- `**Status:** FAIL` when the task needs repair before review/upload.
- `**Status:** INCONCLUSIVE` when there is not enough trajectory evidence.

Then include:

- Run path and timestamp reviewed.
- Per-agent pass/fail table with trial ids and rewards.
- Failure classification for each failed trial, and for each one a plain
  statement of whether the agent's science was wrong.
- **Non-genuine failures**, if any: trial id, what the agent got right, and the
  verifier assertion that rejected it. Say which agents therefore have fewer
  genuine failures than the difficulty gate counts.
- Ignored agent/platform errors and contact issues, if any, with trial ids and
  the log evidence showing they are unrelated to the science.
- Evidence with file paths and concise line/log references.

Recommendations, when you make them, target the verifier or the output
structure the instruction owes the agent. Do not recommend adding method,
parameter, or threshold detail to `instruction.md` to make it match the tests.

When run through `scripts/run-trajectory-review.sh`, the wrapper saves this
complete Markdown result as `skill-reports/trajectory-review.md` and updates
`skill-status.md`. Do not write to either file directly, and do not return only
a summary or a file path.

Keep the report focused on failure evidence. Do not rewrite the task or modify
files unless the user explicitly asks for fixes.
