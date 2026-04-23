# Auto-Bench Routine — First-Run Checklist (operator-facing)

Do this ONCE before enabling the daily schedule. None of these steps are
part of the committed routine prompt; they happen through the claude.ai/code
UI and in your local shell.

## 0. Cloud environment network access (MOST COMMON FAILURE SOURCE)

Most LoCoMo-capable memory frameworks download an embedding model from
HuggingFace Hub or a tiktoken BPE encoding from `openaipublic.blob.core.windows.net`
the first time they construct their client. The default **"Trusted"** network
tier for Claude Code cloud environments blocks these two domains (PyPI and
Docker Hub are allowed, HF/blob are not). This is what took down PR#11/12/13
on the 2026-04-23 run — all three were `[smoke-failed]` with exactly this
cause.

Your options, in increasing invasiveness:

1. **Upgrade network tier.** In the cloud environment UI (where you pasted
   `.claude/setup.sh`), set **Network access** to an unrestricted tier if your
   plan offers one. This is the cleanest fix.

2. **Mirror via `HF_ENDPOINT`.** Add `HF_ENDPOINT=https://<your-mirror>` to
   the cloud environment's env vars. `setup.sh` honors it and `huggingface_hub`
   will route every call through the mirror. Works for HF but NOT tiktoken —
   tiktoken has no mirror-endpoint env var, so candidates using it still need
   direct `openaipublic.blob.core.windows.net` access.

3. **Pre-seed caches in `setup.sh`.** The committed `.claude/setup.sh` already
   does this idempotently — it probes HF + tiktoken reachability at cloud-env
   init, pre-pulls `all-MiniLM-L6-v2` and `o200k_base/cl100k_base`, and logs
   a clear diagnostic when blocked. If the probe says `HF Hub reachable: no`,
   option 1 or 2 is mandatory — further routine runs will keep producing
   `[asset-download-failed]` PRs.

4. **Prefer no-download candidates.** Candidates that use only an OpenAI-
   compatible LLM API (no local embedding, no tiktoken) are unaffected.
   The discover skill does not currently filter by this trait, so `[asset-
   download-failed]` PRs will still open — treat them as a signal to fix
   the network policy, not to change the candidate selection.

Verify after configuring: re-create or re-open the cloud environment, check
the setup log for the `::group::External asset caches (HF Hub + tiktoken)`
section and confirm both probes say `yes`.

## 0.5. GitHub write access (prerequisite — do this FIRST)

Routines use your connected GitHub identity (OAuth token), NOT the Claude
Code GitHub App. Without this step, every `git push` from the routine
returns HTTP 403 "denied to <user>" / "Resource not accessible by integration".

- In your local Claude Code CLI, run `/web-setup` and authorize the GitHub
  account that OWNS the routine target repo (`DuffyCoder/EverOS` in our case).
  This grants the routine's OAuth token `contents:write` + `pull_requests:write`.
- Separately, at https://claude.ai/code/routines → your routine →
  **Select repositories**, confirm `DuffyCoder/EverOS` is listed.
- Branch push policy: routines can push ONLY to `claude/`-prefixed branches
  by default. The routine's convention `claude/auto-bench-<name>-YYYYMMDD`
  satisfies this automatically. If you need to push to other branches from
  the routine, toggle **Allow unrestricted branch pushes** on the repo.

## 1. Create the routine with NO schedule

- Go to https://claude.ai/code/routines → Create routine.
- Paste the body from `.claude/ROUTINE_PROMPT.md` into the Prompt field.
- Leave the Schedule field empty. Use only "Run now" until validation passes.

## 2. Dry-run pass (manual — do NOT edit the committed prompt)

Paste an **extra** block ABOVE the committed prompt, in the routine UI only,
for the first one or two runs:

> DRY RUN — do steps 1 and 2a–2b only. After 2b, print the adapter file
> diff and the system YAML to the session log, then STOP. Do NOT run step
> 2c (bench), 2d (commit), 2e (PR), or step 3 (email).

Click "Run now" and inspect the log:

- [ ] `discover-memory-frameworks` returned a non-empty `candidates` list OR
      returned empty cleanly (no stack trace).
