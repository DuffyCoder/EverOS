# Stage 3 — Context-Engine Plugin Support

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal**: Extend the eval framework so `kind: "context-engine"` openclaw plugins can be onboarded, scored, and reported alongside existing `kind: "memory"` plugins. Currently only memory plugins work end-to-end.

**Architecture**: Context engines are agent-transparent transcript managers (no `memory_search` tool; `assemble()` injects context per turn). They differ from memory plugins along 4 axes: (1) registration API, (2) runtime contract, (3) LLM visibility, (4) slot wiring. The framework's existing memory pipeline cannot serve them — slot wiring is partial in upstream openclaw, the bridge has no in-process plugin loader, and per-QA session isolation conflicts with engine session-keyed `ingest()`. This plan addresses all four.

**Spec**: `docs/superpowers/specs/2026-04-30-plugin-kinds-design-note.md` (revision 4, post 4 rounds of Codex source-grounded review; 12 findings folded in).

**Tech Stack**: Python 3.10+ (eval harness), Node 22 (bridge in-process plugin loader), bash + jq (entrypoint), TypeScript (plugin manifests + smoke gate target plugin).

**Effort estimate** (post-r4): ~8 days for first scorecard, R2-routed (wiring/prototype, NOT comparable with memory plugin matrix). +1-2 days to upgrade to R1/R3 routing for comparable Stage 3 benchmark numbers.

---

## File Structure

**New files**:

- `openclaw-eval/patches/context-engine-slot.patch` — local patch to upstream openclaw (Phase 0)
- `evaluation/src/adapters/context_engine_history.py` — turn-history composition for `engine_import_history` RPC payload
- `openclaw-eval/container/openclaw_engine_loader.mjs` — in-process plugin loader + `resolveContextEngine()` wrapper used by the bridge for the new RPC
- `openclaw-eval/harness/<plugin>_engine_passphrase_gate.sh` — smoke gate template (per onboarded plugin)
- `tests/evaluation/test_resolved_config_context_engine.py` — unit tests for `context_engine_mode` flow through `build_openclaw_resolved_config`
- `tests/evaluation/test_openclaw_bridge_engine_import.py` — bridge `engine_import_history` RPC unit tests
- `tests/evaluation/test_entrypoint_context_engine_render.py` — entrypoint jq render unit tests
- `tests/evaluation/test_docker_adapter_context_engine_env.py` — docker adapter env emission tests

**Modified files**:

- `openclaw-eval/container/openclaw.template.json` — add `slots.contextEngine` placeholder
- `openclaw-eval/container/entrypoint.sh` — conditional `slots.contextEngine` render via jq
- `evaluation/src/adapters/openclaw_resolved_config.py` — `build_openclaw_resolved_config` accepts `context_engine_mode`; `_build_plugins_section` composes memory + contextEngine plugins
- `evaluation/src/adapters/openclaw_docker_adapter.py` — env emit `CONTEXT_ENGINE_PLUGIN_ID` / `CONTEXT_ENGINE_MODE`
- `evaluation/scripts/openclaw_eval_bridge.mjs` — new RPC `engine_import_history`; precedence dispatch (afterTurn ▶ ingestBatch ▶ ingest)
- `evaluation/scripts/openclaw_eval_bridge_lib.mjs` — extract per-RPC handler shape
- `evaluation/src/adapters/openclaw_docker_adapter.py` — `_ingest_conversation` branch for context-engine mode (single conv-level session id, R2 strategy)
- `docs/superpowers/specs/2026-04-30-plugin-kinds-design-note.md` — once Phase 0-5 land, append an "Implementation status" section with actual timings vs estimates

**Not touched**:

- Existing memory plugin code paths (mem0/evermemos/memory-core scaffolds and adapters) — additive only.
- LLM judge / LoCoMo dataset / answer pipeline — context engines reuse them unchanged.
- The existing `kind: "memory"` registration flow, `prompt-builders.ts` pattern.

---

## Phase 0: Upstream openclaw patch — slot normalization + activation hash

