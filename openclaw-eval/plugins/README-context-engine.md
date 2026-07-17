# Authoring a Context-Engine Plugin

This is the second plugin shape supported by the eval framework.

| Shape | What | When to use |
|---|---|---|
| **memory** | Plugin registers `MemoryPluginRuntime` + `memory_search` / `memory_get` tools. Agent **explicitly retrieves** via tool calls. | Most plugins on npm/clawhub today. mem0, evermemos, memory-core, zep. |
| **context-engine** | Plugin registers a `ContextEngine` factory. Agent is **transparent** — no tool calls; the engine's `assemble()` injects context every turn, optionally adds a `systemPromptAddition`. | Compaction/transcript-management oriented plugins. Honcho, hindsight, openclaw's built-in `legacy` engine. |

For the design rationale see
`docs/superpowers/specs/2026-04-30-plugin-kinds-design-note.md`.
For the eval-framework implementation plan see
`docs/superpowers/plans/2026-04-30-stage3-context-engine-support.md`.

## Manifest

`openclaw-eval/plugins/<your-plugin>/openclaw.plugin.json`:

```json
{
  "id": "your-plugin",
  "kind": "context-engine",
  "configSchema": {}
}
```

`kind` accepts:
- `"context-engine"` — pure context engine.
- `["memory", "context-engine"]` — dual-kind. Both capabilities activate
  only when the plugin is selected in their respective slot
  (`plugins.slots.memory` / `plugins.slots.contextEngine`). The dual-kind
  asymmetry to be aware of:
  - **memory side**: gated at registration by `record.memorySlotSelected`.
    Loader logs "dual-kind plugin not selected for memory slot; skipping
    memory capability registration" when not selected.
  - **context-engine side**: factory always registers; selection happens
    later when `resolveContextEngine()` reads `slots.contextEngine` and
    looks up the registered factory.

## Required exports

`openclaw-eval/plugins/<your-plugin>/index.ts`:

```ts
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import type { ContextEngine } from "openclaw/plugin-sdk/context-engine";

export default definePluginEntry({
  register(api) {
    api.registerContextEngine("your-plugin", () => makeYourEngine());
  },
});

function makeYourEngine(): ContextEngine {
  return {
    info: { id: "your-plugin", name: "Your Engine" },

    // Required
    async ingest(params) {
      // params.sessionId, params.message, optional sessionKey
      // store into your engine's session-keyed state
      return { ingested: true };
    },

    async assemble(params) {
      // params.sessionId, params.messages, params.tokenBudget, etc.
      // return the full ordered message array your engine wants the LLM to see
      return {
        messages: params.messages,             // or rewritten/compacted
        estimatedTokens: countTokens(params.messages),
        systemPromptAddition: undefined,        // optional; appears before system prompt
      };
    },

    async compact(params) {
      // optional but commonly useful; signal whether compaction happened
      return { ok: true, compacted: false, reason: "no-op" };
    },

    // Optional
    async ingestBatch(params) { /* one batch of new messages per turn */ },
    async afterTurn(params) { /* canonical persistence + compaction trigger; preferred over ingestBatch */ },
    async bootstrap(params) { /* import session DAG file once */ },
    async maintain(params) { /* post-bootstrap or post-turn maintenance */ },
    async dispose() { /* free resources */ },
  };
}
```

The `ContextEngine` type is in
`/Data3/shutong.shan/openclaw/repo/src/context-engine/types.ts`.

## Production write-hook precedence

When the openclaw runtime finalizes a turn it dispatches in this order
(per `attempt.context-engine-helpers.ts:110`):

1. `engine.afterTurn(...)` if present — canonical write hook; the engine
   does its own persistence + compaction decisions. **Preferred** for
   production engines because they get the full turn context (sessionFile,
   prePromptMessageCount, tokenBudget, runtimeContext).
2. `engine.ingestBatch(...)` if `afterTurn` absent and `ingestBatch` present
   — engine receives just the new messages this turn produced.
3. Per-message `engine.ingest(...)` for each new message if neither
   batch hook is present — slowest path.

The eval framework's `engine_import_history` bridge RPC mirrors this
precedence when replaying historical conversations into the engine
(Phase 3 of the Stage 3 plan).

## Slot wiring at eval time

The eval framework renders `slots.contextEngine` in
`/workspace/openclaw.docker.json` at container startup. Your yaml
declares which plugin to bind:

```yaml
# evaluation/config/systems/experiments/openclaw-docker-<your-plugin>.yaml
extends: "_bases/openclaw-docker.yaml"

openclaw:
  memory_mode: "noop"                   # no memory plugin needed
  context_engine_mode: "your-plugin"    # NEW field — drives slots.contextEngine
  flush_mode: "disabled"                # the engine owns compaction
  agent_llm: { ... }

openclaw_docker:
  image: "openclaw-eval:<sha>-<your-plugin>-<rev>-slim"
  ...
```

Register the public system id and categorized path in
`evaluation/config/systems/index.yaml`; do not add a new root-level system
yaml.

The adapter env-emits `CONTEXT_ENGINE_PLUGIN_ID=your-plugin` and the
container entrypoint conditionally injects `slots.contextEngine` into
the rendered openclaw config.

## Session ID strategy

Context engines are **session-keyed**: both `ingest()` and `assemble()`
take `sessionId`. The eval framework's first context-engine onboarding
uses **R2 routing** (conversation-level session id):

- Ingest writes under `session_id = conv_id`.
- All QA in the conversation share `session_id = conv_id` for `assemble()`.

This is **wiring/prototype** territory, not a comparable benchmark
against the memory plugin matrix:
- Memory plugins use per-QA isolation: `session_id = f"{conv_id}__{qid}"`
  (see `openclaw_adapter.py:414`, "v0.6: per-QA isolation").
- R2 lets answer to QA n leak into context for QA n+1.
- Context-engine scorecards under R2 are reported separately, with an
  explicit "not directly comparable" footnote.

R1 (replicate per QA) and R3 (engine fork primitive) are deferred to
Stage 4 if/when comparable numbers are needed.

## Tools your plugin should NOT register

Don't register `memory_search` or `memory_get`. Those are memory-plugin
tools and the prompt template assumes they're called explicitly. Context
engines are LLM-transparent; if you need to expose retrieval to the LLM
as a tool, you're authoring a memory plugin, not a context engine.

## Smoke gate

The framework provides `openclaw-eval/harness/<plugin>_engine_passphrase_gate.sh`
as a per-plugin starting template. Pass criteria (per Codex r3):

1. `resolveContextEngine()` returns the expected engine id (proves slot
   wiring resolved correctly).
2. `assemble().messages` or `systemPromptAddition` contains the test
   passphrase (proves the engine actually injected the ingested context;
   non-empty messages alone is insufficient, since `assemble()` echoing
   its `messages` input back trivially produces non-empty output).
3. The agent reply contains the passphrase (end-to-end success).

All three criteria must hold for the gate to pass. Failing any one
emits a diagnostic and exits non-zero.

## Where to put your plugin

| Mode | Where | When |
|---|---|---|
| Bundled | `openclaw-eval/plugins/<your-plugin>/` | Self-authored plugins, forks needing source-level changes |
| Install | `npm:<your-plugin>@<version>` via `--install-spec` | Officially-published plugins you test as-is. See `openclaw-eval/plugins/README-install-mode.md`. |

Both modes route through the same slot-wiring + entrypoint render
pipeline once the plugin is loaded.
