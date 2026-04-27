// mem0 plugin entry point.
//
// Day 1 (this commit): registers MemoryPluginRuntime and a stub
// MemorySearchManager. Plugin loads cleanly in the build but search
// calls throw "not yet wired" until Day 2 lands sidecar-client +
// search-manager wiring + memory_search/memory_get tool registration.
//
// Day 2 will:
//   - implement src/sidecar-client.ts (HTTP client)
//   - swap Mem0SearchManager.search/readFile bodies to call sidecar
//   - register memory_search + memory_get tools in this register()
//
// Note on minimal deps:
//   Like the stub plugin, this package.json declares NO deps (not even
//   workspace:*) so frozen-lockfile build accepts the new workspace
//   member. Imports of "openclaw/plugin-sdk/*" resolve via openclaw's
//   jiti alias map at runtime and via tsconfig paths at compile time.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createMem0Runtime, type Mem0RuntimeOptions } from "./src/runtime.js";

interface Mem0PluginConfigShape {
  sidecarUrl?: string;
  sidecarTimeoutMs?: number;
  writeSessionFiles?: boolean;
}

function resolveMem0Options(pluginConfig: unknown): Mem0RuntimeOptions {
  const cfg = (pluginConfig ?? {}) as Mem0PluginConfigShape;
  const out: Mem0RuntimeOptions = {};
  if (typeof cfg.sidecarUrl === "string" && cfg.sidecarUrl.trim()) {
    out.sidecarUrl = cfg.sidecarUrl.trim();
  }
  if (typeof cfg.sidecarTimeoutMs === "number" && cfg.sidecarTimeoutMs >= 100) {
    out.sidecarTimeoutMs = cfg.sidecarTimeoutMs;
  }
  if (typeof cfg.writeSessionFiles === "boolean") {
    out.writeSessionFiles = cfg.writeSessionFiles;
  }
  return out;
}

export default definePluginEntry({
  id: "mem0",
  name: "mem0 Memory Plugin",
  description:
    "Form B memory plugin delegating to mem0ai via a co-located Python HTTP sidecar. Implements MemoryPluginRuntime; tool registration deferred to Day 2.",
  kind: "memory",
  register(api) {
    const opts = resolveMem0Options(api.pluginConfig);
    const runtime = createMem0Runtime(opts);

    api.registerMemoryCapability({
      runtime,
      promptBuilder: () => [
        "## Memory Recall (mem0)",
        "Memory is delegated to a co-located mem0 sidecar. Use `memory_search` to recall stored facts and decisions.",
        "",
      ],
      publicArtifacts: { listArtifacts: async () => [] },
    });
    // Day 2: api.registerTool(memory_search, ...) + memory_get.
  },
});
