# Stage 1 Spike #2 — MemoryPluginRuntime Real Effort Estimate

> **Date**: 2026-04-26
> **Goal**: Validate v0.7's "3-5 days/plugin" estimate by mining `memory-core` source for the real interface surface. Ship Form B plugin authoring guide.

---

## TL;DR

**v0.7 estimate confirmed**: first Form B plugin (mem0) = ~3-4 days; subsequent plugins (evermemos, zep) = ~1-2 days each.

The fear "MemoryPluginRuntime is huge — must reimplement memory-core's 17K lines" is **unfounded**. Form B plugins are thin shims (~300-500 lines TS + 150-200 lines Python) that delegate to the upstream memory system via HTTP sidecar.

memory-core's 17K lines exist to provide an **end-to-end implementation** with FTS5 + sqlite + sophnet embeddings + atomic reindex + concept vocabulary + temporal decay + caching. Form B plugins **do not** need any of this — the upstream memory system (mem0/evermemos/zep) already handles retrieval; the plugin just adapts the interface.

---

## Real Interface Surface (from `memory-core/src/memory/search-manager.ts`)

```typescript
interface MemorySearchManager {
  // CORE — must implement
  search(query: string, opts?: {
    maxResults?: number;
    minScore?: number;
    sessionKey?: string;
  }): Promise<MemorySearchResult[]>;
  // → calls sidecar /search → maps mem0 hits to MemorySearchResult shape

  status(): MemoryProviderStatus;
  // → calls sidecar /stats once + caches; returns
  //   { backend: "builtin", provider: "mem0", files, chunks, dirty: false }

  probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult>;
  // → returns { ok: true, model: "text-embeddings" } if upstream supports embed
  // → returns { ok: false, error: "..." } otherwise

  probeVectorAvailability(): Promise<boolean>;
  // → returns true if upstream has vector index (most do)

  // OPTIONAL — return no-op or stub
  readFile(params: { relPath: string; from?: number; lines?: number }):
    Promise<{ text: string; path: string }>;
  // ⚠️ readFile is the trickiest method — see "readFile decision" below

  sync?(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
    progress?: (update: MemorySyncProgressUpdate) => void;
  }): Promise<void>;
  // → call sidecar /sync; can no-op if upstream auto-indexes

  close?(): Promise<void>;
  // → cleanup sidecar HTTP client; can no-op
}

interface MemoryPluginRuntime {
  getMemorySearchManager(params: {
    cfg: OpenClawConfig;
    agentId: string;
    purpose?: "default" | "status";
  }): Promise<{ manager: MemorySearchManager | null; error?: string }>;
  // → 1-method factory; return cached singleton per agentId

  resolveMemoryBackendConfig(params): MemoryRuntimeBackendConfig;
  // → just return { backend: "builtin" } for Form B

  closeAllMemorySearchManagers?(): Promise<void>;
  // → close all cached managers
}
```

memory-core's runtime-provider.ts is **20 lines**. Form B plugin's runtime would be similar: just delegate to the inner sidecar-backed manager.

---

## File Structure (per Form B plugin)

```
plugins/<name>/
├── openclaw.plugin.json          ← extension manifest (~10 lines)
├── package.json                  ← npm package (~30 lines)
├── tsconfig.json                 ← TS config (~10 lines)
├── index.ts                      ← definePluginEntry + register (~30 lines)
├── src/
│   ├── runtime.ts                ← MemoryPluginRuntime impl (~50 lines)
│   ├── search-manager.ts         ← MemorySearchManager impl (~150 lines)
│   ├── sidecar-client.ts         ← HTTP client to sidecar (~50 lines)
│   └── backend-config.ts         ← resolveMemoryBackendConfig (~10 lines)
├── sidecar/                      ← Python (mem0 wrapper)
│   ├── server.py                 ← FastAPI with 7 endpoints (~150 lines)
│   ├── requirements.txt          ← mem0 + fastapi + uvicorn
│   └── Dockerfile.sidecar        ← optional separate image
└── tests/
    ├── search-manager.test.ts    ← Bun/Vitest unit tests
    └── sidecar.test.py           ← Pytest sidecar tests

Total LOC: ~500-700 (TS + Python combined)
```

---

## Key Architectural Decisions for Form B

### Decision 1: `readFile` semantics

memory-core's `readFile` reads actual session-X.md from workspace. mem0/evermemos don't have a "file" concept — they have document IDs.

**Option A** (recommended): Form B plugin writes session markdown to workspace during `index()` (just like memory-core). `readFile` reads from workspace. Pros: matches openclaw's expectations; mem0 doesn't need to support readFile. Cons: workspace must persist.

**Option B**: Form B plugin maps relPath to upstream document ID. `readFile` calls upstream's get-by-id. Pros: fully delegates. Cons: each upstream needs a stable ID scheme.

**Decision**: **A**. Reuse evermemos `write_session_files` pattern (already done in current adapter). Both mem0 and evermemos will see session-X.md; their indexer adds them to the upstream store; `readFile` reads from disk.

