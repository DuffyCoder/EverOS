// mem0 plugin entry point.
//
// Day 1: scaffolding + runtime stub.
// Day 2 (this commit): wires sidecar HTTP client into search-manager;
// registers memory_search + memory_get tools that route through the
// plugin's MemoryPluginRuntime closure (avoiding the registry indirection
// memory-core uses for its own bundled-only path).
//
// Day 3 work: bring up the Python sidecar (FastAPI + mem0ai SDK) and
// adjust the openclaw-eval Dockerfile to start it alongside Node.
//
// Note on minimal deps:
//   Like the stub plugin, this package.json declares NO deps (not even
//   workspace:*) so frozen-lockfile build accepts the new workspace
//   member. Imports of "openclaw/plugin-sdk/*" resolve via openclaw's
//   jiti alias map at runtime and via tsconfig paths at compile time.
import { resolveDefaultAgentId } from "openclaw/plugin-sdk/agent-runtime";
import {
  jsonResult,
  type AnyAgentTool,
  type MemoryPluginRuntime,
} from "openclaw/plugin-sdk/memory-core-host-runtime-core";
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

// Plain JSON-Schema-shaped literals (cast `unknown` then `any` to satisfy
// AgentTool<TSchema>). pi-agent-core forwards parameters verbatim to the
// LLM provider without runtime TypeBox validation, so this is sufficient.
// oxlint-disable-next-line typescript/no-explicit-any
const MemorySearchSchema: any = {
  type: "object",
  properties: {
    query: { type: "string", description: "Search query." },
    maxResults: { type: "number" },
    minScore: { type: "number" },
    corpus: { type: "string" },
  },
  required: ["query"],
};

// oxlint-disable-next-line typescript/no-explicit-any
const MemoryGetSchema: any = {
  type: "object",
  properties: {
    relPath: { type: "string", description: "Relative path under workspace memory tree." },
    from: { type: "number" },
    lines: { type: "number" },
  },
  required: ["relPath"],
};

interface ToolCtxLike {
  config?: unknown;
  agentId?: string;
  sessionKey?: string;
}

function buildUnavailablePayload(error: string) {
  return {
    results: [],
    disabled: true,
    unavailable: true,
    error,
    warning: `Memory search unavailable: ${error}`,
    action: "Check that the mem0 sidecar is reachable; retry memory_search.",
  };
}

function makeMemorySearchTool(
  runtime: MemoryPluginRuntime,
  ctx: ToolCtxLike,
): AnyAgentTool {
  return {
    label: "Memory Search",
    name: "memory_search",
    description:
      "Recall facts, decisions, dates, people, and preferences stored in mem0 memory. Always call this tool before answering questions about prior work or context.",
    parameters: MemorySearchSchema,
    async execute(_toolCallId, params) {
      const p = (params ?? {}) as {
        query?: string;
        maxResults?: number;
        minScore?: number;
      };
      const query = (p.query ?? "").toString();
      if (!query.trim()) {
        return jsonResult(buildUnavailablePayload("query is required"));
      }
      // oxlint-disable-next-line typescript/no-explicit-any
      const cfg = ctx.config as any;
      const agentId = ctx.agentId ?? (cfg ? resolveDefaultAgentId(cfg) : "main");
      const { manager, error } = await runtime.getMemorySearchManager({
        cfg,
        agentId,
      });
      if (!manager) {
        return jsonResult(buildUnavailablePayload(error ?? "no memory manager"));
      }
      try {
        const results = await manager.search(query, {
          maxResults: p.maxResults,
          minScore: p.minScore,
          sessionKey: ctx.sessionKey,
        });
        const status = manager.status();
        return jsonResult({
          results,
          provider: status.provider,
          model: status.model,
          mode: "mem0",
        });
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err);
        return jsonResult(buildUnavailablePayload(reason));
      }
    },
  };
}

function makeMemoryGetTool(
  runtime: MemoryPluginRuntime,
  ctx: ToolCtxLike,
): AnyAgentTool {
  return {
    label: "Memory Get",
    name: "memory_get",
    description:
      "Read a specific memory snippet by relative path under the workspace memory tree. Use after memory_search to pull only the lines you need.",
    parameters: MemoryGetSchema,
    async execute(_toolCallId, params) {
      const p = (params ?? {}) as {
        relPath?: string;
        from?: number;
        lines?: number;
      };
      const relPath = (p.relPath ?? "").toString();
      if (!relPath.trim()) {
        return jsonResult({ path: "", text: "", disabled: true, error: "relPath required" });
      }
      // oxlint-disable-next-line typescript/no-explicit-any
      const cfg = ctx.config as any;
      const agentId = ctx.agentId ?? (cfg ? resolveDefaultAgentId(cfg) : "main");
      const { manager, error } = await runtime.getMemorySearchManager({
        cfg,
        agentId,
      });
      if (!manager) {
        return jsonResult({ path: relPath, text: "", disabled: true, error });
      }
      try {
        const out = await manager.readFile({
          relPath,
          from: p.from,
          lines: p.lines,
        });
        return jsonResult(out);
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err);
        return jsonResult({ path: relPath, text: "", disabled: true, error: reason });
      }
    },
  };
}

export default definePluginEntry({
  id: "mem0",
  name: "mem0 Memory Plugin",
  description:
    "Form B memory plugin delegating to mem0ai via a co-located Python HTTP sidecar. Implements MemoryPluginRuntime + memory_search/memory_get tools.",
  kind: "memory",
  register(api) {
    const opts = resolveMem0Options(api.pluginConfig);
    const runtime = createMem0Runtime(opts);

    api.registerMemoryCapability({
      runtime,
      promptBuilder: ({ availableTools }) => {
        const hasSearch = availableTools.has("memory_search");
        const hasGet = availableTools.has("memory_get");
        if (!hasSearch && !hasGet) {
          return [];
        }
        // Mirror memory-core's directive phrasing — proven on LoCoMo.
        // The non-directive "use these tools" wording fails on simple
        // factoid questions because the LLM doesn't probe memory by
        // default; assertive wording forces the recall step.
        let guidance: string;
        if (hasSearch && hasGet) {
          guidance =
            "Before answering anything about prior work, decisions, dates, people, preferences, todos, or any user-supplied facts: run memory_search to recall stored memories from mem0; then use memory_get to pull additional lines if needed. If low confidence after search, say you checked.";
        } else if (hasSearch) {
          guidance =
            "Before answering anything about prior work, decisions, dates, people, preferences, todos, or any user-supplied facts: run memory_search to recall stored memories from mem0 and answer from the matching results. If low confidence after search, say you checked.";
        } else {
          guidance =
            "Before answering anything about prior work, decisions, or user-supplied facts that already point to a specific memory entry: run memory_get to pull only the needed lines.";
        }
        return ["## Memory Recall (mem0)", guidance, ""];
      },
      publicArtifacts: { listArtifacts: async () => [] },
    });

    api.registerTool((ctx) => makeMemorySearchTool(runtime, ctx), {
      names: ["memory_search"],
    });
    api.registerTool((ctx) => makeMemoryGetTool(runtime, ctx), {
      names: ["memory_get"],
    });
  },
});
