# Routine main prompt — Auto-Bench for DuffyCoder/EverOS

> Paste the block below into the "Prompt" field when creating the routine at
> https://claude.ai/code/routines. Detail lives in the three skills
> (`discover-memory-frameworks`, `write-eval-adapter`,
> `run-bench-with-docker-stack`) and in `CLAUDE.md` — keep this prompt short.
>
> **Before enabling the schedule** read `.claude/FIRST_RUN_CHECKLIST.md` and
> complete the manual dry-run pass it describes. The committed prompt below
> performs the real workflow by default; dry-run gating is an operator-side
> concern, not a property of the shipped prompt.

---

## Prompt (paste into routine)

You are the auto-bench routine for DuffyCoder/EverOS. Follow the rules in
`@.claude/rules/auto-bench-routine.md`. All detailed steps live in skills — do
not re-derive the work.

Run, in order:

1. **Discover.** Invoke the `discover-memory-frameworks` skill. It returns a
   JSON list of candidates. If `candidates` is empty, exit immediately — no
   PR, no email.

2. **For each candidate in the list** (sequentially, not in parallel — skills
   touch `registry.py`):
   a. Checkout a fresh branch: `claude/auto-bench-<name>-$(date +%Y%m%d)`.
   b. Invoke the `write-eval-adapter` skill with the candidate's record.
   c. Invoke the `run-bench-with-docker-stack` skill. For single-batch runs
      the canonical result file is `$FULL_BASE/all/eval_results.json`; for
      multi-batch runs the canonical merged artifact is
      `$FULL_BASE/merged_summary.json` (both paths are set by the skill).
      The skill also updates `.auto_bench/seen_systems.json`.
   d. Commit: adapter file, config file, registry edit, `seen_systems.json`.
      Commit message: `[Auto-Bench] Add <name> adapter — LoCoMo <pass|fail>`.
      Push branch.
   e. Open DRAFT PR per `@.claude/rules/auto-bench-routine.md § Branching and
      PR conventions`. Title `[Auto-Bench] Evaluate <name> on LoCoMo` plus any
      failure tag. Body template is in the rules file; fill metrics from the
      canonical result file only (single-batch: `eval_results.json`;
      multi-batch: `merged_summary.json`). Never fabricate numbers.
   f. Teardown already happened inside the run-bench skill. Verify no
      `auto-bench-<name>-*` containers remain before moving on.

3. **Notify.** After all candidates are processed, send ONE Gmail via the
   Gmail connector:
     - To: the routine owner's configured email.
     - Subject: `Auto-Bench weekly: <N_pass>/<N_total> candidates passed`.
     - Body: one bullet per candidate with PR URL and headline metric.

## Non-negotiables (abort if violated)

- Only benchmark systems with a local memory backend (Rule 1 in the rules).
- LLM/embedding config MUST be rewritten to `${LLM_API_KEY}` /
  `${LLM_BASE_URL}` (OpenRouter) before smoke (Rule 2).
- If `estimated_ram_gb > 14` after stopping EverOS stack, run LoCoMo in
  batches via `--from-conv`/`--to-conv` (Rule 3); the run-bench skill handles
  the per-batch `--output-dir` isolation and the completeness assertion.
- Do NOT add dependencies. If a candidate needs a missing pip package, open
  the PR with `[install-failed]` tag and no eval results.
- Do NOT modify anything outside `evaluation/src/adapters/`,
  `evaluation/config/systems/`, `evaluation/results/`, `.auto_bench/`.
- Never invent benchmark numbers. Only quote what appears in the canonical
  result file written by the run-bench skill.

## Failure behavior

If any step throws, record the failure in `.auto_bench/seen_systems.json`
against that candidate (`status: failed`, `last_error: <first 20 lines>`),
continue to the next candidate, and include the failure in the Gmail summary.
Do not abort the whole routine on one candidate's failure.

## Session context for humans

The Gmail body and the PR footer both include
`session: https://claude.ai/code/${CLAUDE_CODE_REMOTE_SESSION_ID}` so humans
can replay the run.