**Why first**: Codex r1 caught that openclaw's `normalizePluginsConfigWithResolver` drops `slots.contextEngine` and the activation hash omits `contextEngineSlot`. Without this patch the rest of the work is dead on arrival — the runtime config never sees our slot value.

### Task 0.1: Audit normalize/loader for parity with memorySlot

**Files**:
- Read: `/Data3/shutong.shan/openclaw/repo/src/plugins/config-normalization-shared.ts`
- Read: `/Data3/shutong.shan/openclaw/repo/src/plugins/loader.ts`

- [ ] **Step 1: Find all references to `slots.memory` / `memorySlot`**

```bash
grep -rn "slots.memory\|memorySlot\|slots\\.memory" /Data3/shutong.shan/openclaw/repo/src/plugins/ | grep -v test
```

Document each occurrence: which file/line, what it does (normalize, hash, validate, render, etc.), whether it has a context-engine analog already.

- [ ] **Step 2: Confirm the gap**

Two known gaps from Codex r1:

1. `config-normalization-shared.ts:137-148` `normalizePluginsConfigWithResolver` only preserves `slots.memory`.
2. `loader.ts:403-411` activation hash includes `memorySlot` only.

Surface any third / fourth gap if grep finds one.

- [ ] **Step 3: Verify no production codepath silently uses `slots.contextEngine` already**

```bash
grep -rn "slots.contextEngine\|contextEngineSlot" /Data3/shutong.shan/openclaw/repo/src/
```

If anything reads it, document where; the patch must not conflict with existing readers.

### Task 0.2: Write the patch

**Files**:
- Create: `openclaw-eval/patches/context-engine-slot.patch`

- [ ] **Step 1: Write the failing test (in openclaw repo)**

Add to a fresh test file in our patch:

```ts
// /Data3/shutong.shan/openclaw/repo/src/plugins/config-normalization-shared.test.ts
test("normalize preserves contextEngine slot when set", () => {
  const r = normalizePluginsConfigWithResolver({
    slots: { memory: "memory-core", contextEngine: "hindsight" },
    entries: {},
  });
  expect(r.slots.contextEngine).toBe("hindsight");
});

test("normalize defaults contextEngine slot when absent", () => {
  const r = normalizePluginsConfigWithResolver({});
  expect(r.slots.contextEngine).toBe("legacy");  // matches DEFAULT_SLOT_BY_KEY
});
```

Run upstream test suite; this should fail.

- [ ] **Step 2: Patch normalize**

Modify `config-normalization-shared.ts:137-148` to preserve `slots.contextEngine` parallel to `slots.memory`. Use `defaultSlotIdForKey("contextEngine")` for the fallback — this gives `"legacy"` from `DEFAULT_SLOT_BY_KEY`.

- [ ] **Step 3: Patch activation hash**

Modify `loader.ts:403-411` to include `contextEngineSlot: params.activationSource.plugins.slots.contextEngine` in the hash payload. Order matters for cache compatibility — append, don't insert.

- [ ] **Step 4: Verify both tests pass**

```bash
cd /Data3/shutong.shan/openclaw/repo && pnpm test -- config-normalization-shared
```

- [ ] **Step 5: Capture diff**

```bash
cd /Data3/shutong.shan/openclaw/repo
git diff src/plugins/config-normalization-shared.ts src/plugins/loader.ts > /tmp/context-engine-slot.patch
```

Move to `openclaw-eval/patches/context-engine-slot.patch`. Document in patch header what it does and which upstream commit it applies on top of.

### Task 0.3: Wire patch into build pipeline

- [ ] **Step 1: Modify `openclaw-eval/harness/build.py`**

After cloning openclaw repo (or before staging plugins), apply the patch:

```python
def apply_local_patches(openclaw_repo: Path, patches_dir: Path) -> None:
    """Apply local patches to upstream openclaw before building."""
    for patch in sorted(patches_dir.glob("*.patch")):
        ...
```

- [ ] **Step 2: Test idempotence**

Re-running build twice must not fail on already-applied patches. Use `git apply --check` first, then conditionally apply.

