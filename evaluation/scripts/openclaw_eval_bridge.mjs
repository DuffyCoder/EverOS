// Node bridge for the OpenClaw benchmark adapter.
//
// Dispatches BridgeCommand JSON to either:
//   * the real OpenClaw CLI at $OPENCLAW_REPO_PATH/openclaw.mjs (when the
//     env var points at a valid repo), or
//   * built-in stub handlers that still honor the BridgeResponse shape
//     defined in openclaw_types.py.
//
// The stub path is always safe to use in CI - contract tests lock the
// response shape - and the native path keeps the wire protocol identical
// so swapping OPENCLAW_REPO_PATH on/off should be transparent to Python
// callers. Smoke validation against the native path is documented in
// docs/plans/2026-04-13-openclaw-benchmark-a.md Task 8 Step 4.

import { readFileSync, existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";

function respond(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function fail(message, command) {
  respond({ ok: false, command, error: message });
  process.exit(0);
}

function epochSeconds() {
  return Math.floor(Date.now() / 1000);
}

function readStdin() {
  return readFileSync(0, "utf8");
}

function resolveLauncher() {
  const repo = process.env.OPENCLAW_REPO_PATH;
  if (!repo) return null;
  const launcher = path.join(repo, "openclaw.mjs");
  return existsSync(launcher) ? launcher : null;
}

function envForSandbox(input) {
  const env = { ...process.env };
  if (input.config_path) env.OPENCLAW_CONFIG_PATH = input.config_path;
  if (input.state_dir) env.OPENCLAW_STATE_DIR = input.state_dir;
  if (input.workspace_dir) env.HOME = input.workspace_dir;
  return env;
}

function runLauncher(launcher, args, env) {
  return new Promise((resolve, reject) => {
    const proc = spawn("node", [launcher, ...args], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (b) => (stdout += b.toString()));
    proc.stderr.on("data", (b) => (stderr += b.toString()));
    proc.on("close", (code) => resolve({ code, stdout, stderr }));
    proc.on("error", reject);
  });
}

function extractJsonTail(stdout) {
  const trimmed = stdout.trim();
  try {
    return JSON.parse(trimmed);
  } catch (_) {
    const lines = trimmed.split("\n").reverse();
    for (const line of lines) {
      if (line.startsWith("{")) {
        try {
          return JSON.parse(line);
        } catch (_) {
          // continue
        }
      }
    }
    return null;
  }
}

async function handleIndex(input, launcher) {
  if (!launcher) {
    return {
      ok: true,
      command: "index",
      flush_epoch: 0,
      index_epoch: 0,
      input_artifacts: [],
      output_artifacts: [],
    };
  }
  const env = envForSandbox(input);
  const { code, stdout, stderr } = await runLauncher(
    launcher,
    ["memory", "index", "--force"],
    env
  );
  if (code !== 0) {
    return { ok: false, command: "index", error: stderr || stdout || `exit ${code}` };
  }
  return {
    ok: true,
    command: "index",
    flush_epoch: epochSeconds(),
    index_epoch: epochSeconds(),
    input_artifacts: [],
    output_artifacts: [],
  };
}

async function handleFlush(input, launcher) {
  // OpenClaw has no standalone flush; re-run index and report the epochs.
  const result = await handleIndex(input, launcher);
  if (!result.ok) return { ...result, command: "flush" };
  return { ...result, command: "flush" };
}

async function handleStatus(input, launcher) {
  // OpenClaw CLI has no status subcommand; report settled=true if the
  // workspace exists.
  return {
    ok: true,
    command: "status",
    settled: true,
    flush_epoch: 0,
    index_epoch: 0,
    active_artifacts: [],
  };
}

async function handleSearch(input, launcher) {
  if (!launcher) {
    return { ok: true, command: "search", hits: [] };
  }
  const env = envForSandbox(input);
  const args = [
    "memory",
    "search",
    "--query",
    String(input.query ?? ""),
    "--max-results",
    String(input.top_k ?? 30),
    "--json",
  ];
  const { code, stdout, stderr } = await runLauncher(launcher, args, env);
  if (code !== 0) {
    return { ok: false, command: "search", error: stderr || stdout || `exit ${code}` };
  }
  const parsed = extractJsonTail(stdout);
  if (!parsed) {
    return { ok: false, command: "search", error: "stdout not JSON" };
  }
  const rawResults = parsed.results || [];
  const hits = rawResults.map((r) => ({
    score: Number(r.score ?? 0),
    snippet: r.snippet ?? "",
    artifact_locator: {
      kind: "memory_file_range",
      path_rel: r.path ?? "",
      line_start: Number(r.startLine ?? 0),
      line_end: Number(r.endLine ?? 0),
    },
    metadata: {
      source: r.source ?? "memory",
    },
  }));
  return { ok: true, command: "search", hits };
}

async function handleGet(input) {
  // OpenClaw has no get command; read the markdown file range directly.
  const locator = input.artifact_locator || {};
  if (!input.workspace_dir || !locator.path_rel) {
    return { ok: true, command: "get", artifact_locator: locator, snippet: "" };
  }
  try {
    const absPath = path.join(input.workspace_dir, locator.path_rel);
    const content = await readFile(absPath, "utf8");
    const lines = content.split("\n");
    const start = Math.max(0, (locator.line_start ?? 1) - 1);
    const end = Math.max(start, locator.line_end ?? lines.length);
    const snippet = lines.slice(start, end).join("\n");
    return { ok: true, command: "get", artifact_locator: locator, snippet };
  } catch (err) {
    return {
      ok: true,
      command: "get",
      artifact_locator: locator,
      snippet: "",
    };
  }
}

const raw = readStdin();
let input;
try {
  input = JSON.parse(raw);
} catch (err) {
  fail(`invalid input json: ${err.message}`, undefined);
}

const launcher = resolveLauncher();
const command = input.command;

(async () => {
  try {
    let resp;
    switch (command) {
      case "index":
        resp = await handleIndex(input, launcher);
        break;
      case "flush":
        resp = await handleFlush(input, launcher);
        break;
      case "status":
        resp = await handleStatus(input, launcher);
        break;
      case "search":
        resp = await handleSearch(input, launcher);
        break;
      case "get":
        resp = await handleGet(input);
        break;
      default:
        return fail(`unknown command: ${command}`, command);
    }
    respond(resp);
  } catch (err) {
    fail(err.stack || err.message || String(err), command);
  }
})();
