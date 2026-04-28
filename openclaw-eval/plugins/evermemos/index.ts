// EverMemOS plugin entry point.
//
// This plugin's runtime fetches the host's stand-alone EverMemOS HTTP
// API (default http://host.docker.internal:1995). No Python sidecar —
// all retrieval/ingest happens via fetch from the openclaw container to
// the host. Container must be started with
//     --add-host=host.docker.internal:host-gateway
// (handled by OpenClawDockerAdapter for memory_mode=evermemos).
//
// Prompt note (Stage 1 caveat): each memory plugin ships its own
// recommended prompt as it would deploy in production. The prompt
// below describes EverMemOS's positioning (group conversation memory,
// retrieved by query) WITHOUT mimicking memory-core or mem0 phrasing.
// EverMemOS does not bundle an "official agent system prompt" template
// in this repo, so this is our faithful interpretation of its API
// surface and use-case docs (docs/dev_docs/agentic_retrieval_guide.md).
import { resolveDefaultAgentId } from "openclaw/plugin-sdk/agent-runtime";
import {
  jsonResult,
  type AnyAgentTool,
  type MemoryPluginRuntime,
} from "openclaw/plugin-sdk/memory-core-host-runtime-core";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import {
  createEverMemosRuntime,
  type EverMemosRuntimeOptions,
} from "./src/runtime.js";

interface EverMemosPluginConfigShape {
  apiUrl?: string;
  apiKey?: string;
  apiTimeoutMs?: number;
  syncMode?: boolean;
  retrieveMethod?: string;
  scope?: "personal" | "group";
  memoryTypes?: string[];
}

function resolveOptions(pluginConfig: unknown): EverMemosRuntimeOptions {
  const cfg = (pluginConfig ?? {}) as EverMemosPluginConfigShape;
  const out: EverMemosRuntimeOptions = {};
  // Precedence: pluginConfig.apiUrl > env EVERMEMOS_API_URL > built-in default.
  if (typeof cfg.apiUrl === "string" && cfg.apiUrl.trim()) {
    out.apiUrl = cfg.apiUrl.trim();
  } else if (typeof process.env.EVERMEMOS_API_URL === "string" && process.env.EVERMEMOS_API_URL.trim()) {
    out.apiUrl = process.env.EVERMEMOS_API_URL.trim();
  }
  if (typeof cfg.apiKey === "string" && cfg.apiKey.length > 0) {
    out.apiKey = cfg.apiKey;
  } else if (typeof process.env.EVERMEMOS_API_KEY === "string" && process.env.EVERMEMOS_API_KEY.length > 0) {
    out.apiKey = process.env.EVERMEMOS_API_KEY;
  }
  // EVERMEMOS_GROUP_ID env var lets the OpenClawDockerAdapter scope
  // memory_search to the active LoCoMo conversation_id, since openclaw
  // agentId is "main" for every conv (one container per conv).
  if (typeof process.env.EVERMEMOS_GROUP_ID === "string" && process.env.EVERMEMOS_GROUP_ID.trim()) {
    out.groupId = process.env.EVERMEMOS_GROUP_ID.trim();
  }
  if (typeof cfg.apiTimeoutMs === "number" && cfg.apiTimeoutMs >= 100) {
    out.apiTimeoutMs = cfg.apiTimeoutMs;
  }
  if (typeof cfg.syncMode === "boolean") {
    out.syncMode = cfg.syncMode;
  }
  if (typeof cfg.retrieveMethod === "string" && cfg.retrieveMethod.trim()) {
    out.retrieveMethod = cfg.retrieveMethod.trim();
  }
  if (cfg.scope === "personal" || cfg.scope === "group") {
    out.scope = cfg.scope;
  }
  if (Array.isArray(cfg.memoryTypes)) {
    out.memoryTypes = cfg.memoryTypes.filter((m): m is string => typeof m === "string");
  }
  return out;
}