- [ ] For each candidate, the adapter file printed to the log is syntactically
      valid Python (spot-check the `@register_adapter` decorator and the
      four `_add_user_messages` / `_search_single_user` / `_build_*` overrides).
- [ ] The generated system YAML has `api_key: "${LLM_API_KEY}"` and
      `base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"`
      in its `llm:` block — not the candidate's default.

If any check fails, fix the skill and re-run dry-run. Do NOT proceed.

## 3. Real-run supervised pass

Remove the DRY RUN block from the routine UI prompt (the committed file
never contained it). Click "Run now" again. Watch the live log.

- [ ] Smoke test for each candidate exits 0 and writes
      `$SMOKE_DIR/eval_results.json`.
- [ ] Full run produces either `$FULL_BASE/all/eval_results.json`
      (single-batch) or `$FULL_BASE/merged_summary.json` (multi-batch).
- [ ] Coverage assertion in the merge step passed (no `COVERAGE GAP` error).
- [ ] Draft PRs opened on `DuffyCoder/EverOS`, branch name matches
      `claude/auto-bench-<name>-YYYYMMDD`, body has metrics from the
      canonical result file (not from log output).
- [ ] Gmail summary received with one bullet per candidate.
- [ ] After teardown, `docker ps` shows zero `auto-bench-*` containers.

## 4. Enable the schedule

- [ ] Set schedule to **daily at 02:00 Asia/Shanghai (UTC+8)** — i.e. `0 2 * * *`
      in Asia/Shanghai, or `0 18 * * *` in UTC. Low-traffic slot for the
      cloud runner; discovery that finds no new candidates exits cheaply
      without burning the per-candidate bench budget. Daily cadence uses
      1/15 runs on Max 5x (30 on Max 20x, 5 on Pro — Pro users should
      downshift to every 2–3 days to leave headroom for other routines).
- [ ] Configure required env vars in the routine's env section:
      - `LLM_API_KEY=<sophnet-project-key>` (Sophnet key; no stable prefix —
        copy the exact value from the project's .env)
      - `LLM_BASE_URL=https://www.sophnet.com/api/open-apis/v1` (optional;
        skill defaults to this)
      - `VECTORIZE_API_KEY` / `VECTORIZE_BASE_URL` / `RERANK_API_KEY` /
        `RERANK_BASE_URL` — only if the integrated baseline system requires
        them (evermemos does — uses Sophnet for embeddings and SiliconFlow
        for rerank; most candidates will not).
      - `MONGODB_HOST` is NOT required here — `setup.sh` writes a stub
        value to `.env` so the harness boots. Auto-bench candidates never
        touch EverOS Mongo. Set it only if you plan to run an integrated
        system (e.g. evermemos) from the same routine, which this routine
        is not designed for.

## 5. Post-enable guardrails

- After the first scheduled run, visit the resulting PR and Gmail. If the PR
  body contains a `[coverage-gap]`, `[install-failed]`, `[oom-batched]`,
  `[zero-score]`, or `[smoke-failed]` tag, investigate before letting the
  next day's run happen.
- If **7 consecutive days** produce empty discovery, widen the discovery
  sources in `.claude/skills/discover-memory-frameworks/SKILL.md`.
- If the daily-run budget is hit (5 on Pro, 15 on Max 5x, 30 on Max 20x),
  either reduce to every 2–3 days or upgrade plan.

## Troubleshooting

| Symptom | First thing to check |
|---|---|
| Routine exits immediately with "Missing required env vars" | Routine env section is empty or keys are set on the wrong routine |
| Every candidate smokes but scores 0 | `_search_single_user` isn't returning the candidate's actual results; run the adapter manually against a known LoCoMo conv |
| Multi-batch run fails at merge with "COVERAGE GAP" | One batch silently failed — check batch subdir for a missing `eval_results.json`; rerun that batch with its own explicit `--output-dir` |
| PR opens with 0-byte metrics block | Bench skill's canonical artifact wasn't produced; re-run with `AUTO_BENCH_VERBOSE=1` (if set) or tail the session log |
| Docker containers remain after teardown | The candidate's compose project name was not `auto-bench-<name>`; fix in write-eval-adapter skill |