**Phase 0 exit criteria**:
- [ ] Patch passes upstream tests in openclaw repo
- [ ] `build.py` applies patch idempotently
- [ ] `slots.contextEngine` survives normalize round-trip
- [ ] Activation hash differs when `slots.contextEngine` changes (cache invalidation works)

---

## Phase 1: Manifest, template, entrypoint

**Why next**: Without slot rendering the plugin never gets selected. This phase touches the lowest-level wiring (image template + entrypoint). 0.5 day.

### Task 1.1: Update plugin manifest schema doc

**Files**:
- Modify: `openclaw-eval/plugins/README-install-mode.md` (already exists from Tier 1 work)
- Add: `openclaw-eval/plugins/README-context-engine.md`

- [ ] **Step 1: Write the new doc**

Cover:
- `kind: "context-engine"` or `kind: ["memory", "context-engine"]` in `openclaw.plugin.json`
- Required exports: `register(api)` calling `api.registerContextEngine(id, factory)`
- Factory must implement `ContextEngine` (assemble + ingest required; afterTurn/bootstrap/maintain optional)
- Reference `2026-04-30-plugin-kinds-design-note.md`

### Task 1.2: Template + entrypoint slot render

**Files**:
- Modify: `openclaw-eval/container/openclaw.template.json`
- Modify: `openclaw-eval/container/entrypoint.sh`
- Test: `tests/evaluation/test_entrypoint_context_engine_render.py`

- [ ] **Step 1: Write the failing test**

Drive entrypoint.sh in a docker-less unit test by piping a synthetic env to a thin wrapper. Assert that:
- When `MEMORY_PLUGIN_ID=mem0` and `CONTEXT_ENGINE_PLUGIN_ID` unset, rendered config has `slots.memory=mem0` and **no** `slots.contextEngine` key.
- When `CONTEXT_ENGINE_PLUGIN_ID=hindsight` and `MEMORY_PLUGIN_ID=noop`, rendered config has both slots set.
- When both set, both render.

- [ ] **Step 2: Add `slots.contextEngine` to template**

```json
"plugins": {
  "allow": "__PLUGIN_ALLOW__",
  "slots": {
    "memory": "__MEMORY_SLOT__"
  },
  "entries": "__PLUGIN_ENTRIES__"
}
```

Leave `slots.contextEngine` absent from the template; entrypoint conditionally adds it via jq. This way bundled-mode runs are unaffected.

- [ ] **Step 3: Modify entrypoint.sh**

Read `CONTEXT_ENGINE_PLUGIN_ID` env. When set:

```bash
jq --arg eng "$CONTEXT_ENGINE_PLUGIN_ID" \
   '.plugins.slots.contextEngine = $eng' \
   "$RENDERED_PATH" > "$RENDERED_PATH.tmp" && mv "$RENDERED_PATH.tmp" "$RENDERED_PATH"
```

Do NOT use `__none__` sentinel (Codex r1: `resolveContextEngine` throws on unregistered ids; default `legacy` resolves automatically when slot is absent).

- [ ] **Step 4: Verify tests pass**

**Phase 1 exit criteria**:
- [ ] Existing memory-plugin builds render unchanged (no `slots.contextEngine` key)
- [ ] When `CONTEXT_ENGINE_PLUGIN_ID` env set, jq injects the slot
- [ ] Entrypoint render tests pass
- [ ] Manifest convention documented

---

## Phase 2: Resolved-config + adapter env wiring

**Why now**: Phase 1 lets the entrypoint render the slot, but the env var must come from somewhere. The Python eval pipeline composes the docker run env from `openclaw_resolved_config.py` and `openclaw_docker_adapter.py`. 1 day.

### Task 2.1: Extend `build_openclaw_resolved_config`

**Files**:
- Modify: `evaluation/src/adapters/openclaw_resolved_config.py`
- Test: `tests/evaluation/test_resolved_config_context_engine.py`

- [ ] **Step 1: Write the failing test**

