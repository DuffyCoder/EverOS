# OpenClaw adapter for EverMemOS evaluation

This adapter plugs the **OpenClaw memory backend** into the EverMemOS
`Add → Search → Answer → Evaluate` pipeline, letting us score OpenClaw
with the same dataset loader, stage protocol, and judge as `mem0`, `memos`,
`evermemos`, etc. Prompt ownership depends on `answer_mode`: `shared_llm`
uses the benchmark answer prompt, while `agent_local` sends the raw question to
the real `openclaw agent --local` loop.

It is **not** a faithful reproduction of OpenClaw running in production.
Call it what it is: *OpenClaw retrieval stack embedded in the unified
benchmark protocol*.

## Runtime ownership boundary

`evaluation/` owns the benchmark protocol, the OpenClaw adapter, and the
shared plugin/manifest contracts. `openclaw-eval/` owns the container and
image-build implementation and imports those shared contracts. When
`--build-missing` is requested, evaluation obtains the `openclaw-docker` base
argv from `evaluation/config/runtime_registry.yaml` instead of hard-coding the
harness's internal path in Python, then appends the existing plugin and
manifest flags.

This is a declarative boundary for build invocation, not a claim that the
current adapter is runtime-agnostic. The adapter behavior, runtime id, build
flags, and manifest resolution are still specific to OpenClaw; only the
builder location is supplied by the registry.

System selection is by public registry ID, not by a physical YAML filename.
See the [system-configuration guide](system-configs/README.md) for registry and
inheritance rules, and the [OpenViking notes](system-configs/openviking.md) for
the session-bundle ingest, tenant, timeout, and image invariants.

For routine benchmark runs, select a `canonical` / `active` ID from that
catalog. `openclaw-hybrid` remains a compatibility alias for `openclaw`: the
CLI displays the resolution, metadata records both IDs, and the default result
directory continues to use the requested alias. Experimental, ablation, and
tooling presets emit an experimental warning; deprecated entries name their
replacement.

## Portable local OpenViking runner

The repository-root `build.sh` is a compatibility wrapper around
`openclaw-eval/scripts/run_openviking_local_eval.sh`. It may be invoked from
any working directory. The historical positional form remains valid:

```bash
./build.sh RUN_NAME [evaluation arguments...]
```

Inspect the resolved paths and forwarded arguments without installing plugin
dependencies, stopping a server, deleting `.ovdata`, polling health, or
starting an evaluation:

```bash
./build.sh --dry-run RUN_NAME --from-conv 0 --to-conv 1
```

A real run stops the process listening on port 1933 and resets
`OPENVIKING_FORK/.ovdata`. Non-interactive callers must explicitly authorize
that work with `--yes-reset`; an interactive caller may instead type exactly
`yes` at the prompt. Required directories, configuration files, and
executables are validated before the existing server is stopped. Filesystem
overrides are normalized to absolute canonical paths before dry-run output or
confirmation, with relative paths interpreted from the caller's working
directory. A destructive run refuses `/`, requires the fork's
`pyproject.toml` and `openviking/` checkout markers, and rejects any external
path placed below the reset target.

```bash
./build.sh --yes-reset RUN_NAME --from-conv 0 --to-conv 1
# The control flag is also accepted after RUN_NAME for positional compatibility:
./build.sh RUN_NAME --yes-reset --from-conv 0 --to-conv 1
```

Defaults are derived from the checkout and can be overridden without editing
the script:

| Environment variable | Portable default |
|----------------------|------------------|
| `OPENVIKING_FORK` | `OpenViking-fork` beside this repository |
| `OPENVIKING_PLUGIN_DIR` | `$OPENVIKING_FORK/examples/openclaw-plugin` |
| `OPENVIKING_CONFIG` | `$HOME/.openviking/ov.local.conf` |
| `OPENVIKING_SERVER_BIN` | `$OPENVIKING_FORK/.venv/bin/openviking-server` |
| `EVAL_PYTHON` | this repository's `.venv/bin/python` |
| `EVAL_SYSTEM` | `openclaw-docker-openviking-session-bundle-noop` |
| `EVAL_LOG_DIR` | this repository's `.runlogs` |
| `TCMALLOC_PATH` | unset; no allocator is preloaded |

