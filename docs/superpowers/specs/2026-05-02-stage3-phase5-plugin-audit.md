# Stage 3 Phase 5 — Real Context-Engine Plugin Audit & Selection

**Date:** 2026-05-02
**Status:** Plugin selected — operator-driven scorecard run pending
**Scope:** Pick a real, public openclaw context-engine plugin for the
first Stage 3 R2-routed prototype scorecard.

---

## Method

`npm search` over the public registry for `context-engine openclaw` keyword
combination; filter for `kind: "context-engine"` in the package's
`openclaw.plugin.json` block, recent activity (<60 days), and permissive
license. Inspect each candidate's `package.json#openclaw` metadata via
`npm view`. Confirm self-contained dependency graph (no proprietary
backend service requirements).

## Candidate matrix

| Package | Latest | Date | License | Kind block | Deps | Notes |
|---|---|---|---|---|---|---|
| `@psiclawops/hypercompositor` | 0.9.6 | 2026-05-01 | Apache-2.0 | `"kind": "context-engine"` ✅ | `@psiclawops/hypermem`, `zod` | **Selected.** Recent, clean ContextEngine-only focus, openclaw 2026.4.x compat, 232KB tarball, README documents store options |
| `@sonzai-labs/openclaw-context` | 1.3.3 | 2026-04-23 | MIT | (not in surface metadata) | bundled | Multi-feature (memory + CE + personality + fact extraction) — broader scope than R2 prototype needs |
| `openclaw-clarke` | 0.12.2 | 2026-04-02 | "SEE LICENSE IN LICENSE" | (memory + context engine combo) | unknown | License opaque; combined memory + CE — wrong shape for our slot model |
| `@memclaw/memclaw-context-engine` | 0.9.61 | 2026-04-15 | MIT | unknown | unknown | 8 versions, 0.9.x; recent but kind metadata not surfaced via npm view |
| `@fathippo/fathippo-context-engine` | 0.1.9 | 2026-03-15 | MIT | unknown | unknown | "Encrypted memory" — adds key management surface beyond Phase 5 prototype |
| `metainsight-context-engine` | 0.1.0 | 2026-03-20 | MIT | unknown | unknown | Cloud (Tencent COS) — external service dependency |
| `@pentatonic-ai/openclaw-memory-plugin` | 0.8.3 | 2026-04-24 | MIT | (memory keywords) | unknown | Self-described as memory plugin, not CE-first |

## Selection rationale: `@psiclawops/hypercompositor`

1. **Explicit `kind: "context-engine"`** in `openclaw.plugin.json` — confirms
   slot resolution will pick it up via Phase 2's `context_engine_mode`
   wiring without surprise.
2. **Recent**: published 2026-05-01, openclaw 2026.4.x compat (matches
   the openclaw build at `/Data3/shutong.shan/openclaw/repo`).
3. **Apache-2.0** — permissive, suitable for benchmark inclusion.
4. **Self-contained** — only `zod` + sister plugin `@psiclawops/hypermem`.
   `hypermem` uses sqlite-vec (local file) + optional redis; no
   network/cloud requirement for default config.
5. **Pure CE focus** — described as "context engine plugin", separate
   from the memory-store layer (`hypermem`).

## Architecture note: dual plugin install

`hypercompositor` declares `@psiclawops/hypermem` as a runtime dep. The
Stage 3 build pipeline must install BOTH. Two install strategies:

- **Install-spec mode** (`build.py --install-spec`): supply both via
  the install spec list:
  ```
  --install-spec npm:@psiclawops/hypermem@0.9.6
  --install-spec npm:@psiclawops/hypercompositor@0.9.6
  ```
  The entrypoint Phase 1 jq render adds both to `plugins.allow` and
  `plugins.entries`. Slot mapping:
  - `slots.memory = <hypermem-id>` (memory-side use)
  - `slots.contextEngine = hypercompositor`
- **Bundled mode**: copy both packages into `openclaw-eval/plugins/`
  before docker build. Heavier (workspace member churn) — not preferred.

## Ingestion strategy (R2 routing)

Phase 3's empirical finding: the in-process `engine_import_history`
RPC native path is blocked (openclaw `dist/` doesn't expose
`resolveContextEngine` via stable plugin-sdk subpaths). Until that
upstream change lands, the realistic R2 ingestion is **CLI replay**
through the existing `agent_run` RPC:

```python
# Per-conversation ingest loop
for msg in conv.messages:
    await adapter.agent_run({
        "session_id": conv.id,
        "message": msg.text,
    })
# QA loop reuses the same session_id, engine's assemble() picks up
# the accumulated transcript
for q in conv.questions:
    answer = await adapter.agent_run({
        "session_id": conv.id,
        "message": q.text,
    })
```

