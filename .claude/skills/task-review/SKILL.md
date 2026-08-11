---
name: task-review
description: Review a Harbor task folder against every criterion in the
  repository rubric and produce an evidence-backed PASS / FAIL / N/A report
  with totals and prioritized fixes. Use when the user asks to "review this
  task", "score this task", "grade the task instructions", "task scorecard",
  or points the skill at a task folder for evaluation against the rubric.
---

Review only the criteria declared in the repository rubric. Do not add separate
deployment gates, client-policy checks, holistic requirements, or extra verdict
rows. Cite file paths and line numbers for every criterion verdict.

Use a proportional, researcher-centered standard. The instruction should read
like a natural prompt from a researcher: it should state the scientific goal,
relevant inputs, meaningful constraints, and useful outputs or decision. Assume
the agent can make a reasonable choice of method, numerical routine, library,
and routine output representation from that context. Do not fail a task merely
because the instruction omits a conventional implementation detail.

Review the public contract and verifier together. Material outcome-level
requirements should be visible in `instruction.md`, but do not require the
prompt to repeat every numerical verifier constant. In domain tasks, exact
acceptance thresholds and category boundaries
may be treated as stable operationalization of an outcome-level decision rather
than hidden requirements, provided the instruction identifies the decision and
deliverables at a useful broad level. A compact reference to the task's stated
acceptance criteria or evaluation rules is enough; do not require every
criterion label to be listed. Private fixtures,
independent recomputation, and reasonable tolerances are not hidden
requirements by themselves. Prefer tests that execute the agent's work and
accept scientifically defensible methods over tests that reproduce one
reference algorithm or exact intermediate values.

Mark `FAIL` only for a clear, material violation of a rubric criterion. Treat
rough guides and examples proportionately; do not turn normal scientific
discretion, an inferable output convention, or a reasonable method choice into
a failure. Keep security and anti-cheating findings concrete and strict.

When suggesting repairs, use the smallest useful change:

- If `instruction.md` spoon-feeds the answer, expected value, model choice, or
  reference procedure, remove that leakage while retaining the scientific
  context, objective, inputs, necessary definitions, constraints, and
  deliverables.
- If tests enforce a hidden material requirement or one method, simplify them
  to execute the output and verify the shared scientific outcome or essential
  invariants. Accept reasonable alternative methods and representations.
- If a genuinely required outcome or deliverable is absent from the instruction,
  state that outcome clearly without adding a step-by-step recipe. Do not
  recommend copying verifier-only calibration constants into a concise
  contract when the corresponding domain decision is already named. If a
  conventional detail is inferable, leave it to the agent rather than
  prescribing it.

Docker may be inspected when it is useful evidence for a rubric criterion such
as resource configuration, environment hygiene, or reproducibility. If Docker
evidence is unavailable, explain that limitation in the relevant row; do not
create a separate deployment verdict.

The submission sandbox runs the agent and verifier in the same runtime image.
Treat `tests/Dockerfile` as an optional local two-image fixture, not a task
prerequisite, and do not fail a task or require a second image solely because
that optional file is absent.

## Inputs

- **Rubric**: `task_implementation.toml` at the repo root. Read every
  `[[criteria]]` entry (`name`, `description`, and `guidance`).
- **Target**: the canonical `task/` directory for a submission-ready run.
  Expected layout:
  - `task.toml`
  - `instruction.md`
  - `environment/` (Dockerfile, data, supporting assets)
  - `solution/` (`solve.sh`, `solve.py` or equivalent)
  - `tests/` (`test.sh`, `test_outputs.py` or equivalent)
  - optional `README.md`

If the folder doesn't have this layout, stop and report what's missing — do not invent verdicts.

## Canonical path and logical task name

Keep these two names separate:

- `task/` is the fixed outer directory used by the scaffold and the submission
  package. Do not rename it to make the task look more specific.
- `[task].name` in `task/task.toml` is the Harbor task's logical identity. Use
  that value when judging a rubric criterion about the task's name or whether
  the task is meaningfully identified. Cite `task/task.toml`; do not judge the
  basename `task` as though it were the logical name.

The rubric's `task_name` row uses the older phrase "task folder name." In this
scaffold, interpret that row as the descriptive portion of `[task].name` after
the required namespace prefix (for example, `aligned/kinase-pocket-ranking`:
judge `kinase-pocket-ranking`). Apply the row's concise kebab-case rules to
that portion, then repair `[task].name` if needed. Never satisfy this row by
renaming the outer `task/` directory.

Run the skill from the repository root with the target `task` (the default for
`scripts/run-task-review.sh`). The wrapper records `Target | task` in the
report. A report generated with a renamed target such as
`aligned/my-task` is not submission-ready, even when the files inside it are
valid. Restore the content under `task/` and rerun the review; do not hand-edit
the report's target or rename the canonical directory.

## Procedure

1. **Load the rubric.** Read the rubric file identified in Inputs fully and list
   every criterion `name` in order. Do not skip any. If a new criterion is
   added, score it too — never hardcode the list.
