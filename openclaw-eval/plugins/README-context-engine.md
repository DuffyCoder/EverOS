# Authoring a Context-Engine Plugin

This is the second plugin shape supported by the eval framework.

| Shape | What | When to use |
|---|---|---|
| **memory** | Plugin registers `MemoryPluginRuntime` + `memory_search` / `memory_get` tools. Agent **explicitly retrieves** via tool calls. | Most plugins on npm/clawhub today. mem0, evermemos, memory-core, zep. |
| **context-engine** | Plugin registers a `ContextEngine` factory. Agent is **transparent** — no tool calls; the engine's `assemble()` injects context every turn, optionally adds a `systemPromptAddition`. | Compaction/transcript-management plugins such as OpenViking, HyperCompositor, stub-engine, and OpenClaw's built-in `legacy` engine. |

The active evaluation contracts are the
[system-configuration guide](../../evaluation/docs/system-configs/README.md),
[OpenViking operational notes](../../evaluation/docs/system-configs/openviking.md),
and the [OpenClaw adapter guide](../../evaluation/docs/openclaw_adapter.md).

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

The `ContextEngine` type is in `src/context-engine/types.ts` of the OpenClaw
checkout used to build the evaluation image.

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

The bridge contains a pure `dispatchEngineImport` helper that mirrors this
precedence for contract tests. The native `engine_import_history` RPC is **not
wired** because the OpenClaw distribution does not expose the required engine
loader through a stable export; native calls return `ok: false`. Current
adapters therefore do not use that RPC to replay benchmark history. Do not
claim that a context engine receives imported history merely because its slot
is configured.

## Slot wiring at eval time

The eval framework renders `slots.contextEngine` in
`/workspace/openclaw.docker.json` at container startup. Your yaml
declares which plugin to bind:

```yaml
# evaluation/config/systems/experiments/openclaw-docker-<your-plugin>.yaml
extends: "_bases/openclaw-docker.yaml"

openclaw:
  memory_mode: "noop"                   # no memory plugin needed
  context_engine_mode: "your-plugin"    # drives slots.contextEngine
  flush_mode: "disabled"                # the engine owns compaction
  agent_llm: { ... }

openclaw_docker:
  image: "openclaw-eval:<sha>-<your-plugin>-<rev>-slim"
  ...
```

Register the public system id and categorized path in
`evaluation/config/systems/index.yaml`; do not add a new root-level system
yaml. The image must be a concrete tag (normally one recorded in
`evaluation/config/image_manifest.yaml`); build placeholders are rejected by
the system-config policy.

The adapter env-emits `CONTEXT_ENGINE_PLUGIN_ID=your-plugin` and the
container entrypoint conditionally injects `slots.contextEngine` into
the rendered openclaw config.

## Session ID strategy

Context engines are **session-keyed**: `ingest()` and `assemble()` both take a
`sessionId`. The current evaluation routing depends on the preset:

- OpenViking session-bundle presets create one OV session UUID per benchmark
  conversation. Host-side SDK ingest adds every LoCoMo sub-session to that UUID
  and commits and waits after each sub-session. QA-time `agent_run` reuses the
  same UUID, so OpenViking `assemble()` sees the committed conversation state.
- After every Docker QA, the adapter archives the local OpenClaw session JSONL.
  The next question starts with an empty short-term transcript even though the
  OpenViking server state for the shared UUID remains available.
- Presets without an OV session use `session_id = f"{conv_id}__{qid}"` for each
  QA, and the Docker adapter archives that JSONL on every exit path. During
  non-OV `session_bundle` ingest, each sub-session uses the conversation ID and
  is archived before the next bundle; persisted memory files remain.

This means the older blanket description of context engines as sharing a
conversation transcript across QAs is no longer accurate. Non-OpenViking
context engines also do not gain historical ingest from the unwired
`engine_import_history` RPC. Treat their registered presets as experimental
until their own ingest and comparison contract is documented and tested.

## Tools your plugin should NOT register

Don't register `memory_search` or `memory_get`. Those are memory-plugin
tools and the prompt template assumes they're called explicitly. Context
engines are LLM-transparent; if you need to expose retrieval to the LLM
as a tool, you're authoring a memory plugin, not a context engine.

## Smoke gate

Copy `openclaw-eval/harness/stub_engine_passphrase_gate.sh` as a per-plugin
starting template. The current script hard-fails unless the rendered
`plugins.slots.contextEngine` is `stub-engine`, the bridge exits successfully
with `ok: true`, and the agent reply contains the sentinel. It also reports
`system_prompt_chars`; a low value is a warning that `assemble()` may not have
run, not an independent hard assertion. The reply sentinel is the
load-bearing end-to-end content check.

## Where to put your plugin

| Mode | Where | When |
|---|---|---|
| Bundled | `openclaw-eval/plugins/<your-plugin>/` | Self-authored plugins, forks needing source-level changes |
| npm install | Register `type: npm` in `evaluation/config/plugin_registry.yaml`, then select `--context-engine <id>@<version>` | Officially published plugins tested as-is. Use `--plugin-spec <id>=npm:...` only for an explicit source override; `--install-spec` is deprecated. See [install-mode onboarding](README-install-mode.md). |

Both modes route through the same slot-wiring + entrypoint render
pipeline once the plugin is loaded.