```python
def test_resolved_config_context_engine_mode():
    cfg = build_openclaw_resolved_config(
        workspace_dir="/ws",
        native_store_dir="/ws/state",
        backend_mode="fts_only",
        flush_mode="disabled",
        memory_mode="noop",
        context_engine_mode="hindsight",
        agent_llm=...,
    )
    plugins = cfg["plugins"]
    assert plugins["slots"]["contextEngine"] == "hindsight"
    assert "hindsight" in plugins["allow"]
    assert plugins["entries"]["hindsight"]["enabled"] is True

def test_resolved_config_dual_mode():
    """Memory plugin + context-engine plugin can co-exist."""
    cfg = build_openclaw_resolved_config(
        ..., memory_mode="mem0", context_engine_mode="hindsight",
    )
    p = cfg["plugins"]
    assert p["slots"]["memory"] == "mem0"
    assert p["slots"]["contextEngine"] == "hindsight"
    assert set(p["allow"]) >= {"memory-core", "mem0", "hindsight"}
```

- [ ] **Step 2: Add `context_engine_mode` parameter**

```python
def build_openclaw_resolved_config(
    *,
    workspace_dir: str,
    native_store_dir: str,
    backend_mode: str,
    flush_mode: str,
    memory_mode: str = "memory-core",
    context_engine_mode: str | None = None,  # NEW
    agent_llm: Optional[dict] = None,
    embedding: Optional[dict] = None,
) -> dict:
    ...
    resolved["plugins"] = _build_plugins_section(memory_mode, context_engine_mode)
    return resolved
```

- [ ] **Step 3: Rewrite `_build_plugins_section`**

Compose `allow / slots / entries` from BOTH modes. Bundled ids (memory-core, noop) get treated as base; external ids get added. Allow list is the union. Slots are independent.

- [ ] **Step 4: Verify tests pass**

### Task 2.2: Extend docker adapter env emit

**Files**:
- Modify: `evaluation/src/adapters/openclaw_docker_adapter.py`
- Test: `tests/evaluation/test_docker_adapter_context_engine_env.py`

- [ ] **Step 1: Write the failing test**

```python
def test_docker_env_emits_context_engine_plugin_id():
    cfg = {
        "memory_mode": "noop",
        "context_engine_mode": "hindsight",
        "agent_llm": {...},
    }
    adapter = OpenClawDockerAdapter(cfg)
    pairs = adapter._docker_env_for_container(conv_id="locomo_0")
    keys = {k for k, _ in pairs}
    assert "CONTEXT_ENGINE_PLUGIN_ID" in keys
    assert "CONTEXT_ENGINE_MODE" in keys
```

- [ ] **Step 2: Add env pairs**

In `_docker_env_for_container`, append:

```python
context_engine_mode = self._openclaw_cfg.get("context_engine_mode")
if context_engine_mode:
    pairs.append(("CONTEXT_ENGINE_PLUGIN_ID", context_engine_mode))
    pairs.append(("CONTEXT_ENGINE_MODE", context_engine_mode))
```

Whitelist them in the agent LLM env_vars passthrough check.

- [ ] **Step 3: Verify tests pass**

### Task 2.3: System YAML schema

**Files**:
- Create: `evaluation/config/systems/openclaw-docker-<example>.yaml`

Stub out a yaml that demonstrates the new `context_engine_mode` field. Used in Phase 6 for the first onboarded plugin; for now, it's a documentation artifact.

**Phase 2 exit criteria**:
- [ ] `build_openclaw_resolved_config(context_engine_mode=...)` returns correct allow/slots/entries
- [ ] Docker adapter env emits `CONTEXT_ENGINE_PLUGIN_ID` and `CONTEXT_ENGINE_MODE`
- [ ] Phase 1 + 2 chain: setting yaml `context_engine_mode` flows through to entrypoint jq render

---

## Phase 3: Bridge `engine_import_history` RPC

**Why this is the biggest phase**: The bridge currently only spawns CLI subcommands and has one direct-import path (memory-core flush plan). Context-engine ingestion needs an in-process plugin loader that can `resolveContextEngine()` and dispatch the production turn-finalization precedence. Codex r2 + r4: budget 3-3.5 days.

### Empirical finding (2026-05-02): native path blocked, scope reduced

Surveying openclaw `dist/` revealed:

