# LoCoMo Fair-Baseline Benchmark (A1 / B2 / B1)

How to reproduce the OpenClaw memcore / OpenViking / OpenViking+memcore
LoCoMo numbers on a fair, internally-consistent stack.

## What gets compared

These three registered presets preserve the historical A1/B2/B1 comparison
surface. They are not a one-variable ablation: in addition to memory wiring,
they retain different top-level model, timeout, tenant, concurrency, and image
settings. Treat the published scores as historical references, not as proof
that only the memory plugin caused the delta.

| Label | Public system ID | What it tests | OV official |
| ----- | ---------------- | ------------- | ----------- |
| A1 | `openclaw-docker-memcore-session-bundle` | memcore only | 35.65% |
| B2 | `openclaw-docker-openviking-session-bundle-noop` | OV only | 52.08% |
| B1 | `openclaw-docker-openviking-session-bundle-memcore` | OV + memcore | 51.23% |

These are registry IDs, not YAML filenames. See the
[system-configuration guide](../evaluation/docs/system-configs/README.md) and
[OpenViking operational notes](../evaluation/docs/system-configs/openviking.md)
for their current categorized paths and runtime invariants.

The reference numbers come from OpenViking/README.md and were produced
with `seed-2.0-code` as the agent LLM via the upstream
[openclaw-eval](https://github.com/ZaynJarvis/openclaw-eval) harness.

## Current locked evaluation surfaces

- OpenClaw agent model: `doubao-seed-2-0-code-preview-260215` via Sophnet in
  all three presets.
- Pipeline LLM model: the code-preview model in A1/B1 and
  `doubao-seed-2-0-pro-260215` in B2.
- OpenClaw embedding: Sophnet `text-embeddings` 1024-dim in A1/B1; B2 has no
  OpenClaw embedding block because memory-core retrieval is disabled.
- OpenViking embedding and rerank are server-side settings outside these
  presets and must be recorded with each run.
- Judge LLM: `doubao-seed-2-0-pro-260215` via Sophnet
  (`evaluation/config/datasets/locomo.yaml`).
- Dataset: LoCoMo10 with `filter_category: [5]` (matches official; 1540
  cases evaluated)
- num_runs: 3 (judge each Q three times, average)
- Aggregation: per-run `correct / (total - None)`; rate-limit failures
  return `Optional[bool] None` and are excluded from the denominator.

## Memory-plugin-specific knobs

| Setting                                  | A1 (memcore) | B2 (OV)   | B1 (OV+memcore) |
| ---------------------------------------- | ------------ | --------- | --------------- |
| `memory_mode`                            | memory-core  | noop      | memory-core     |
| `context_engine_mode`                    | (unset)      | openviking| openviking      |
| `ingest_session_tail` (drives file_write)| medium       | ""        | medium          |
| OV SDK user identity | n/a | `{conv_id}` template | fixed `eval-1` |
| OV SDK agent identity | n/a | `{conv_id}` template | server default |

The medium tail is:
```
[End of session. Use the write tool to save key facts from this
 conversation to memory/<date>.md before replying.]
```

The medium tail explicitly asks the agent to persist durable facts. Its exact
effect is model-dependent, so retain the literal string when reproducing the
locked presets.

For B2, both `user_id_template` and `agent_id_template` resolve to the
conversation ID. The host-side SDK ingest and the in-container plugin must
use the same tenant identity. B1 retains the fixed `user_id: eval-1` required
by its locked baseline.

## Prerequisites

1. **Docker images built** with the openclaw-eval container code. The image
   must bake the current `openclaw_eval_bridge.mjs`, which reads the LAST
   assistant payload via `findLast`; a stale image with the old `payloads[0]`
   bridge silently grades the preamble instead of the answer.
   - `openclaw-eval:7da23c3-memory-core-0000000-slim` (A1)
   - B1 uses the public GHCR image pinned by digest in its registered preset.
   - B2 names
     `openclaw-eval-plugins:7da23c3-openviking-2026.6.04-qidtrace-on-findlast-slim`;
     build that source-matched image locally, or publish and digest-pin it for
     cross-host reproduction. See the OpenViking operational notes above.
2. **EverMemOS infra running** via `docker-compose up -d`.
3. **OpenViking server running** on host port 1933 with `ov.conf`
   configured for Sophnet VLM/embedding and SiliconFlow rerank.
4. **`.env` populated** with `LLM_API_KEY`, `LLM_BASE_URL`,
   `SOPH_API_KEY`, `SOPH_EMBED_URL`, `SOPH_EMBED_EASYLLM_ID`,
   `OPENVIKING_API_KEY`.

## Run a single system

```bash
uv run python -m evaluation.cli \
  --dataset locomo \
  --system openclaw-docker-memcore-session-bundle \
  --run-name fair-A1-memcore
```

Output lands in
`evaluation/results/locomo-openclaw-docker-memcore-session-bundle-fair-A1-memcore/eval_results.json`.

## Run all three sequentially

Run the systems sequentially unless the host is sized for all three presets'
internal container concurrency:

```bash
for sys in \
  openclaw-docker-memcore-session-bundle \
  openclaw-docker-openviking-session-bundle-noop \
  openclaw-docker-openviking-session-bundle-memcore; do
  uv run python -m evaluation.cli --dataset locomo \
    --system "$sys" --run-name "fair-${sys}"
done
```

Runtime depends on provider quotas, host capacity, and OpenViking server
throughput. Record those inputs alongside the result.

## Interpreting results

The percentages in the first table are historical upstream references. A new
run is comparable only when its resolved system config, container digest,
OpenViking server commit/config, dataset, provider endpoints, and judge config
are all recorded. Compare repeated runs of the same public ID first; do not
attribute a cross-ID delta solely to memory architecture.

## Caveats and known limitations

- `compaction.memoryFlush` autoCapture is structurally unviable for
  LoCoMo: openclaw's gate is `threshold = contextWindow - reserveTokens
  - softThreshold`, which yields a 104k-token trigger with the configured
  128k context window. A single ~5k session bundle never crosses it. Memcore
  writes must therefore come from explicit `write` tool calls driven
  by the tail directive.

- Memory between `agent --local` invocations does not accumulate
  across LoCoMo sessions even within the same conversation: each
  `send_message` archives the prior session jsonl and starts fresh.
  Memory persists on disk under `/workspace/memory/<date>.md`; the
  agent reads via the `memory_search` tool at QA time.

- The docker image's baked-in `/eval/entrypoint.sh` is older than the
  current `openclaw-eval/container/entrypoint.sh` and does not honor
  `MEMORY_FLUSH_ENABLED`. The `_patch_memory_flush_enabled` post-spawn
  step in `openclaw_docker_adapter.py` works around this by editing
  `/workspace/openclaw.docker.json` in place after each container
  starts. Rebuilding the image with the current entrypoint.sh removes
  the need for the patch.
