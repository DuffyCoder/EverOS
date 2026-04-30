# Plugin Kinds — Memory vs Context-Engine

> **Date**: 2026-04-30
> **Status**: Design note (not yet a Stage 3 plan; informs Stage 3 scoping)
> **Trigger**: Survey of openclaw plugin types prompted by user question
> "对于 openclaw plugin 支持的是 memory 还是 context engine"

---

## TL;DR

Our evaluation framework currently supports **`kind: "memory"`** plugins
only. openclaw also defines **`kind: "context-engine"`** as a first-class
plugin shape with a different runtime contract. Several "memory" memory
products in the wild (hindsight, honcho, some hermes variants) are
actually context-engine-shaped — agent-transparent transcript managers
rather than tool-callable retrievers. To onboard them we need a
**second integration path** in the framework, not a copy of the existing
memory path.

---

## openclaw's Two Plugin Kinds

Source: `/Data3/shutong.shan/openclaw/repo/src/plugins/types.ts:113`

```ts
export type PluginKind = "memory" | "context-engine";
```

A plugin manifest declares one of these (or both for dual-kind plugins).
`registry.ts:255` allows `kind?: PluginKind | PluginKind[]`.

### kind: "memory"

- **Registration**: `api.registerMemoryCapability(...)` plus
  `api.registerTool(...)` for `memory_search` / `memory_get`.
- **Runtime contract**: `MemoryPluginRuntime` — 7 methods (index,
  search, get, readFile, status, probeEmbedding, probeVector).
- **Visibility to LLM**: agent **explicitly calls** `memory_search`
  via tool calls; plugin's `promptBuilder` injects a directive section
  ("Always call this when …") into the system prompt.
- **Slot wiring**: `plugins.slots.memory = "<plugin id>"` in
  the rendered openclaw config.
- **Examples**: memory-core, mem0, evermemos, zep, our `stub` test
  plugin.

### kind: "context-engine"

- **Registration**: `api.registerContextEngine(id, factory)`.
- **Runtime contract**: `ContextEngine` — required methods `ingest`,
  `assemble`, `compact`; optional `bootstrap`, `maintain`,
  `ingestBatch`, `afterTurn`, `prepareSubagentSpawn`,
  `onSubagentEnded`, `dispose`.
  Reference: `/Data3/shutong.shan/openclaw/repo/src/context-engine/types.ts`
- **Visibility to LLM**: **transparent**. The engine's `assemble()` is
  called every turn to produce the message array; engine can inject
  `systemPromptAddition` text but does **not** expose tools to the LLM.
  The agent has no idea a context engine is in the loop.
- **Slot wiring**: `plugins.slots.contextEngine = "<plugin id>"`.
- **Examples**: openclaw's built-in default context engine
  (manages auto-compaction); third-party: hindsight, honcho.

### Dual-kind plugins

A plugin can declare `kind: ["memory", "context-engine"]`. Both
capabilities only activate if the plugin is selected in BOTH slots.
`registry.ts` repeatedly logs `"dual-kind plugin not selected for
memory slot; skipping memory capability registration"` (and the
analog for context-engine) — selection is per-slot, not per-plugin.

---

## What Our Framework Currently Does

```
openclaw-eval/plugins/{stub,mem0,evermemos}/openclaw.plugin.json
  → "kind": "memory"

openclaw-eval/container/entrypoint.sh
  → renders plugins.allow / slots.memory / entries
  → does NOT render plugins.slots.contextEngine

openclaw-eval/container/openclaw.template.json
  → "plugins": {
        "allow": "__PLUGIN_ALLOW__",
        "slots": {"memory": "__MEMORY_SLOT__"},   ← no contextEngine
        "entries": "__PLUGIN_ENTRIES__"
     }

evaluation/src/adapters/openclaw_docker_adapter.py
  → drives: ingest (write session.md + index) → search → answer
  → answer phase: agent calls memory_search via tool, gets results,
    composes reply
```

The whole eval loop assumes the agent **explicitly retrieves**.
Latency metrics, retrieval-quality metrics, and the prompt-builders
all reflect that assumption.

---

## Why Context-Engine Plugins Don't Work As-Is

If a user dropped a context-engine plugin (e.g. hindsight) into
`openclaw-eval/plugins/<name>/`, our build pipeline would happily
build the image, but at runtime:

1. **`entrypoint.sh` only sets `slots.memory`** — even if the plugin
   declares `kind: "context-engine"`, slot wiring lands it as a
   memory plugin (which it cannot be, since it doesn't implement
   `MemoryPluginRuntime`). openclaw plugin loader rejects or no-ops.
2. **No `memory_search` / `memory_get` tools** — context-engine plugins
   don't register tools. `memory_search` would not exist; the agent
   cannot retrieve.
3. **Prompt builder mismatch** — our prompt-builders.ts assumes
   `availableTools.has("memory_search")`. Without those tools the
   prompt section is empty (per our own gating in
   `prompt-builders.ts`) and the agent gets no memory-related
   instructions.
4. **No `assemble()` invocation** — even if `slots.contextEngine` were
   set, openclaw's runtime calls `ContextEngine.assemble()` per turn;
   our adapter ingests via `add()` and answers via tool-call —
   `assemble()` is never reached because the slot is empty.

Net effect: context-engine plugin scores **identical to noop** (or
hangs, depending on plugin error handling).

---

## Evaluation Semantics Shift

Switching from memory- to context-engine plugins changes what
"accuracy" means and which signals we can capture:

