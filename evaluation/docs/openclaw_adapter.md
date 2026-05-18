# OpenClaw adapter for EverMemOS evaluation

This adapter plugs the **OpenClaw memory backend** into the EverMemOS
`Add → Search → Answer → Evaluate` pipeline, letting us score OpenClaw
with the same prompt + judge + dataset loader as `mem0`, `memos`,
`evermemos`, etc.

It is **not** a faithful reproduction of OpenClaw running in production.
Call it what it is: *OpenClaw retrieval stack embedded in the unified
benchmark protocol*.

The fidelity/comparability tradeoff is explicit. Three parts:

## Strictly faithful to OpenClaw

| Concern | What we use |
|---------|-------------|
| Storage format | `memory/*.md` markdown files under the workspace |
| Index build | `openclaw memory index --force` via the Node bridge |
| Retrieval | `openclaw memory search --json` (FTS, vector, or hybrid depending on `backend_mode`) |
| Config schema | Real OpenClaw `openclaw.json` written to `OPENCLAW_CONFIG_PATH` |
| Per-conversation isolation | Own `workspace`, `state_dir`, `home`, `cwd` — matches how v0.1/v0.2 bench adapters isolated runs |
| Embedding provider | sophnet via OpenClaw's native `memorySearch.remote` config (for `vector` / `hybrid` modes) |
| source_sessions projection | `memory/session-<SX>-<date>.md` filenames — FTS hits project back to session ids for cross-system retrieval metrics |
| Status check | `openclaw memory status --json`; sandbox refuses to promote `visibility_state` to `settled` unless OpenClaw confirms (plan Review-driven revision #3) |

## Approximate, with documented divergence

| Concern | OpenClaw native | This adapter |
|---------|-----------------|--------------|
| Memory flush (`flush_mode: "shared_llm"`) | Agent-runner-memory triggers an in-turn flush agent when the conversation crosses a token threshold (`buildMemoryFlushPlan`). Uses the agent's own LLM. | Runs once per session at ingest time with the benchmark's shared LLM provider and a prompt *modelled on* (not copied from) `buildMemoryFlushPlan`. OpenClaw's own `compaction.memoryFlush.enabled` is kept **off** so search never re-flushes. |
| Answer prompt | OpenClaw agents have their own system prompts per agent definition. | Reuses the shared benchmark answer prompt (`prompts.yaml -> online_api.default.answer_prompt_mem0`) so OpenClaw answers are directly comparable with mem0/memos/etc. |
| Session bucketing | OpenClaw buckets markdown by date (`YYYY-MM-DD.md`). Single date can mix multiple sessions. | One file per session (`session-<SX>-<date>.md`) so `source_sessions` can be derived from the path alone. |
| Search concurrency | Unrestricted; OpenClaw uses its own sqlite WAL concurrency. | Per-conversation async semaphore (`max_inflight_queries_per_conversation`, default 1) because each query spawns a cold Node subprocess. |

## Explicitly omitted

| Concern | Why |
|---------|-----|
| Dreaming (light/REM/deep consolidation) | Cron-driven async promotion. No meaningful tick in a batch benchmark. |
| Short-term promotion into `MEMORY.md` | Requires real usage-signal history. |
| Mid-turn compaction / pre-compaction flush | Requires a running agent loop. Benchmark feeds transcripts as a whole. |
| `memory promote` / `memory promote-explain` CLIs | Same reason as above. |
| OpenClaw's internal answer prompt | Replaced by the shared benchmark prompt for cross-system comparability. |

## `flush_mode` values

| Value | Behaviour | Used when |
|-------|-----------|-----------|
| `disabled` | Raw session transcript dumped as markdown bullets. Matches v0.1/v0.2 ingestion exactly. | Ablation preset (`*-noflush.yaml`) — for direct comparison against the historical bench adapters. |
| `shared_llm` | Framework LLM distils each session into retention-worthy bullets before OpenClaw indexes them. | Main `openclaw.yaml` + `*-fts.yaml` / `*-vector.yaml` / `*-hybrid.yaml` — the "closest to OpenClaw production lifecycle, given benchmark constraints" preset. |

## Deciding which preset to use

- `openclaw-fts-noflush.yaml` ← cheapest, best for wiring smokes. Byte-for-byte comparable with v0.1.
- `openclaw-hybrid-noflush.yaml` ← measures the pure impact of adding sophnet embeddings without confounding LLM flush.
- `openclaw-hybrid.yaml` (`openclaw.yaml` main preset) ← full stack. Closest to production but with documented divergences above.

## Cross-QA short-term isolation (OV / openclaw-eval alignment)

OV LoCoMo presets use **one OV `session_id` (UUID) per conversation** for
SDK ingest and QA-time `before_prompt_build` / assemble. OpenClaw's
on-disk transcript is still keyed by that same `--session-id` passed to
`agent_run`.

After each QA the docker adapter calls bridge `archive_session`, which
renames `state/agents/main/sessions/<session_id>.jsonl` to
`<session_id>.jsonl.<ts>`. The next QA reuses the same `session_id` but
starts with an empty short-term buffer. OV server-side state keyed by the
UUID is **not** cleared by the rename.

Without `archive_session`, later QAs in the same conv would accumulate
prior Q/A turns in the jsonl and inflate `final_context_tokens` (Tier B
transcript estimate) as well as the live LLM prompt.

## `final_context_tokens` diagnostics (`answer_mode: agent_local`)

The harness reports `final_context_tokens_mean` / `final_context_tokens_stats`
in `report.txt` Diagnostics. Semantics depend on the adapter answer path:

| Adapter answer path | `final_context_tokens` meaning |
|---------------------|--------------------------------|
| **OpenClaw `agent_local`** (incl. `openclaw-docker`) | Last LLM hop **prompt-side** tokens (`lastCallUsage` / session assistant `usage`, including cache read/write when present). Fallback: provider usage → session jsonl usage → **session transcript tiktoken** (system/tool overhead + messages before the final assistant turn) → coarse estimate (system + schema + harness question only). |
| **EverMemOS / search-then-answer** | tiktoken estimate of the **retrieved context string** passed to the shared answer LLM (not the full agent prompt). |

Also recorded for OpenClaw agent runs:

- `agent_run_total_input_tokens` — sum of prompt-side tokens across all LLM hops.
- `final_context_tokens_source` / `agent_run_total_input_tokens_source` — provenance
  (`last_call_usage`, `session_jsonl_*`, `session_transcript_*`, `prompt_estimate`).

**Cross-system comparability:** OpenClaw agent_local counts are typically
much larger (workspace bootstrap, tool schemas, context-engine assemble).
Use them to compare OpenClaw plugin presets (OV vs memory-core vs noop).
Do **not** treat them as directly comparable to EverMemOS
`final_context_tokens` without reading both definitions.

## Bugs the benchmark is *not* designed to catch

- **LLM judge leniency.** Cross-mode sweeps on LoCoMo conv 9 showed gpt-4o-mini accepting `"Sep 2023"` as matching the gold `"Mar 2023"`. Consider adding a stricter exact-match sanity rail before trusting accuracy deltas below ~5%.
- **Small-smoke retrieval noise.** `--smoke-messages 20` loads only 1–2 sessions out of 50+ per conversation, so gold sessions often do not even live in memory. Retrieval metrics under that setting primarily measure "what is in the smoke subset", not retrieval quality. Use `--smoke-messages` large enough to cover the gold sessions, or skip `--smoke` for full scoring.
