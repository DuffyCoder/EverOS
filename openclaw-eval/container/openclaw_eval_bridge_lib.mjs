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
