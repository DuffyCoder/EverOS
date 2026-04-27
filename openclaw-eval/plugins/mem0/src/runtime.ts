// MemoryPluginRuntime impl for the mem0 plugin.
//
// Per Spike #2 Decision 2: keep one Map<agentId, Mem0SearchManager>.
// "purpose=status" returns the same manager (no separate read-only path).
// closeAllMemorySearchManagers iterates the map.
//
// resolveMemoryBackendConfig returns {backend: "builtin"} via
// backend-config.ts. workspaceDir is resolved per-agent from cfg via
// resolveAgentWorkspaceDir so readFile() can read session.md files
// directly (Spike #2 Decision 1 Option A).
import { resolveAgentWorkspaceDir } from "openclaw/plugin-sdk/agent-runtime";
import type {
  MemoryPluginRuntime,
} from "openclaw/plugin-sdk/memory-core-host-runtime-core";
import { resolveMem0BackendConfig } from "./backend-config.js";
import { Mem0SearchManager } from "./search-manager.js";

export interface Mem0RuntimeOptions {
  // Defaults used when constructing per-agent managers. The actual values
  // come from plugin config (configSchema) and are resolved by index.ts.
  sidecarUrl?: string;
  sidecarTimeoutMs?: number;
  writeSessionFiles?: boolean;
}

export function createMem0Runtime(opts: Mem0RuntimeOptions = {}): MemoryPluginRuntime {
  const managers = new Map<string, Mem0SearchManager>();

  return {
    async getMemorySearchManager(params) {
      const cached = managers.get(params.agentId);
      if (cached) {
        return { manager: cached };
      }
      const workspaceDir = resolveAgentWorkspaceDir(params.cfg, params.agentId);
      const manager = new Mem0SearchManager({
        agentId: params.agentId,
        workspaceDir,
        sidecarUrl: opts.sidecarUrl,
        sidecarTimeoutMs: opts.sidecarTimeoutMs,
        writeSessionFiles: opts.writeSessionFiles,
      });
      managers.set(params.agentId, manager);
      return { manager };
    },
    resolveMemoryBackendConfig() {
      return resolveMem0BackendConfig();
    },
    async closeAllMemorySearchManagers() {
      for (const manager of managers.values()) {
        try {
          await manager.close();
        } catch {
          // best-effort cleanup; one bad manager shouldn't prevent the rest
        }
      }
      managers.clear();
    },
  };
}
