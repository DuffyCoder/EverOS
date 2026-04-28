// MemoryPluginRuntime impl for the EverMemOS plugin. Mirrors the mem0
// plugin pattern: per-agent cache of EverMemosSearchManager instances;
// closeAll iterates and best-effort closes.
import { resolveAgentWorkspaceDir } from "openclaw/plugin-sdk/agent-runtime";
import type {
  MemoryPluginRuntime,
} from "openclaw/plugin-sdk/memory-core-host-runtime-core";
import { resolveEverMemosBackendConfig } from "./backend-config.js";
import { EverMemosSearchManager } from "./search-manager.js";

export interface EverMemosRuntimeOptions {
  apiUrl?: string;
  apiKey?: string;
  apiTimeoutMs?: number;
  syncMode?: boolean;
  retrieveMethod?: string;
  scope?: "personal" | "group";
  memoryTypes?: string[];
}

export function createEverMemosRuntime(
  opts: EverMemosRuntimeOptions = {},
): MemoryPluginRuntime {
  const managers = new Map<string, EverMemosSearchManager>();

  return {
    async getMemorySearchManager(params) {
      const cached = managers.get(params.agentId);
      if (cached) return { manager: cached };
      const workspaceDir = resolveAgentWorkspaceDir(params.cfg, params.agentId);
      const manager = new EverMemosSearchManager({
        agentId: params.agentId,
        workspaceDir,
        ...opts,
      });
      managers.set(params.agentId, manager);
      return { manager };
    },
    resolveMemoryBackendConfig() {
      return resolveEverMemosBackendConfig();
    },
    async closeAllMemorySearchManagers() {
      for (const manager of managers.values()) {
        try {
          await manager.close();
        } catch {
          // best-effort
        }
      }
      managers.clear();
    },
  };
}
