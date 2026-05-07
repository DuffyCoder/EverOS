// resolveMemoryBackendConfig for mem0 plugin.
//
// memory-core uses backend="builtin" + an internal SQLite/FTS5 store.
// mem0 has its own vector backend (managed by the Python sidecar via the
// mem0ai SDK) but to OpenClaw it still looks like "builtin" because we
// don't expose qmd-style external interfaces — the agent talks to mem0
// only via memorySearch / memory_search tools, never via the
// MemoryRuntimeBackendConfig.qmd path.
//
// Form B plugins for upstream stores that DO expose qmd contracts (e.g.
// future mcp-based stores) can swap to ``backend: "qmd"`` here.
import type { MemoryRuntimeBackendConfig } from "openclaw/plugin-sdk/memory-core-host-engine-storage";

export function resolveMem0BackendConfig(): MemoryRuntimeBackendConfig {
  return { backend: "builtin" };
}
