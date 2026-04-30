# Plugin Kinds — Memory vs Context-Engine

> **Date**: 2026-04-30
> **Status**: Design note (not yet a Stage 3 plan; informs Stage 3 scoping)
> **Trigger**: Survey of openclaw plugin types prompted by user question
> "对于 openclaw plugin 支持的是 memory 还是 context engine"
> **Revision**: 2026-04-30 — Codex r1 review caught 4 issues; all
> validated against upstream and incorporated. Codex r2 review caught
> 4 more (session-routing under-specification, option-C scope creep,
> dual-kind asymmetry, smoke-gate vagueness); all validated and
> folded in.

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
- **Runtime contract** (two layers, often confused):
  - `MemoryPluginRuntime` (`src/plugins/memory-state.ts:96`) — 3
    methods that the plugin author implements: `getMemorySearchManager`,
    `resolveMemoryBackendConfig`, optional `closeAllMemorySearchManagers`.
  - `MemorySearchManager` (`packages/memory-host-sdk/src/host/types.ts:61`)
    — what `getMemorySearchManager()` returns; the actual host-facing
    interface with `search`, `readFile`, `status`, optional `sync`,
    `probeEmbeddingAvailability`, `probeVectorAvailability`, optional
    `close`.
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

A plugin can declare `kind: ["memory", "context-engine"]`. **Memory
and context-engine sides activate via different mechanisms** (verified
in registry.ts):

- **Memory side** (`registry.ts:~1336`): `registerMemoryCapability`
  is gated by `record.memorySlotSelected`. Dual-kind plugins not
  selected in `slots.memory` log `"dual-kind plugin not selected for
  memory slot; skipping memory capability registration"` and skip the
  memory wiring at registration time.
- **Context-engine side** (`registry.ts:1286`): `registerContextEngine`
  has **no slot-selected gate**. The factory always registers (subject
  only to reserved-id and duplicate-registration checks). Selection
  happens later, when `resolveContextEngine()` reads
  `slots.contextEngine` and looks up the engine id in the registry.

Practical implication for evaluation: a dual-kind plugin's
context-engine factory will be registered even when the eval system
isn't testing the context-engine side. Don't design scorecards
around a (false) symmetry of skip-logs — instead, observe which
engine id `resolveContextEngine()` returns at runtime.

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

### 0. ⚠ Upstream openclaw blocker — audit/patch contextEngine slot

**Codex r1 finding (2026-04-30)**: openclaw's plugin config pipeline has
**half-finished context-engine support** that silently ignores the slot:

- `config-normalization-shared.ts:137-148` — `normalizePluginsConfigWithResolver`
  only preserves `slots.memory`; `slots.contextEngine` is dropped during
  normalization.
- `loader.ts:403-411` — activation hash includes `memorySlot` but **not**
  `contextEngine`. Slot changes don't invalidate plugin cache.

If we don't fix this first, Stage 3 work below is dead on arrival —
even with a correctly rendered template, the runtime config never sees
the contextEngine slot and falls through to the default `"legacy"`
engine.

**Required upstream changes** (PR or local patch):
1. Extend normalization to preserve `slots.contextEngine`.
2. Add `contextEngineSlot` to the activation hash.
3. Audit plugin diagnostics / `validate*` paths for parallel coverage.

This is a prerequisite, not a follow-up. **Estimated effort: 0.5 day
of openclaw source work + verification.**

### 1. Plugin manifest schema

`openclaw-eval/plugins/<name>/openclaw.plugin.json` — accept
`kind` as `"context-engine"` or `["memory","context-engine"]`.
The loader already accepts both kinds (`registry.ts:255`), but per
item 0 above, slot wiring is incomplete; document the convention for
plugin authors after the upstream fix lands.

### 2. Template & entrypoint render

`openclaw-eval/container/openclaw.template.json` — add the
`contextEngine` slot, but make it **optional** (omit when not used):

```json
"plugins": {
  "allow": "__PLUGIN_ALLOW__",
  "slots": {
    "memory": "__MEMORY_SLOT__"
  },
  "entries": "__PLUGIN_ENTRIES__"
}
```

`openclaw-eval/container/entrypoint.sh` — branch on plugin kind, and
**only inject `slots.contextEngine` when the plugin is context-engine
shaped**:

```bash
if has_kind "$PLUGIN" "context-engine"; then
  # add slots.contextEngine = $PLUGIN
  jq '.plugins.slots.contextEngine = $eng' --arg eng "$PLUGIN" ...
fi
# otherwise leave slots.contextEngine absent → openclaw falls back
# to default "legacy" engine (see slots.ts:17)
```

`has_kind` reads `openclaw.plugin.json:kind` (already staged into the
image at `/app/extensions/<name>/`), accepts string or array.

**⚠ Do NOT use a sentinel like `"__none__"`** —
`resolveContextEngine` (`context-engine/registry.ts:411`) treats any
non-empty string as an engine id and throws if it isn't registered.
Either omit the field or set it to `"legacy"`.

### 3. System YAML new field

`evaluation/config/systems/openclaw-docker-<name>.yaml`:

```yaml
openclaw:
  context_engine_mode: "<plugin id>"   # NEW (parallel to memory_mode)
  memory_mode: "noop"                  # set when plugin is pure context-engine
```

### 4. Adapter dispatch — **ingestion path is unresolved**

`evaluation/src/adapters/openclaw_docker_adapter.py`:

**Codex r1 finding (2026-04-30)**: The previous draft said `add()`
would "write transcript to workspace and let openclaw drive
`engine.ingest()`/`ingestBatch()`" — **this path does not exist**.