### Decision 2: Caching `MemorySearchManager`

memory-core caches per (agentId, purpose). Form B can simplify:
- Single `Map<agentId, SidecarBackedManager>`
- "purpose=status" returns the same manager (no separate read-only path needed)
- `closeAllMemorySearchManagers` iterates the map

### Decision 3: Sidecar lifecycle

**Option A** (recommended): single container with two processes — Node openclaw + Python sidecar via supervisord (or simple shell launcher).
**Option B**: docker-compose with separate sidecar service.

**Decision**: **A**, simpler for harness orchestration. Sidecar starts on container init; openclaw spawned per RPC.

### Decision 4: Sidecar HTTP API

Standard contract for all Form B plugins:
```
POST /index    {documents: [{id, content, metadata}, ...]} → {ok, ingested}
POST /search   {query, max_results, session_key?}        → {hits: [{score, snippet, ...}]}
GET  /stats                                              → {provider, files, chunks, dirty}
GET  /probe_embedding                                    → {ok, model?, error?}
GET  /probe_vector                                       → bool
POST /sync     {reason, force, session_files}            → {ok}
GET  /healthz                                            → {ok}
POST /close                                              → {ok}
```

Plugin authors only swap the upstream library binding (mem0 vs zep vs evermemos). Same TS plugin code can serve all upstreams via env var pointing to different sidecar URLs.

---

## Effort Breakdown (mem0 first plugin)

| Task | Estimate |
|---|---|
| Decide upstream contract (already done above) | 0 |
| Scaffolding (package.json, tsconfig, plugin.json) | 0.5 day |
| `runtime.ts` + `backend-config.ts` (boilerplate) | 0.5 day |
| `sidecar-client.ts` (HTTP client wrapper) | 0.5 day |
| `search-manager.ts` (7-method impl) | 1 day |
| `sidecar/server.py` (FastAPI + mem0 SDK wiring) | 1 day |
| Container build + smoke (single conv × 5 Q) | 0.5 day |
| Stub plugin gate verification (passphrase test) | 0.5 day |
| **Total mem0** | **3.5 - 4 days** |

For evermemos / zep:
- Reuse `runtime.ts` / `backend-config.ts` / `search-manager.ts` (~80% identical)
- Only swap `sidecar/server.py` upstream import
- **Estimate: 1.5 - 2 days each**

---

## Risks Surfaced (NOT in v0.7)

### R-S2-1: `readFile` workspace coupling

If we go with Decision 1 Option A, the `evaluation/src/adapters/openclaw_ingestion.py::write_session_files` becomes shared infrastructure across all Form B plugins (already used by memory-core path). This is fine but means: changing session.md format affects every plugin's readFile output.

Mitigation: keep session.md format stable. Document this in `plugins/_base/contract.md`.

### R-S2-2: Sidecar process management

If sidecar dies mid-run, all subsequent agent RPCs fail. Need:
- Healthcheck endpoint pre-RPC
- Sidecar restart policy (supervisord auto-respawn)
- Adapter detects sidecar 5xx → fail conv (raise during `add()`)

Mitigation: `_prebootstrap_workspace` in adapter already runs a probe. Extend to verify sidecar `/healthz` before running agent.

### R-S2-3: TS-Python serialization overhead

Each search call: TS plugin → HTTP → Python sidecar → mem0 → return → JSON encode → HTTP → TS. Adds maybe 5-15ms per call.

For 50 QA × 10 conv with conc=4: ~500-1500 extra ms total. Acceptable.

### R-S2-4: Form B plugin can't replicate memory-core's caching

memory-core has aggressive embedding cache, hybrid retrieval cache. Form B delegates to upstream, which has its own caching. Net performance depends on upstream. mem0 is fast; zep is medium.

Mitigation: Stage 1 scorecard reports per-plugin latency separately. If mem0 plugin is 3x slower than memory-core baseline, plugin design (not interface) is the issue.

---

## Stage 1 Scope Adjustment

v0.7 plan ✅ unchanged:
- Week 1: Docker底座 + spikes (THIS spike completed early)
- Week 2: DockerizedOpenclawAdapter
- Week 3: Stub gate + mem0 plugin
- Week 4-5: evermemos + matrix

Spike #2 result: **no scope reduction needed**. Plugin authoring fits in v0.7 plan.

---

## Plugin Authoring Quick Reference

For future contributors:

1. Copy `plugins/_template/` (TODO: create after first plugin)
2. Implement upstream Python in `sidecar/server.py` (only this is upstream-specific)
3. Update `package.json` name + plugin.json id
4. Smoke: build image, run 1-conv test
5. Run stub gate (passphrase test) using your plugin's docker tag
6. PR with N=3 LoCoMo-S smoke result

---

## Spike Output Files

```
docs/superpowers/specs/
└── 2026-04-26-stage1-spike2-plugin-effort.md   ← this doc

(future, after Week 3)
plugins/
├── _template/                                  ← scaffolding
├── stub/                                       ← passphrase gate plugin
└── mem0/                                       ← first real Form B plugin
```