2. **Survey the task.** Read, at minimum:
   - `task.toml` (metadata, timeouts, resources)
   - `instruction.md` (the contract presented to the agent)
   - `solution/solve.sh` and the script(s) it invokes (`solve.py`, etc.)
   - `solution/process.md` if present, referenced, or needed to understand the
     solution workflow
   - `tests/test.sh` and the verifier (`test_outputs.py`, etc.)
   - `environment/Dockerfile` and a directory listing of `environment/`
   - any recent oracle logs for this task under `jobs/oracle-batch/` if they
     already exist
   - `README.md` if present
   Read whole files when they're small enough; for larger files, read the sections needed to evaluate each criterion. Do not delegate this to a subagent if you can read the files directly — you need the contents in scope to cite line numbers.
   If the repository provides `scripts/validate_scaffold.py`, run its
   dependency-free static check against the repository root. Treat its findings
   as evidence for the existing rubric rows; do not invent a separate
   deployment criterion from a warning.
   Before scoring any criterion that says "task name" or "task folder name",
   resolve it against `[task].name` in `task/task.toml`. The literal `task/`
   wrapper is intentionally not required to have a descriptive basename.
3. **Audit verifier self-consistency.** Build an assertion inventory before
   scoring. For every assertion that can affect pass/fail, including assertions
   in helpers, fixtures, parametrized cases, and `test.sh`, record its file/line, the
   output or input it constrains, and the predicate it applies. Compare the
   inventory for contradictions in required methods, output schemas, filenames,
   units, ranges, tolerances, labels, and decision rules. The conjunction of
   assertions must be satisfiable. Flag cases such as one test requiring Method
   A's output format while another rejects the same output unless Method B was
   used, mutually exclusive keys, incompatible units, or overlapping bounds
   that leave no valid result. A private fixture or independent recomputation
   is not a contradiction by itself. Carry any finding into the relevant
   existing criterion (usually `test_instruction_alignment`,
   `functional_verification`, or `outcome_verified`) with citations; do not add
   a new scorecard row.
4. **Compare contract and verifier.** For each material test assertion, ask
   whether the outcome or deliverable is visible in `instruction.md`. Check
   both directions: do the tests verify the important deliverables, and do they
   introduce a contradictory outcome, an undisclosed deliverable, or a method
   that a reasonable researcher could not infer? Do not require the prompt to
   restate every exact domain-specific cutoff when it names the corresponding
   decision. For a field described with a superlative or target value, a finite,
   valid value satisfying the relevant domain constraint may be a sufficient
   operationalization. Do not fail alignment solely because the verifier does
   not prove global maximality or optimality unless optimization itself is the
   stated objective or changes the decision.
5. **Consider the real workflow.** For scientific criteria, identify the likely
   researcher, setting, decision, and meaningful judgment from the task files.
   Use that context as evidence for the rubric criterion, but do not create a
   separate practitioner score or fail a task merely because its data are
   synthetic or its analysis has one coherent method.
6. **Score each criterion.** For each rubric entry, decide one of:
   - `PASS` — meets the guidance.
   - `FAIL` — clearly violates the guidance in a material way. Quote the
     relevant guidance clause and explain the smallest useful fix.
   - `N/A` — only when the guidance explicitly permits N/A (e.g. `structured_data_schema` when no structured output is expected, `task_readme` when no README is present).
   Do not add `UNKNOWN`, deployment gates, or extra criteria. If evidence is
   unavailable, state that limitation in the relevant Notes cell and do not
   claim a failure that the available evidence does not support.
6. **Cite evidence.** Every verdict needs at least one citation in
   `path/to/file:line` form. For `FAIL`, include a one-sentence fix suggestion.
   For `PASS`, a brief justification is enough.
7. **Write the scorecard.** Start with `**Status:** PASS` when no rubric
   criterion is `FAIL`; otherwise start with `**Status:** FAIL`. Report the
   total number of criteria and separate PASS, FAIL, and N/A counts. Produce
   the complete scorecard in Markdown, not only a summary or a file path. The
   wrapper saves it as `skill-reports/task-review.md`.

## Scorecard format

```
# Task review: <task-folder-path>

**Status:** <PASS|FAIL>

**Total:** <N> criteria reviewed — PASS: <P> | FAIL: <F> | N/A: <A>.

**Summary:** <one-line assessment of the task against the rubric>.

## Verdicts

| # | Criterion | Verdict | Evidence | Notes |
|---|-----------|---------|----------|-------|
| 1 | verifiable | PASS | tests/test_outputs.py:14-260 | Deterministic numeric tolerances, no LLM judge. |
| 2 | well_specified | FAIL | tests/test_outputs.py:42 | The verifier requires an outcome not stated in the instruction; expose the outcome or simplify the assertion. |
| ...

## Top fixes (ordered by impact)

1. **<criterion>** — <one-line action>. Evidence: <path:line>.
2. ...

## Out of scope / N/A

- `task_readme` — no README present (allowed).
- `structured_data_schema` — N/A: ...
```