// oxlint-disable-next-line typescript/no-explicit-any
const MemorySearchSchema: any = {
  type: "object",
  properties: {
    query: { type: "string", description: "Search query." },
    maxResults: { type: "number" },
    minScore: { type: "number" },
  },
  required: ["query"],
};

// oxlint-disable-next-line typescript/no-explicit-any
const MemoryGetSchema: any = {
  type: "object",
  properties: {
    relPath: { type: "string", description: "Memory message_id or workspace path." },
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

function buildUnavailable(error: string) {
  return {
    results: [],
    disabled: true,
    unavailable: true,
    error,
    warning: `EverMemOS retrieval unavailable: ${error}`,
    action:
      "Verify the EverMemOS HTTP API is reachable from inside the container (docker run must use --add-host=host.docker.internal:host-gateway when targeting a host server at 127.0.0.1:1995).",
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
      "Recall messages, events, decisions, and profile facts from the prior conversation history stored in EverMemOS. Always call this when the user asks about earlier turns of the conversation, what someone said, or what was decided.",
    parameters: MemorySearchSchema,
    async execute(_toolCallId, params) {
      const p = (params ?? {}) as {
        query?: string;
        maxResults?: number;
        minScore?: number;
      };
      const query = (p.query ?? "").toString();
      if (!query.trim()) {
        return jsonResult(buildUnavailable("query is required"));
      }
      // oxlint-disable-next-line typescript/no-explicit-any
      const cfg = ctx.config as any;
      const agentId = ctx.agentId ?? (cfg ? resolveDefaultAgentId(cfg) : "main");
      const { manager, error } = await runtime.getMemorySearchManager({
        cfg,
        agentId,
      });
      if (!manager) {
        return jsonResult(buildUnavailable(error ?? "no memory manager"));
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
          mode: "evermemos",
        });
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err);
        return jsonResult(buildUnavailable(reason));
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
      "Read a specific memory entry by its message_id (or workspace memory path). Use after memory_search to inspect a hit in full.",
    parameters: MemoryGetSchema,
    async execute(_toolCallId, params) {
      const p = (params ?? {}) as {
        relPath?: string;
        from?: number;
        lines?: number;
      };
      const relPath = (p.relPath ?? "").toString();
      if (!relPath.trim()) {
        return jsonResult({
          path: "",
          text: "",
          disabled: true,
          error: "relPath required",
        });
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
        return jsonResult({
          path: relPath,
          text: "",
          disabled: true,
          error: reason,
        });
      }
    },
  };
}

export default definePluginEntry({
  id: "evermemos",
  name: "EverMemOS Memory Plugin",
  description:
    "Form B memory plugin: registers MemoryPluginRuntime + memory_search/memory_get tools that delegate to the host EverMemOS HTTP API (default http://host.docker.internal:1995).",
  kind: "memory",
  register(api) {
    const opts = resolveOptions(api.pluginConfig);
    const runtime = createEverMemosRuntime(opts);

    api.registerMemoryCapability({
      runtime,
      promptBuilder: ({ availableTools, citationsMode }) => {
        const hasSearch = availableTools.has("memory_search");
        const hasGet = availableTools.has("memory_get");
        if (!hasSearch && !hasGet) return [];
        const lines: string[] = [
          "## Memory (EverMemOS)",
          "You have access to the prior conversation history of this group, along with extracted events, decisions, and profile facts. Memory is retrieved by querying the EverMemOS index.",
        ];
        if (hasSearch) {
          lines.push(
            "Use memory_search whenever the user asks about earlier turns of the conversation, what someone said, decisions made, dates and people mentioned, or facts the user previously shared. Run memory_search before answering such questions and use the returned snippets to ground your reply.",
          );
        }
        if (hasGet) {
          lines.push(
            "Use memory_get to read the full text of a specific memory entry (e.g. when memory_search surfaces a snippet you want to expand).",
          );
        }
        if (citationsMode === "off") {
          lines.push(
            "Citations are disabled: do not mention message_ids in replies unless the user explicitly asks.",
          );
        }
        lines.push("");
        return lines;
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