| Signal | memory plugin | context-engine plugin |
|---|---|---|
| End-to-end LoCoMo accuracy (LLM judge) | ✅ | ✅ |
| Retrieval precision@K / recall@K | possible (track tool calls) | **not directly observable** (no tool calls) |
| Per-question retrieval latency | tool-call duration | engine assemble duration (different shape) |
| `final_context_tokens` | post-tool-call assembly | engine's `assemble()` output |
| Tool-call count distribution | rich signal (selectivity) | **always 0** for memory tools |
| Compaction events | none | engine-driven (per-turn or threshold-triggered) |

We will need engine-side instrumentation hooks (or rely on
openclaw's trace API, which is itself a Stage 3 prerequisite) to
capture context-engine internal behavior.

---

## Minimum Changes To Support Context-Engine Plugins

### 1. Plugin manifest schema

`openclaw-eval/plugins/<name>/openclaw.plugin.json` — accept
`kind` as `"context-engine"` or `["memory","context-engine"]`.
No code change needed (openclaw's loader already accepts both);
just document the convention for plugin authors.

### 2. Template & entrypoint render

`openclaw-eval/container/openclaw.template.json`:

```json
"plugins": {
  "allow": "__PLUGIN_ALLOW__",
  "slots": {
    "memory": "__MEMORY_SLOT__",
    "contextEngine": "__CONTEXT_ENGINE_SLOT__"
  },
  "entries": "__PLUGIN_ENTRIES__"
}
```

`openclaw-eval/container/entrypoint.sh` — branch on plugin kind:

```bash
if has_kind "$PLUGIN" "context-engine"; then
  CONTEXT_ENGINE_SLOT="$PLUGIN"
else
  CONTEXT_ENGINE_SLOT="__none__"   # or omit slot entirely
fi
```

`has_kind` reads `openclaw.plugin.json:kind` (already staged into the
image at `/app/extensions/<name>/`), accepts string or array.

### 3. System YAML new field

`evaluation/config/systems/openclaw-docker-<name>.yaml`:

```yaml
openclaw:
  context_engine_mode: "<plugin id>"   # NEW (parallel to memory_mode)
  memory_mode: "noop"                  # set when plugin is pure context-engine
```

### 4. Adapter dispatch

`evaluation/src/adapters/openclaw_docker_adapter.py`:

- `add()` — for context-engine plugins, write transcript to
  workspace and let openclaw runtime drive `engine.ingest()` /
  `ingestBatch()` per turn (no explicit `memory index --force`).
- `search()` — return skipped (context-engine has no callable
  retrieve; assemble happens transparently inside agent run).
- `answer()` — same shape as today, but the prompt section will be
  whatever `engine.assemble().systemPromptAddition` produces, not a
  manually-injected `memory_search` directive.

### 5. Prompt-builders gating

Current `openclaw-eval/plugins/<name>/src/prompt-builders.ts`
returns `[]` when `memory_search`/`memory_get` tools are absent.
Keep that behavior — context-engine plugins **should** return `[]`
from the memory prompt builder; their prompt contribution comes
from `assemble().systemPromptAddition` instead.

### 6. Metrics adjustments

- `evaluation/src/metrics/retrieval_metrics.py` — already has
  skipped-suppress; ensure context-engine plugins consistently mark
  `retrieval_route: null`.
- Add a new diagnostic group `engine_compaction_events` (count,
  cumulative tokens saved) — requires openclaw trace API hooks
  (Stage 3).

### 7. Smoke gate

A new `<name>_engine_passphrase_gate.sh` template:
- Inject a transcript with the passphrase
- Run a fresh agent turn asking for it
- Assert the engine's `assemble()` carried the relevant context
- Pass criterion: reply contains the passphrase

This validates the engine wired through end-to-end, the same way
`stub_passphrase_gate.sh` validates memory plugin wiring.

---

## Estimated Effort

| Item | Effort |
|---|---|
| Template + entrypoint + has_kind dispatch | 0.5 day |
| Adapter context-engine code path + tests | 1 day |
| New smoke gate template | 0.5 day |
| Stage 3 docs + first context-engine plugin onboarding (e.g. hindsight or honcho) | 1-2 days |
| **Total** | **3-4 days** for first context-engine plugin |

Subsequent context-engine plugins after the first reuse the path;
~0.5-1 day each.

---

## Where This Fits

**Stage 3 backlog item** — explicitly NOT in Stage 2 scope (which
closes Stage 1 open risks for memory plugins only). Slots in Stage 3
alongside:

- Trace API instrumentation (Stage 1 R&D Spike #1 follow-up)
- Per-question retrieval-quality metrics (precision@K / recall@K)
- More memory plugins (zep, memos, memu)

Of those, **trace API** is a prerequisite for properly evaluating
context-engine plugins (since their internal compaction events are
the main differentiator and aren't visible without trace).

---

## Open Questions

1. **Dual-kind plugins** — if a plugin declares both kinds, do we
   want the eval framework to be able to test (memory only) /
   (context-engine only) / (both) as three separate scorecards?
   Decision deferred until we have a real dual-kind plugin to test.
2. **Workspace flush_mode interaction** — context-engine plugins own
   `compact()` lifecycle; our existing `flush_mode: shared_llm` may
   conflict or duplicate work. Likely answer: `flush_mode: disabled`
   becomes the default for context-engine plugins, deferring all
   compaction to the engine itself.
3. **Bootstrap ordering** — `engine.bootstrap()` runs before first
   message ingest; our adapter's `_ingest_conversation()` posts the
   full transcript in a tight loop. Need to verify openclaw runtime
   triggers `bootstrap()` exactly once before the first `ingest()`.

These get resolved when we write the actual Stage 3 plan.
