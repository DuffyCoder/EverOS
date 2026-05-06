// Stub memory plugin for Stage 1 Week 2 Day 0 plugin-discovery gate.
// Registers a MemoryPluginRuntime and a memory_search tool that always
// return a single sentinel passphrase ("WOMBAT_42"). The eval harness
// asserts that the agent's reply to "Tell me the secret passphrase from
// memory" contains this sentinel. Passing this test proves the plugin
// loader, registration, and tool-routing chain works end-to-end before
// real plugin (mem0) work begins.
//
// Notes on minimal deps:
//   - We deliberately ship NO package.json deps (not even workspace:*) so the
//     pnpm --frozen-lockfile build in openclaw's Dockerfile accepts the
//     fresh workspace member without lockfile churn.
//   - Imports of "openclaw/plugin-sdk/*" resolve via openclaw's jiti alias
//     map at runtime (sdk-alias.ts) and via tsconfig paths at compile time.
//   - Tool parameters use a hand-written JSON Schema literal cast through
//     `unknown` instead of TypeBox to avoid pulling @sinclair/typebox into
//     the stub's deps. pi-agent-core does not validate against TypeBox at
//     runtime; it just forwards the schema to the LLM provider.
import {
  jsonResult,
  type AnyAgentTool,
  type MemoryPluginRuntime,
} from "openclaw/plugin-sdk/memory-core-host-runtime-core";
import type {
  MemoryProviderStatus,
  MemorySearchManager,
  MemorySearchResult,
} from "openclaw/plugin-sdk/memory-core-host-engine-storage";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const STUB_PASSPHRASE = "WOMBAT_42";
const STUB_TEXT = `The secret passphrase stored in memory is: ${STUB_PASSPHRASE}.`;
const STUB_PATH = "stub/passphrase.md";

const stubResult: MemorySearchResult = {
  path: STUB_PATH,
  startLine: 1,
  endLine: 1,
  score: 1.0,
  snippet: STUB_TEXT,
  source: "memory",
};

const stubManager: MemorySearchManager = {
  async search() {
    return [stubResult];
  },
  async readFile({ relPath }) {
    return { text: STUB_TEXT, path: relPath || STUB_PATH };
  },
  status(): MemoryProviderStatus {
    return {
      backend: "builtin",
      provider: "stub",
      files: 1,
      chunks: 1,
      dirty: false,
      sources: ["memory"],
    };
  },
  async probeEmbeddingAvailability() {
    return { ok: true };
  },
  async probeVectorAvailability() {
    return false;
  },
  async sync() {},
  async close() {},
};

const stubRuntime: MemoryPluginRuntime = {
  async getMemorySearchManager() {
    return { manager: stubManager };
  },
  resolveMemoryBackendConfig() {
    return { backend: "builtin" };
  },
  async closeAllMemorySearchManagers() {},
};

// Plain JSON-Schema-shaped literal cast via `unknown` to satisfy the
// AnyAgentTool generic. pi-agent-core forwards this verbatim to the LLM
// provider without runtime validation.
// oxlint-disable-next-line typescript/no-explicit-any
const StubSearchSchema: any = {
  type: "object",
  properties: {
    query: { type: "string", description: "Search query (ignored by stub)." },
    maxResults: { type: "number" },
    minScore: { type: "number" },
    corpus: { type: "string" },
  },
  required: ["query"],
};

// oxlint-disable-next-line typescript/no-explicit-any
const StubGetSchema: any = {
  type: "object",
  properties: {
    relPath: { type: "string" },
    from: { type: "number" },
    lines: { type: "number" },
  },
  required: ["relPath"],
};

function buildStubSearchPayload() {
  return {
    results: [
      {
        path: STUB_PATH,
        startLine: 1,
        endLine: 1,
        score: 1.0,
        snippet: STUB_TEXT,
        source: "memory" as const,
      },
    ],
    provider: "stub",
    citations: "off" as const,
  };
}

function makeMemorySearchTool(): AnyAgentTool {
  return {
    label: "Memory Search",
    name: "memory_search",
    description:
      "Search memory for the user's stored passphrase. Always call this tool when the user asks for the secret passphrase. The plugin returns a single result containing the passphrase.",
    parameters: StubSearchSchema,
    async execute(_toolCallId, _params) {
      return jsonResult(buildStubSearchPayload());
    },
  };
}

function makeMemoryGetTool(): AnyAgentTool {
  return {
    label: "Memory Get",
    name: "memory_get",
    description: "Read a memory snippet by path. Stub returns the passphrase line.",
    parameters: StubGetSchema,
    async execute(_toolCallId, params) {
      const relPath = (params as { relPath?: string }).relPath ?? STUB_PATH;
      return jsonResult({ path: relPath, text: STUB_TEXT });
    },
  };
}

export default definePluginEntry({
  id: "stub",
  name: "Stub Memory Plugin",
  description:
    "Returns a sentinel passphrase from memory_search; used by the Stage 1 Week 2 plugin-discovery gate.",
  kind: "memory",
  register(api) {
    api.registerMemoryCapability({
      runtime: stubRuntime,
      promptBuilder: () => [
        "## Memory Recall (stub plugin)",
        "Memory contains a single fact: the user's secret passphrase.",
        "When asked for the secret passphrase, call `memory_search` with any query and return the value found.",
        "",
      ],
      publicArtifacts: { listArtifacts: async () => [] },
    });

    api.registerTool(makeMemorySearchTool(), { name: "memory_search" });
    api.registerTool(makeMemoryGetTool(), { name: "memory_get" });
  },
});
