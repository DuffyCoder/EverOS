// Stub context-engine plugin — Stage 3 Phase 4 smoke gate.
//
// Validates Phase 0-2 slot wiring end-to-end: when CONTEXT_ENGINE_PLUGIN_ID
// resolves through entrypoint render → resolved-config → docker env →
// openclaw plugin loader → resolveContextEngine, the agent_run reply
// should contain the sentinel "WOMBAT_42".
//
// The engine implements only the required ContextEngine surface:
//   - assemble(): returns input messages unchanged + a systemPromptAddition
//     that contains WOMBAT_42. Production openclaw concatenates this
//     systemPromptAddition into the agent's system prompt before the LLM
//     call, so a "say the passphrase" prompt yields a reply containing
//     the sentinel.
//   - ingest(): no-op (counts calls in-memory for diagnostics; not
//     observable to callers since smoke gate doesn't assert ingest).
//
// Why no afterTurn/ingestBatch/bootstrap/maintain/compact: these are all
// optional in the ContextEngine contract (context-engine/types.ts:150).
// The smoke gate only cares about (a) the slot resolved correctly and
// (b) assemble() systemPromptAddition flows to the model.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import type {
  AssembleResult,
  ContextEngine,
  ContextEngineInfo,
  IngestResult,
} from "openclaw/plugin-sdk";

const STUB_PASSPHRASE = "WOMBAT_42";

const STUB_INFO: ContextEngineInfo = {
  id: "stub-engine",
  name: "Stub Context Engine",
  version: "0.0.0",
  ownsCompaction: false,
};

function makeStubEngine(): ContextEngine {
  let ingestCount = 0;

  return {
    info: STUB_INFO,

    async assemble(params): Promise<AssembleResult> {
      // Pass through the supplied messages without modification — the
      // smoke gate doesn't test history reshaping. The systemPromptAddition
      // is the load-bearing observable that asserts the slot resolved
      // and the engine ran.
      const systemPromptAddition =
        `## Context Engine Sentinel\n` +
        `The smoke-gate passphrase is: ${STUB_PASSPHRASE}.\n` +
        `When asked to repeat the passphrase, reply with: ${STUB_PASSPHRASE}.\n`;

      return {
        messages: params.messages,
        estimatedTokens: 0,
        systemPromptAddition,
      };
    },

    async ingest(_params): Promise<IngestResult> {
      ingestCount += 1;
      return { ingested: true };
    },
  };
}

export default definePluginEntry({
  id: "stub-engine",
  name: "Stub Context Engine Plugin",
  description:
    "Phase 4 smoke gate: a context-engine plugin that injects a sentinel passphrase via assemble().systemPromptAddition.",
  kind: "context-engine",
  register(api) {
    api.registerContextEngine("stub-engine", () => makeStubEngine());
  },
});