**Token cost trade-off**: each ingest call elicits an LLM reply we
discard. For LoCoMo's ~50 turns per conv, the ingest phase costs
~50× the QA phase. With sophnet quota rotated 2026-04-30, the 50Q
prototype run is tractable but not cheap. R-S2-3 (judge fragility)
still applies; in-pipeline LLM judge concurrency 4 + retry recommended.

This routing leaves R-S2-1 from Stage 2 closure unchanged: scorecard
remains "wiring/prototype, NOT comparable" per Codex r3 caveat.

## Out of scope for this audit

- Side-by-side scorecard comparison across the 7 candidates — Stage 4.
- Performance / latency profiling — only correctness signal (replies
  contain expected facts) is validated in Phase 5.
- Memory-side scorecard regression while `slots.memory = hypermem` —
  Stage 1/2 closed against a different memory plugin set; cross-axis
  comparability with hypermem's memory side is Stage 4.

## Operator runbook (Phase 5 execution)

The actual scorecard run is operator-driven; the following are the
prerequisites and steps. None of them are wired into CI.

**Prereqs:**
- Docker engine + buildx
- `LLM_API_KEY` + `LLM_BASE_URL` (sophnet, post-2026-04-30 rotation)
- `SOPH_API_KEY` + `SOPH_EMBED_URL` + `SOPH_EMBED_EASYLLM_ID` (sophnet
  embedding for hypermem's vector store, if used)

**Steps:**

1. Build image with both plugins installed:
   ```bash
   python openclaw-eval/harness/build.py \
     --install-spec 'npm:@psiclawops/hypermem@0.9.6' \
     --install-spec 'npm:@psiclawops/hypercompositor@0.9.6' \
     --tag openclaw-eval:hypercompositor-0.9.6-slim
   ```

2. Adapt the smoke gate for hypercompositor (the `WOMBAT_42` sentinel
   in `stub_engine_passphrase_gate.sh` won't apply — hypercompositor's
   assemble() doesn't inject sentinels). Lower-criterion smoke:
   ```bash
   IMAGE=openclaw-eval:hypercompositor-0.9.6-slim \
   CONTEXT_ENGINE_PLUGIN_ID=hypercompositor \
   MEMORY_PLUGIN_ID=hypermem \
     ./openclaw-eval/harness/<adapt-from-stub-engine-gate>.sh
   ```
   Pass criterion: bridge response `ok=true` + `system_prompt_chars > 50`
   (proves hypercompositor's assemble() composed something non-trivial).

3. 1c1q LoCoMo (1 conv × 1 question) wiring smoke:
   ```bash
   .venv/bin/python -m evaluation.cli run \
     --system openclaw-hypercompositor \
     --dataset locomo \
     --run-name s3-hypercompositor-1c1q-r2 \
     --max-conversations 1 --max-questions-per-conv 1
   ```

4. 50Q LoCoMo prototype:
   ```bash
   .venv/bin/python -m evaluation.cli run \
     --system openclaw-hypercompositor \
     --dataset locomo \
     --run-name s3-hypercompositor-r2-prototype \
     --max-questions 50
   ```
   Expect ~50 × (1 + 50) = ~2550 LLM calls (50 QA + 50 × 50 ingest replays).
   Use `LLM_JUDGE_CONCURRENCY=4` + retry to avoid the Stage 2 R-S2-3 issue.

5. Capture results into `docs/superpowers/specs/2026-04-28-stage1-closure.md`
   under a "Stage 3 First Plugin" subsection per plan template; mark as
   "wiring/prototype, NOT comparable" with explicit footnote.

## Risks specific to this plugin

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| H-1 | hypercompositor's openclaw 2026.4.x compat doesn't match `/Data3/shutong.shan/openclaw/repo` build's resolved version | Medium | Verify openclaw version at `pnpm view openclaw version` in container before scorecard; fall back to npm install hypercompositor@0.9.5 if compat tightens |
| H-2 | hypermem's optional redis path activates by default and breaks in our isolated container | Low | Default config uses sqlite-vec; verify no `REDIS_URL` env leaks into container's allowed env list |
| H-3 | hypercompositor's assemble() doesn't fire under our minimal QA prompts (engine triggers may require min context size) | Medium | Smoke gate criterion 4b (system_prompt_chars > 50) catches this; falls back to alternative engine selection (sonzai-labs) if so |
| H-4 | Token cost of CLI replay exceeds sophnet quota again | Low | Quota rotated 2026-04-30, fresh; 50Q prototype's ~2550 calls is well under typical monthly cap |