- `dist/index.js` exposes `loadConfig` publicly, but **NOT** `resolveContextEngine` or `resolveRuntimePluginRegistry` — both live in hashed bundles (e.g. `dist/registry-D4L8wbCo.js`) under munged names (`r`, `t`, `n`).
- The `package.json` `exports` map enumerates 255 paths, all `./plugin-sdk/*` subpaths; no path reaches `context-engine/registry`.

In-process loading therefore requires either:

1. **Upstream patch** — add stable `./context-engine` or `./eval-harness/import-history` exports (a Phase 0-style upstream change; was deferred for Phase 0 normalization, would have to be revisited).
2. **Hash-discovery import** — glob `dist/registry-*.js`, import the munged `r` symbol; brittle across openclaw rebuilds.
3. **CLI replay** — feed conversation messages through `agent_run` per-turn; engine's `afterTurn`/`ingestBatch` fires natively, but burns LLM tokens (~50 turns/conv).

**Phase 3 scope (revised)**:

- ✅ Task 3.2 **dispatcher**: `dispatchEngineImport(engine, params)` in `openclaw_eval_bridge_lib.mjs` — pure, fully unit-tested precedence (afterTurn ▶ ingestBatch ▶ ingest), 11 cases.
- ✅ Task 3.2 **bridge handler**: `handleEngineImportHistory` wired into bridge dispatch with **stub mode** (no launcher → ok:true contract response). Native mode returns `ok:false` with explicit "not yet wired" marker; adapters can branch deterministically.
- ⏸ Task 3.1 **production loader** (`openclaw_engine_loader.mjs`): deferred. The right path is upstream stable exports (#1 above). Tracked as Stage 3 follow-up.
- ⏸ Task 3.3 **adapter R2 dispatch**: deferred until a production path exists. Phase 4 smoke gate doesn't need it (it routes through `agent_run`, not `engine_import_history`).

**Net effect**: Phase 3 went from 3-3.5 days → ~half day for the dispatcher + bridge handler. Phase 4's smoke gate becomes the next executable step and routes around the blocked native path entirely (the slot wiring from Phase 0-2 is sufficient for `agent_run` to load + use the context engine).

### Task 3.1: In-process plugin loader

**Files**:
- Create: `openclaw-eval/container/openclaw_engine_loader.mjs`
- Test: covered by Task 3.3 integration test (no isolated unit test for loader; it's a thin wrapper)

- [ ] **Step 1: Survey upstream entry points**

Read:
- `/Data3/shutong.shan/openclaw/repo/src/plugins/loader.ts` — main loader
- `/Data3/shutong.shan/openclaw/repo/src/context-engine/registry.ts` — `resolveContextEngine`
- `/Data3/shutong.shan/openclaw/repo/src/config/config.ts` — `loadConfig`

Document the minimal sequence: `loadConfig() → loader.run() → resolveContextEngine(config) → engine instance`.

- [ ] **Step 2: Write `openclaw_engine_loader.mjs`**

```js
// Loads OpenClaw config, runs plugin loader, resolves the context engine
// for the active session, and exposes a small surface to the bridge.
import { loadConfig } from "openclaw/dist/config/config.js";
import { resolveContextEngine } from "openclaw/dist/context-engine/registry.js";
// ... plugin loader entry; check `attempt.context-engine-helpers.ts` for
// the production sequence

export async function withContextEngine(callback) {
  const config = await loadConfig();
  // ... run loader, get engine
  const engine = await resolveContextEngine(config);
  try {
    return await callback(engine, config);
  } finally {
    if (engine.dispose) await engine.dispose();
  }
}
```

Lifecycle: load + resolve + dispose around each RPC. No long-lived engine state in the bridge process (each conv has its own container; engine state lives in the engine-managed store, not in-process).

### Task 3.2: `engine_import_history` RPC handler

**Files**:
- Modify: `evaluation/scripts/openclaw_eval_bridge.mjs`
- Modify: `evaluation/scripts/openclaw_eval_bridge_lib.mjs`
- Test: `tests/evaluation/test_openclaw_bridge_engine_import.py`

- [ ] **Step 1: Write the failing test**

Use the existing `test_openclaw_bridge_lib.py` mocking pattern. Drive `bridge.handleRequest({ command: "engine_import_history", session_id, messages })` against a stub engine that captures which method was called.

```python
def test_engine_import_prefers_afterTurn():
    """When engine implements afterTurn, that's the only call we make."""
    stub_engine = StubEngine(supports=["afterTurn", "ingestBatch"])
    result = run_bridge_handler({
        "command": "engine_import_history",
        "session_id": "test_conv",
        "messages": [...3 fake messages...],
    }, engine_factory=lambda: stub_engine)
    assert stub_engine.afterTurn_called == 1
    assert stub_engine.ingestBatch_called == 0

def test_engine_import_falls_back_to_ingestBatch():
    stub_engine = StubEngine(supports=["ingestBatch"])
    ...
    assert stub_engine.ingestBatch_called == 1

def test_engine_import_falls_back_to_per_message_ingest():
    stub_engine = StubEngine(supports=["ingest"])
    ...
    assert stub_engine.ingest_call_count == 3  # one per message
```

- [ ] **Step 2: Implement handler in bridge**

```js
async function handleEngineImportHistory(input, launcher) {
  return await withContextEngine(async (engine, config) => {
    const sessionId = input.session_id;
    const messages = input.messages;

    // Production precedence (attempt.context-engine-helpers.ts:110)
    if (typeof engine.afterTurn === "function") {
      // afterTurn expects sessionFile + prePromptMessageCount + tokenBudget;
      // we synthesize a turn shape with prePromptMessageCount=0 (full
      // history is "new" relative to engine's empty state).
      await engine.afterTurn({
        sessionId,
        sessionFile: input.session_file ?? "",
        messages,
        prePromptMessageCount: 0,
      });
    } else if (typeof engine.ingestBatch === "function") {
      await engine.ingestBatch({ sessionId, messages });
    } else {
      for (const msg of messages) {
        await engine.ingest({ sessionId, message: msg });
      }
    }
    return { ok: true, command: "engine_import_history" };
  });
}
```

Add to bridge.mjs switch dispatch.

- [ ] **Step 3: Verify tests pass**

### Task 3.3: Adapter dispatch — context-engine `_ingest_conversation`

**Files**:
- Modify: `evaluation/src/adapters/openclaw_docker_adapter.py`
- Modify: `evaluation/src/adapters/context_engine_history.py` (new)

- [ ] **Step 1: Write the failing test (integration smoke)**

A test that does:
1. Starts a container with `MEMORY_PLUGIN=noop` `CONTEXT_ENGINE_PLUGIN_ID=stub-engine` (a new stub plugin we add for testing — see Phase 5)
2. Calls `adapter._ingest_conversation(sandbox, conv)`
3. Asserts the stub engine's afterTurn was called once with the full LoCoMo conv

Mark this test with `@pytest.mark.skipif(no_docker)` so CI can skip if needed.

- [ ] **Step 2: Compose the history payload**

`context_engine_history.py`:

```python
def conversation_to_engine_messages(conv: Conversation) -> list[dict]:
    """Convert LoCoMo Conversation to AgentMessage[] shape."""
    out = []
    for sess in conv.sessions:
        for msg in sess.messages:
            out.append({
                "role": "user" if msg.speaker == ... else "assistant",
                "content": [{"type": "text", "text": msg.text}],
                "timestamp": msg.timestamp,
            })
    return out
```

- [ ] **Step 3: Branch in adapter**

```python
async def _ingest_conversation(self, sandbox, conv):
    if self._is_context_engine_mode():
        # R2 (conv-level session) — first scorecard prototype only
        session_id = conv.id  # NOT f"{conv.id}__{qid}"
        messages = conversation_to_engine_messages(conv)
        await self._invoke_bridge({
            "command": "engine_import_history",
            "session_id": session_id,
            "messages": messages,
        })
        return
    return await super()._ingest_conversation(sandbox, conv)
```

Document explicitly via inline comment that R2 leaks state across QA — this is the wiring/prototype mode per design note.

- [ ] **Step 4: Verify tests pass**

**Phase 3 exit criteria**:
- [ ] Bridge `engine_import_history` RPC dispatches in correct precedence
- [ ] Stub engine receives expected call shape per dispatch branch
- [ ] Adapter integration smoke ingests a full LoCoMo conversation through the new path
- [ ] R2 routing is documented with caveats inline + in design note

---

## Phase 4: Smoke gate per onboarded plugin

**Why now**: With Phases 0-3 complete, we can validate end-to-end on a known plugin before committing to scorecard runs. 0.5 day.

### Task 4.1: Stub context-engine plugin

**Files**:
- Create: `openclaw-eval/plugins/stub-engine/openclaw.plugin.json`
- Create: `openclaw-eval/plugins/stub-engine/index.ts`
- Create: `openclaw-eval/plugins/stub-engine/package.json`

- [ ] **Step 1: Stub plugin like memory-stub**

A no-op `ContextEngine` that:
- `assemble(...)` returns the input messages unchanged + a `systemPromptAddition` containing "STUB_ENGINE_OK"
- `ingest(...)` / `ingestBatch(...)` just count calls (in-memory)
- `afterTurn(...)` ditto

This proves Phase 0-3 wiring without depending on any real third-party plugin.

### Task 4.2: Smoke gate template

**Files**:
- Create: `openclaw-eval/harness/stub_engine_passphrase_gate.sh`

- [ ] **Step 1: Implement the three-criteria pass logic**

Per Codex r3 finding:
1. **4a**: Trace event proves `resolveContextEngine` returned `stub-engine`
2. **4b**: `assemble().systemPromptAddition` contains "WOMBAT_42" (we ingest a 3-msg transcript with the passphrase, stub engine reflects it back)
3. **4c**: Reply contains "WOMBAT_42"

All three pass = gate passes. Any one fails = gate fails with diagnostic output.

- [ ] **Step 2: Run the gate**

```bash
./openclaw-eval/harness/stub_engine_passphrase_gate.sh
```

Expected exit 0 if Phase 0-3 wired correctly.

**Phase 4 exit criteria**:
- [ ] Stub engine plugin builds + loads + registers
- [ ] Smoke gate passes with all 3 criteria green

---

## Phase 5: First real plugin onboarding (R2 prototype)

**Files**:
- Create: `evaluation/config/systems/openclaw-docker-<plugin-name>.yaml`
- Possibly create: `openclaw-eval/plugins/<plugin-name>/` (if bundled mode) OR
- Use `--install-spec npm:<plugin>@<version>` (if install mode, see `README-install-mode.md`)

### Task 5.1: Pick the target

Candidates (per design note + Stage 2 closure):
- **hindsight** (likely `kind: "context-engine"`, used as a plausible reference)
- **honcho** (cloud, may not be context-engine kind upon inspection)

- [ ] **Step 1: Audit the candidate plugin**

```bash
# If npm-published:
npm view <plugin> | grep -A2 "openclaw"

# If GitHub source:
git clone <repo> /tmp/inspect && cat /tmp/inspect/openclaw.plugin.json
```

Confirm `kind: "context-engine"` (or `["memory","context-engine"]`); confirm the runtime contract methods present (assemble, ingest at minimum).

### Task 5.2: Wire it through

- [ ] **Step 1: Build image** (install or bundled mode based on availability)
- [ ] **Step 2: Write yaml** with `context_engine_mode: <plugin-id>` and `memory_mode: noop`
- [ ] **Step 3: Run smoke gate adapted to this plugin** (Phase 4 template, parameterized by plugin id)
- [ ] **Step 4: 1c1q LoCoMo run** to validate end-to-end answer flow
- [ ] **Step 5: 50Q LoCoMo run** with `--run-name s3-<plugin>-r1-r2-routing`

### Task 5.3: Report

- [ ] **Step 1: Write phase 5 closure into Stage 1 closure doc**

Append to `docs/superpowers/specs/2026-04-28-stage1-closure.md`:

```markdown
## Stage 3 First Plugin (2026-XX-XX) — wiring/prototype scorecard

Plugin: <name>, kind: context-engine, ingest: R2 (conv-level session)

| n  | Acc | Note |
|----|-----|------|
| 50 | XX% | NOT comparable to memory plugin matrix; R2 leaks state across QA |
```

Explicit "not comparable" footnote per Codex r3.

**Phase 5 exit criteria**:
- [ ] One real context-engine plugin produces a smoke-gate pass
- [ ] 1c1q LoCoMo run returns non-empty answers
- [ ] 50Q scorecard documented as wiring/prototype with comparability caveat

---

## Phase 6 (deferred): R1 / R3 routing for comparable scorecards

Out of scope for first scorecard. Adds 1-2 days when undertaken:

- **R1** (replicate per QA): adapter copies engine state per `conv__qid` session before that QA's answer call. Engine-specific state copy may be expensive — choose between `bootstrap()` re-run (slow) vs file-level copy of engine store (fast but leaky).
- **R3** (engine fork primitive): submit a `ContextEngine.forkSession()` proposal upstream. Each plugin must implement; 1+ months for any community uptake.

Track R1/R3 in a Stage 4 plan, not this one.

---

## Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | Upstream openclaw refuses our patch (style/scope conflicts) | Medium | Patch is minimal (parity with existing memorySlot); but if blocked, fork to `openclaw-eval/vendor-openclaw/` for our build only |
| R2 | First target plugin uses `bootstrap()` for canonical persistence (not afterTurn/ingest), which is unhandled in `engine_import_history` precedence | Medium | Add `bootstrap()` as 4th branch if engine implements it; the design note already calls bootstrap a viable Option B |
| R3 | First real context-engine plugin has hidden config requirements (env vars, network access, etc.) | High | Smoke gate (Phase 4) validates wiring before scorecard; any failure surfaces here |
| R4 | R2-routed scorecard for context-engine reads as "the plugin scores X%" in casual reading despite our caveats | Medium | Closure doc strict footnoting + plot-level visual marker (e.g., dashed bar style) per Codex r3 |
| R5 | sophnet quota again | Low | `.env` was rotated 2026-04-30; monitor wall-clock and pause runs at 70% quota use |

---

## Dependencies

**External**:
- Upstream openclaw repo accessible at `/Data3/shutong.shan/openclaw/repo` (already true)
- Sophnet API quota for LLM (Stage 2 exhausted but rotated 2026-04-30; sufficient for Phase 5 smoke + 50Q)
- Docker engine + buildx (already in use)
- Target plugin npm-published or git-cloneable (TBD per Phase 5.1)

**Upstream blocker**:
- Phase 0 patch must land before Phase 1+; estimated 0.5 day, but if upstream behavior surprises us, may grow to 1 day

**Recommended sequencing**:
- Phase 0 first (blocker)
- Phases 1, 2, 3 can interleave once Phase 0 lands (different files, low conflict)
- Phase 4, 5 strictly after Phases 0-3
- Phase 6 deferred

---

## Out of scope

- Memory plugins (Stage 1/2 closed)
- More memory plugins (zep, memos, memu) — Stage 4
- Trace API instrumentation beyond what `engine_import_history` needs — Stage 4
- Per-question retrieval-quality metrics for context engines (precision@K / recall@K don't apply; engines have no tool calls to count)
- Dual-kind plugin scorecard variations (memory-only vs ce-only vs both) — Stage 4
- Performance optimization of the in-process plugin loader — only if it becomes a bottleneck

---

## Testing summary

| Phase | Tests | When run |
|---|---|---|
| 0 | upstream test in openclaw repo | Phase 0 task |
| 1 | `test_entrypoint_context_engine_render.py` | CI on every Phase 1+ change |
| 2 | `test_resolved_config_context_engine.py`, `test_docker_adapter_context_engine_env.py` | CI on every Phase 2+ change |
| 3 | `test_openclaw_bridge_engine_import.py` (unit) + adapter integration smoke | CI |
| 4 | smoke gate scripts | manual / CI |
| 5 | LoCoMo 1c1q + 50Q | manual after smoke pass |

Coverage target: 80% on new files (matches project convention).
