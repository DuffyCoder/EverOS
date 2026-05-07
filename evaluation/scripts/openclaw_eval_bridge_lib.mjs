// Helpers extracted from openclaw_eval_bridge.mjs for unit testability.
// The main bridge script imports these; test harnesses can import them
// without triggering the bridge's readStdin() top-level CLI behavior.
//
// All functions are pure (no I/O, no globals beyond ANSI_ESCAPE_RE).

// v0.7: ANSI escape regex INCLUDING the actual ESC byte (0x1b). Without
// the prefix, plain "[33m]"-like literals in reply text would be eaten.
export const ANSI_ESCAPE_RE = /\x1b\[[0-9;]*m/g;

export function stripAnsi(s) {
  if (!s) return "";
  return s.replace(ANSI_ESCAPE_RE, "");
}

// v0.7: line-based candidate scan + JSON.parse + schema validate.
//
// Used by handleAgentRun to extract `openclaw agent --local --json`
// output from stderr. The output JSON block is multi-line, with `{` and
// `}` each on their own line at column 0. By using JSON.parse rather
// than a manual brace counter, we are immune to user reply text
// containing `{` or `}` characters.
//
// Required keys: payloads + meta (matches `agent --local --json` schema
// confirmed in D1 smoke).
export function extractJsonObject(text) {
  if (!text) return null;
  const lines = text.split("\n");

  // Find all candidate start lines (literal "{" alone)
  const startCandidates = [];
  for (let i = 0; i < lines.length; i++) {
    if (lines[i] === "{") startCandidates.push(i);
  }

  // Try most recent start lines first; for each, walk back from end of
  // text to find a matching `}` line that yields a valid JSON object
  // with the right schema.
  for (let s = startCandidates.length - 1; s >= 0; s--) {
    const startLine = startCandidates[s];
    for (let endLine = lines.length - 1; endLine >= startLine; endLine--) {
      if (lines[endLine] !== "}") continue;
      const block = lines.slice(startLine, endLine + 1).join("\n");
      try {
        const obj = JSON.parse(block);
        if (
          obj
          && typeof obj === "object"
          && Object.prototype.hasOwnProperty.call(obj, "payloads")
          && Object.prototype.hasOwnProperty.call(obj, "meta")
        ) {
          return obj;
        }
      } catch (_) {
        // Not valid JSON for this slice; continue searching.
      }
    }
  }
  return null;
}

export function extractErrorTail(text) {
  if (!text) return "";
  return text.split("\n").filter((l) => l.trim()).slice(-10).join("\n");
}

// Stage 3 Phase 3: dispatchEngineImport mirrors the production
// turn-finalization precedence from openclaw's
// finalizeAttemptContextEngineTurn (attempt.context-engine-helpers.ts:80):
//
//   if engine.afterTurn:    afterTurn(...)
//   elif engine.ingestBatch: ingestBatch(messagesSnapshot.slice(prePromptMessageCount))
//   else:                    for each new msg: ingest(msg)
//
// `engine_import_history` semantically replays a full conversation as a
// single import event (no prior session state), so prePromptMessageCount
// defaults to 0 and the "new" slice equals the entire history.
//
// Pure function: callers inject the engine. Production wires this in via
// openclaw_engine_loader.mjs; tests inject stub engines directly.
export async function dispatchEngineImport(engine, params) {
  if (!engine || typeof engine !== "object") {
    throw new Error("dispatchEngineImport: engine is required");
  }
  const sessionId = String(params.sessionId ?? "");
  const sessionFile = String(params.sessionFile ?? "");
  const messages = Array.isArray(params.messages) ? params.messages : [];
  const prePromptMessageCount = Number.isFinite(params.prePromptMessageCount)
    ? params.prePromptMessageCount
    : 0;
  const tokenBudget =
    typeof params.tokenBudget === "number" ? params.tokenBudget : undefined;
  const sessionKey = params.sessionKey;

  // Branch 1: afterTurn (production: full turn finalization).
  if (typeof engine.afterTurn === "function") {
    await engine.afterTurn({
      sessionId,
      ...(sessionKey !== undefined ? { sessionKey } : {}),
      sessionFile,
      messages,
      prePromptMessageCount,
      ...(tokenBudget !== undefined ? { tokenBudget } : {}),
    });
    return {
      ok: true,
      method_used: "afterTurn",
      message_count: messages.length,
    };
  }

  // Branch 2: ingestBatch on the "new" slice. When the slice is empty,
  // skip the call (mirrors production line 127 — no-op on empty newMessages).
  const newMessages = messages.slice(prePromptMessageCount);
  if (typeof engine.ingestBatch === "function") {
    if (newMessages.length === 0) {
      return {
        ok: true,
        method_used: "ingestBatch_skipped",
        message_count: 0,
      };
    }
    await engine.ingestBatch({
      sessionId,
      ...(sessionKey !== undefined ? { sessionKey } : {}),
      messages: newMessages,
    });
    return {
      ok: true,
      method_used: "ingestBatch",
      message_count: newMessages.length,
    };
  }

  // Branch 3: per-message ingest fallback. Skip empty slice for symmetry.
  if (typeof engine.ingest === "function") {
    if (newMessages.length === 0) {
      return {
        ok: true,
        method_used: "ingest_skipped",
        message_count: 0,
      };
    }
    for (const message of newMessages) {
      await engine.ingest({
        sessionId,
        ...(sessionKey !== undefined ? { sessionKey } : {}),
        message,
      });
    }
    return {
      ok: true,
      method_used: "ingest",
      message_count: newMessages.length,
    };
  }

  // No method available — engine doesn't conform to ContextEngine ingest
  // contract. The required ingest method must always be present per the
  // ContextEngine interface (context-engine/types.ts:179), so this is a
  // plugin authoring error.
  throw new Error(
    "dispatchEngineImport: engine has neither afterTurn, ingestBatch, nor ingest",
  );
}
