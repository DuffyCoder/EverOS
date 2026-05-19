# LoCoMo Fair-Baseline Benchmark (A1 / B2 / B1)

How to reproduce the OpenClaw memcore / OpenViking / OpenViking+memcore
LoCoMo numbers on a fair, internally-consistent stack.

## What gets compared

Three system configurations that share **everything except the system
under test**. Only the memory plugins differ.

| Label | yaml                                                                    | What it tests       | OV official |
| ----- | ----------------------------------------------------------------------- | ------------------- | ----------- |
| A1    | `openclaw-docker-memcore-session-bundle.yaml`                           | memcore only        | 35.65%      |
| B2    | `openclaw-docker-openviking-session-bundle-noop.yaml`                   | OV only             | 52.08%      |
| B1    | `openclaw-docker-openviking-session-bundle-memcore.yaml`                | OV + memcore        | 51.23%      |

The reference numbers come from OpenViking/README.md and were produced
with `seed-2.0-code` as the agent LLM via the upstream
[openclaw-eval](https://github.com/ZaynJarvis/openclaw-eval) harness.

## Shared baseline (locked for fairness)

Every system uses the same stack so a delta between them can only come
from the memory plugin:

- Agent LLM: `gpt-4.1-mini` via Sophnet
- Embedding: Sophnet `text-embeddings` 1024-dim
- Rerank: SiliconFlow `BAAI/bge-reranker-v2-m3` (configured in
  `~/.openviking/ov.conf`)
- Judge LLM: `gpt-4.1-mini` via Sophnet (`evaluation/config/datasets/locomo.yaml`)
- Dataset: LoCoMo10 with `category_filter: [5]` (matches official; 1540
  cases evaluated)
- num_runs: 3 (judge each Q three times, average)
- Aggregation: per-run `correct / (total - None)`; rate-limit failures
  return `Optional[bool] None` and are excluded from the denominator.
  The judge's built-in retry (`max_retries` in the system config under
  `evaluator.llm_judge`) handles transient errors; if a run still leaves
  many None verdicts, re-run only the `evaluate` stage with a higher
  `max_retries`.

## Memory-plugin-specific knobs

| Setting                                  | A1 (memcore) | B2 (OV)   | B1 (OV+memcore) |
| ---------------------------------------- | ------------ | --------- | --------------- |
| `memory_mode`                            | memory-core  | noop      | memory-core     |
| `context_engine_mode`                    | (unset)      | openviking| openviking      |
| `ingest_session_tail` (drives file_write)| medium       | ""        | medium          |
| OV SDK ingest (`ov_ingest.user_id_template`) | n/a       | `""` (shared) | `""` (shared) |

The medium tail is:
```
[End of session. Use the write tool to save key facts from this
 conversation to memory/<date>.md before replying.]
```

This is the "fair compromise" between the openclaw-eval README's weak
tail (which `seed-2.0-code` honors but `gpt-4.1-mini` ignores → 0
memory writes) and our previous 230-character explicit directive
(which over-elicits → memcore inflated to 0.51 above OV's 0.36). With
the medium tail, `gpt-4.1-mini` writes ~6-8 memory bullets per LoCoMo
session.

`user_id_template: ""` matches OV team's `import_to_ov.py
--no-user-agent-id` flag — both SDK ingest and the in-container OV
plugin read from the OV server's default namespace. Cross-conversation
name collisions are resolved by the rerank stage at QA time.

## Prerequisites

1. **Docker images built** with the openclaw-eval container code:
   - `openclaw-eval:7da23c3-memory-core-0000000-slim` (A1)
   - `openclaw-eval:7da23c3-openviking-b7e6bcb-slim` (B1, B2)
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

Memory constraint: each system needs ~8 GB for its docker containers
plus ~10 GB for the EverMemOS infra. Three systems concurrently exceed
the 16 GB budget on standard nodes, so run serial:

```bash
for sys in \
  openclaw-docker-memcore-session-bundle \
  openclaw-docker-openviking-session-bundle-noop \
  openclaw-docker-openviking-session-bundle-memcore; do
  uv run python -m evaluation.cli --dataset locomo \
    --system "$sys" --run-name "fair-${sys}"
done
```

Estimated time per system on a single 16 GB node: 3-4 h. The judge's
built-in retry (`max_retries` in system yaml) absorbs transient Sophnet
rate-limit hiccups; if a run still leaves many None judgments, re-run
only the `evaluate` stage with a higher `max_retries`.

## What numbers to expect

Reproducing with `gpt-4.1-mini` instead of `seed-2.0-code` introduces a
model-driven offset that we cannot eliminate without doubao API access.
Expect:

- **A1** ≈ 0.40-0.50. Above OV's 0.36 because `gpt-4.1-mini` writes
  more memory per session than `seed-2.0-code` did under the same tail.
- **B2** ≈ 0.45-0.55. Closer to OV's 0.52 because OV does the memory
  work; the agent's job is just QA retrieval.
- **B1** ≈ 0.45-0.55. Typically ≤ B2 (memcore additions don't help OV
  on commonsense / open questions and may dilute retrieval).

Numbers within each system are directly comparable to one another
because the entire stack — model, embedding, rerank, judge, dataset
filter, aggregation — is locked.

## Caveats and known limitations

- `compaction.memoryFlush` autoCapture is structurally unviable for
  LoCoMo: openclaw's gate is `threshold = contextWindow - reserveTokens
  - softThreshold`, which yields a 104k-token trigger on gpt-4.1-mini's
  128k window. A single ~5k session bundle never crosses it. Memcore
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