Keep the table rows one line each where possible; spill into "Notes" only when
needed. The Top fixes list should call out the 3–7 most consequential failures
so the author knows where to start. If there are no failures, write
`No fixes required.` Do not add deployment-gate rows or a separate holistic
score.

## Applying the criteria

Treat each criterion's `guidance` as the intended standard, but apply its
examples and rough guides proportionately. A criterion is not a license to add
hidden implementation requirements. Mark `FAIL` only when the evidence shows a
clear, material violation; otherwise give credit for a natural, solvable task.

- **Verifiable and functional verification**: Prefer tests that execute the
  agent's output and check concrete scientific results or invariants. Reject
  source-keyword checks, subjective prose grading, brittle reference-only
  comparisons, and undisclosed scientific-content gates. A verifier must not
  require a literature reference, author name, paper title, citation, or
  domain-specific term unless `instruction.md` explicitly requests it as an
  evaluated output or fact. Private reference fixtures and independently
  recomputed values are fine when they check an outcome the instruction asks
  for. Record a content-gating defect under the existing functional or
  instruction-alignment criterion; do not create a separate row.
- **Well specified, aligned, and structured output**: Require the goal,
  relevant inputs, material constraints, outcome-level decisions, and
  deliverables to be understandable to a reasonable researcher. Every material
  pass/fail assertion should map to an outcome in the instruction, but do not
  require the prompt to spell out each operational cutoff, solver tolerance,
  integration routine, uncertainty algorithm, library, or routine
  serialization choice. If a test forces one method, a contradictory outcome,
  an undisclosed deliverable, or a non-inferable field, fail the relevant
  criterion and suggest simplifying the test or stating the needed outcome
  explicitly.
- **Solvable, solution quality, and reviewability**: Confirm that the supplied
  solution genuinely derives the result, dependencies and data are available,
  and the files give enough scientific context to understand the work. Do not
  invent a required process file or other artifact unless the criterion or task
  explicitly calls for it.
- **Outcome verification**: Grade the end state and decision, not the route.
  Accept scientifically defensible alternatives whose intermediate values
  differ within the task's legitimate variation.
- **Determinism, hygiene, security, and anti-cheating**: Check the concrete
  criterion requirements, including reproducibility, dependency behavior,
  runtime-image boundaries, hardcoded answers, malicious code, and obvious
  shortcuts. Keep security findings strict, but do not turn normal scientific
  implementation choices into security findings.
- **Difficulty, novelty, agentic work, and scientific grounding**: Ask whether
  a real researcher could plausibly give this prompt to an agent and use the
  result. Credit meaningful domain judgment, interpretation, validation, and
  iteration. Do not require a particular number of tools, branches, or a
  fabricated level of complexity; do fail textbook, clerical, or purely
  mechanical tasks when the criterion clearly calls for more.
- **Instruction clarity and minimality**: Keep the prompt natural, concise, and
  fair. Necessary domain facts, outcome-level definitions, units, principal
  decision criteria, and output requirements are not spoonfeeding. Remove the
  expected answer and reference-solution recipe, but do not punish a task for
  keeping exact operational cutoffs in the verifier when the associated
  domain decisions are stated. Do not punish an agent for making a
  reasonable choice of method or routine output details.
- **Metadata, resources, explanations, names, and optional README**: Check the
  specific rubric language and judge whether the values are plausible and
  useful. Treat optional material as N/A when the rubric permits it; do not add
  deployment policies or requirements from outside the rubric.

For `expert_time_estimate`, make the plausibility judgment in the review itself:
assess the number from the scientific scope, required judgment, data and
validation burden, and reference-solution workflow. Treat it as a human-only
estimate.
When completed Harbor or quick-trial results exist, use their recorded
timestamps as supporting evidence, report model/agent elapsed time separately
from verifier and total trial time when possible, identify timeout-censored
runs, and use those observations only to assess agent timeout behavior,
infrastructure problems, and reproducibility. Never compare agent elapsed time
with the human estimate or infer human duration from it. Do not replace this
judgment with a heuristic duration test.

## What to do, not do

- **Do** quote specific lines as evidence. `instruction.md:14` is useful; "the instructions are unclear" is not.
- **Do** be willing to FAIL a task. A scorecard that says PASS for everything is useless.
- **Do not** edit any task files. This is a review skill, not a repair skill. Mechanical repairs live in the `task-fixer` skill.
- **Do not** invoke the oracle or run the verifier. If you need to know whether the verifier passes, say so in the scorecard — running it is the user's call.
- **Do not** leave Docker containers or task-test images behind if the user
  explicitly asks for Docker validation during review. Clean up every container
  and image you create, including failed runs.
- **Do not** invent criteria or deployment gates. Score exactly what's in the
  selected repository rubric file.
- **Do not** offer to "rewrite the instruction" unsolicited. If the user asks for fixes after the scorecard, that's a separate request.