Reading `attempt.context-engine-helpers.ts:126`:

```ts
const newMessages = params.messagesSnapshot.slice(params.prePromptMessageCount);
if (newMessages.length > 0) {
  await params.contextEngine.ingestBatch({...});
}
```

`ingestBatch()` only fires from messages produced **inside a real
agent turn**. There's no openclaw-internal mechanism to replay our
`memory/session-*.md` markdown into `ingestBatch()`. Likewise
`bootstrap()` accepts a `sessionFile` — that's a session DAG path,
not arbitrary markdown.

**Three options, must choose before Stage 3 starts**:

| Option | What | Pros | Cons |
|---|---|---|---|
| **A. Real session replay** | Drive 419 turns through openclaw agent loop with a stub LLM that returns "ok"; engine ingests via normal turn path | Most faithful to production | Wall-clock 30+ min/conv even with stub LLM; need stub-LLM hook |
| **B. Bootstrap import format** | Use `engine.bootstrap({sessionFile})` — write the LoCoMo transcript into the session DAG file format openclaw expects | Single bootstrap call instead of N turns | Need to reverse-engineer session DAG file format; some engines may not implement bootstrap |
| **C. Custom bridge command** | Add bridge RPC `engine_ingest_batch` that calls `engine.ingestBatch(messages)` directly | Conceptually cleanest | **Substantial engineering**: bridge currently only spawns CLI subcommands + a single `memory-core` direct-import for flush plans (`bridge.mjs:469` switch); a new in-process path needs config load + plugin loader run + `resolveContextEngine()` + env/cwd plumbing + lifecycle dispose |

Recommendation: **start with C** (still the cheapest path to
something testable), but treat it as building a small embedded
openclaw runtime. The 1.5-day estimate previously listed was too
optimistic — reset to **2.5-3 days** (see Estimated Effort).

#### Critical: session-id routing must be specified

`ContextEngine.ingest()` and `assemble()` are both **session-keyed**
(`types.ts:179, 224`). The eval adapter answers each question with
`session_id = f"{conv_id}__{qid}"` (per-QA isolation, see
`openclaw_adapter.py:414`). If `add()` ingests history under
`conv_id` (or some source-data-derived id), then the per-QA `assemble()`
runs against an empty engine store and the agent retrieves nothing.

Three viable session-routing strategies; **must pick one** as part of
the option-C/A/B selection:

| Strategy | What | Trade-off |
|---|---|---|
| **R1. Replicate per QA** | After ingesting under a base id, replay/copy state into each `conv__qid` session before its run | Most isolated; engine state copy may be expensive (50× per conv) and engine-specific |
| **R2. Conversation-level session** | All QA share `session_id = conv_id`; ingest once, no replay | Simplest; but answers from earlier QA pollute the engine state for later QA — questions are no longer independent |
| **R3. Engine-supported clone/link** | Engine exposes a "fork session" primitive; eval calls it per QA | Cleanest semantically; requires the engine to support it (not in current `ContextEngine` interface) |

Recommendation: **R2 for first plugin** (simple, validates the chain),
document the QA-pollution risk, then iterate to R1 if scorecard
quality demands isolation. R3 is a Stage 3+ proposal to upstream.

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

A new `<name>_engine_passphrase_gate.sh` template. **The gate must
bind to the chosen ingestion route + chosen session strategy** (see
item 4) — otherwise it can pass/fail for the wrong reason. Concrete
shape (assuming Option C + R2):

1. Compose a 3-message transcript containing the passphrase
   "WOMBAT_42".
2. Drive `engine_ingest_batch` bridge RPC with
   `session_id = "smoke_gate_conv"`.
3. Issue an `agent_run` with `session_id = "smoke_gate_conv"` (same
   id) and message "What's the passphrase from the conversation?".
4. Pass criterion: reply contains "WOMBAT_42" AND the engine
   instrumentation (afterTurn callback or trace event) shows
   `assemble()` was called and returned non-empty messages.

Step 4's assemble-was-called check is what distinguishes a real
context-engine pass from a "agent guessed correctly" coincidence.
Without it, the gate passes vacuously when the engine is broken
but the LLM happens to know "WOMBAT_42" from training data.

This validates the engine wired through end-to-end, the same way
`stub_passphrase_gate.sh` validates memory plugin wiring.

---

## Estimated Effort

Updated 2026-04-30 to reflect the upstream blocker (item 0) and the
ingestion-path open question (item 4) flagged by Codex review.

| Item | Effort |
|---|---|
| **0. Upstream openclaw normalization + activation hash patch** | 0.5 day |
| 1. Manifest schema convention docs | 0.1 day |
| 2. Template + entrypoint contextEngine slot rendering | 0.3 day |
| 3. System YAML new field | 0.1 day |
| 4. Adapter ingest path — **option C bridge RPC** (embedded openclaw runtime) | **2.5-3 days** (was 1.5; r2 review showed this is "build a small in-process engine resolver", not a thin RPC) |
| 4a. Session-routing strategy — implement R2 (single conv-level session) | 0.5 day |
| 4-extension. Adapter ingest path — **option A real replay** (later) | 2-3 days |
| 5. Prompt-builders gating sanity (no change, just verify) | 0.1 day |
| 6. Metrics adjustments + new diagnostic group | 0.5 day |
| 7. New smoke gate template (bound to chosen ingest route + session id) | 0.5 day |
| Stage 3 docs + first context-engine plugin onboarding | 1-2 days |
| **Total to first scorecard** | **~6.5 days** (was 5 after r1; r2 surfaced more in option-C scope) |

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