The source server still starts with direct provider access: inherited HTTP,
HTTPS, and all-proxy variables are removed only from the server process. The
runner selects the old listener with an exact port-1933 `ss` query, requires
its unique PID to stop and release the port, then verifies that the healthy
listener belongs to the newly launched server PID. It retains the plugin bind
mount, LoCoMo dataset, and all trailing evaluation CLI arguments.

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
| Status check | `openclaw memory status --json`; the sandbox does not promote `visibility_state` to `settled` until OpenClaw confirms the index state. |

## Approximate, with documented divergence

| Concern | OpenClaw native | This adapter |
|---------|-----------------|--------------|
| Memory flush (`flush_mode: "shared_llm"`) | Agent-runner-memory triggers an in-turn flush agent when the conversation crosses a token threshold (`buildMemoryFlushPlan`). Uses the agent's own LLM. | Runs once per session at ingest time with the benchmark's shared LLM provider and a prompt *modelled on* (not copied from) `buildMemoryFlushPlan`. OpenClaw's own `compaction.memoryFlush.enabled` is kept **off** so search never re-flushes. |
| Answer prompt | OpenClaw agents have their own system prompts per agent definition. | `shared_llm` replaces the agent answer path with the benchmark prompt (`prompts.yaml -> online_api.default.answer_prompt_mem0`); `agent_local` sends the raw question through OpenClaw's own agent loop. |
| Session bucketing | OpenClaw buckets markdown by date (`YYYY-MM-DD.md`). Single date can mix multiple sessions. | One file per session (`session-<SX>-<date>.md`) so `source_sessions` can be derived from the path alone. |
| Search concurrency | Unrestricted; OpenClaw uses its own sqlite WAL concurrency. | Per-conversation async semaphore (`max_inflight_queries_per_conversation`, default 1) because each query spawns a cold Node subprocess. |

## Explicitly omitted

| Concern | Why |
|---------|-----|
| Dreaming (light/REM/deep consolidation) | Cron-driven async promotion. No meaningful tick in a batch benchmark. |
| Short-term promotion into `MEMORY.md` | Requires real usage-signal history. |
| Mid-turn compaction / pre-compaction flush | Requires a running agent loop. Benchmark feeds transcripts as a whole. |
| `memory promote` / `memory promote-explain` CLIs | Same reason as above. |
| OpenClaw's internal answer prompt in `shared_llm` mode | Replaced by the shared benchmark prompt for cross-system comparability. It remains active in `agent_local` mode. |

## `flush_mode` values

| Value | Behaviour | Used when |
|-------|-----------|-----------|
| `disabled` | Raw session transcript dumped as markdown bullets. Matches v0.1/v0.2 ingestion exactly. | The `openclaw-fts-noflush`, `openclaw-vector-noflush`, and `openclaw-hybrid-noflush` public presets. |
| `shared_llm` | Framework LLM distils each session into retention-worthy bullets before OpenClaw indexes them. | The `openclaw`, `openclaw-fts`, and `openclaw-vector` public presets. |
| `session_bundle` | With memory-core, each LoCoMo sub-session goes through `agent_run` and its local transcript is archived before the next bundle. OpenViking combinations also use host-side SDK ingest; an OV-only `memory_mode: noop` preset uses only that SDK path and skips the memory-core agent/index work. | The registered Memcore and OpenViking session-bundle families. |

## Deciding which preset to use

- `openclaw-fts-noflush` ← cheapest, best for wiring smokes. Byte-for-byte comparable with v0.1.
- `openclaw-hybrid-noflush` ← measures the pure impact of adding sophnet embeddings without confounding LLM flush.
- `openclaw` (`openclaw-hybrid` is its compatibility alias) ← full stack. Closest to production but with documented divergences above.

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
