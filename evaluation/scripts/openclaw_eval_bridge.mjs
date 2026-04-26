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
import { pathToFileURL } from "node:url";

import {
  stripAnsi,
  extractJsonObject,
  extractErrorTail,
} from "./openclaw_eval_bridge_lib.mjs";

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

function resolveLauncher(input) {
  // Prefer repo_path from the BridgeCommand payload so the system YAML
  // actually drives which OpenClaw repo we spawn. Fall back to
  // OPENCLAW_REPO_PATH env for developer convenience (and for the stub
  // contract tests which intentionally leave the env unset).
  const repo =
    (input && typeof input.repo_path === "string" && input.repo_path.trim())
      ? input.repo_path.trim()
      : process.env.OPENCLAW_REPO_PATH;
  if (!repo) return null;
  const launcher = path.join(repo, "openclaw.mjs");
  return existsSync(launcher) ? launcher : null;
}

function envForSandbox(input) {
  // Minimal env - mirrors v0.1/v0.2 isolation. Inheriting the full parent
  // env would leak OPENAI_API_KEY etc into OpenClaw's auto-provider
  // selection, which we explicitly do NOT want.
  //
  // v0.7: agent_llm_env_vars is the explicit whitelist for env passthrough
  // when answer_mode=agent_local. The resolved config carries ${VAR}
  // template strings for secrets (apiKey), so OpenClaw resolves them at
  // startup against the env this function provides. Without the whitelist
  // OpenClaw throws MissingEnvVarError on unresolved templates.
  const env = {
    PATH: process.env.PATH || "",
    HOME: input.home_dir || input.workspace_dir || "",
    NODE_OPTIONS: "",
    NPM_CONFIG_USERCONFIG: "/dev/null",
    NPM_CONFIG_GLOBALCONFIG: "/dev/null",
  };
  if (input.config_path) env.OPENCLAW_CONFIG_PATH = input.config_path;
  if (input.state_dir) env.OPENCLAW_STATE_DIR = input.state_dir;

  // v0.7: explicit env whitelist - only listed names are passed through.
  if (Array.isArray(input.agent_llm_env_vars)) {
    for (const name of input.agent_llm_env_vars) {
      if (typeof name !== "string") continue;
      // Validate: env var names should match openclaw SecretRef regex
      // (uppercase + digits + underscore, starts with uppercase). This
      // prevents accidentally listing unrelated entries like full paths.
      if (!/^[A-Z][A-Z0-9_]{0,127}$/.test(name)) continue;
      const value = process.env[name];
      if (value !== undefined) env[name] = value;
    }
  }

  return env;
}

function cwdForSandbox(input) {
  return input.cwd_dir || input.workspace_dir || undefined;
}

function runLauncher(launcher, args, env, cwd) {
  return new Promise((resolve, reject) => {
    const proc = spawn("node", [launcher, ...args], {
      env,
      cwd,
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
  const cwd = cwdForSandbox(input);
  const { code, stdout, stderr } = await runLauncher(
    launcher,
    ["memory", "index", "--force"],
    env,
    cwd
  );
  if (code !== 0) {
    const tail = [stderr, stdout].filter((s) => s && s.trim()).join("\n---\n");
    return { ok: false, command: "index", error: tail || `exit ${code}` };
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
  if (!launcher) {
    return {
      ok: true,
      command: "status",
      settled: true,
      flush_epoch: 0,
      index_epoch: 0,
      active_artifacts: [],
    };
  }
  const env = envForSandbox(input);
  const cwd = cwdForSandbox(input);
  const { code, stdout, stderr } = await runLauncher(
    launcher,
    ["memory", "status", "--json"],
    env,
    cwd
  );
  if (code !== 0) {
    const tail = [stderr, stdout].filter((s) => s && s.trim()).join("\n---\n");
    return { ok: false, command: "status", error: tail || `exit ${code}` };
  }
  const parsed = extractJsonTail(stdout);
  if (!parsed) {
    return { ok: false, command: "status", error: "stdout not JSON" };
  }
  // OpenClaw's `memory status --json` returns an array:
  //   [{ agentId: "main", status: { backend, files, chunks, dirty, dbPath, ... }}]
  // Map to our BridgeResponse shape with a best-effort `settled` flag.
  const agentStatus = Array.isArray(parsed) ? (parsed[0] || {}).status : parsed.status;
  const s = agentStatus || {};
  const settled = s.dirty === false;
  return {
    ok: true,
    command: "status",
    settled,
    files: Number(s.files || 0),
    chunks: Number(s.chunks || 0),
    backend: s.backend || null,
    provider: s.provider || null,
    flush_epoch: Number(s.lastFlushEpoch || 0),
    index_epoch: Number(s.lastIndexEpoch || 0),
    active_artifacts: [],
    native: true,
  };
}

async function handleSearch(input, launcher) {
  if (!launcher) {
    return { ok: true, command: "search", hits: [] };
  }
  const env = envForSandbox(input);
  const cwd = cwdForSandbox(input);
  const args = [
    "memory",
    "search",
    "--query",
    String(input.query ?? ""),
    "--max-results",
    String(input.top_k ?? 30),
    "--json",
  ];
  const { code, stdout, stderr } = await runLauncher(launcher, args, env, cwd);
  if (code !== 0) {
    const tail = [stderr, stdout].filter((s) => s && s.trim()).join("\n---\n");
    return { ok: false, command: "search", error: tail || `exit ${code}` };
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

// v0.7: agent --local one-shot agent run. Used by Path B answer mode.
//
// Stdout is empty; the structured JSON block lands on stderr after some
// plugin warning lines (D1 smoke confirmed). We strip ANSI escapes and
// scan stderr for the last well-formed `{payloads, meta}` block.
//
// Stub mode (no launcher) returns a deterministic shape so contract
// tests can assert response keys without spawning real openclaw.
async function handleAgentRun(input, launcher) {
  if (!launcher) {
    return {
      ok: true,
      command: "agent_run",
      reply: "[stub] agent_run reply",
      raw: {
        payloads: [{ text: "[stub] agent_run reply", mediaUrl: null }],
        meta: { stub: true },
      },
      duration_ms: 0,
      aborted: false,
      stop_reason: "stub",
      tool_names: [],
      system_prompt_chars: 0,
      last_call_usage: null,
    };
  }

  const env = envForSandbox(input);
  const cwd = cwdForSandbox(input);
  const args = [
    "agent",
    "--local",
    "--session-id",
    String(input.session_id ?? ""),
    "--message",
    String(input.message ?? ""),
    "--json",
    "--timeout",
    String(input.timeout_seconds ?? 180),
  ];

  const { code, stdout, stderr } = await runLauncher(launcher, args, env, cwd);

  // openclaw agent --local --json puts the JSON on stderr; stdout is empty.
  // Fall back to stdout if stderr is empty (e.g. behavior changes upstream).
  const merged = stripAnsi(stderr || "") || stripAnsi(stdout || "");

  if (code !== 0) {
    return {
      ok: false,
      command: "agent_run",
      error: extractErrorTail(merged) || `exit ${code}`,
    };
  }

  const parsed = extractJsonObject(merged);
  if (!parsed) {
    return {
      ok: false,
      command: "agent_run",
      error: "no valid JSON object (with payloads+meta) found in stderr",
    };
  }

  const reply = parsed.payloads?.[0]?.text ?? "";
  const meta = parsed.meta || {};
  return {
    ok: true,
    command: "agent_run",
    reply,
    raw: parsed,
    duration_ms: meta.durationMs ?? null,
    aborted: meta.aborted ?? false,
    stop_reason: meta.stopReason ?? null,
    tool_names: (meta.systemPromptReport?.tools?.entries || []).map((t) => t.name),
    system_prompt_chars: meta.systemPromptReport?.systemPrompt?.chars ?? null,
    last_call_usage: meta.agentMeta?.lastCallUsage ?? null,
  };
}

async function handleBuildFlushPlan(input, launcher) {
  // Returns OpenClaw's native memory-flush plan (the canonical system/user
  // prompt that the production agent-runner would send when flushing
  // memory pre-compaction). Executor is still the framework's LLM per the
  // scope of Option A; only the plan/prompt side is native.
  if (!launcher) {
    // Stub path: mirror the real response shape with an obvious sentinel
    // so callers can tell they're not hitting upstream.
    return {
      ok: true,
      command: "build_flush_plan",
      native: false,
      silent_token: "NO_REPLY",
      relative_path: "memory/stub-date.md",
      soft_threshold_tokens: 4000,
      system_prompt: "[stub] shared_llm placeholder system prompt.",
      prompt: "[stub] shared_llm placeholder user prompt.",
    };
  }

  // Dynamic import from the launcher's dist. We resolve relative to the
  // launcher path so a different OPENCLAW_REPO_PATH works out of the box.
  const repoRoot = path.dirname(launcher);
  const distIndex = path.join(
    repoRoot,
    "dist",
    "extensions",
    "memory-core",
    "index.js",
  );
  if (!existsSync(distIndex)) {
    return {
      ok: false,
      command: "build_flush_plan",
      error: `memory-core dist not found at ${distIndex}`,
    };
  }

  let mod;
  try {
    mod = await import(pathToFileURL(distIndex).href);
  } catch (err) {
    return {
      ok: false,
      command: "build_flush_plan",
      error: `failed to import memory-core: ${err.message}`,
    };
  }

  let cfg;
  if (input.config_path && existsSync(input.config_path)) {
    try {
      cfg = JSON.parse(readFileSync(input.config_path, "utf8"));
    } catch (err) {
      return {
        ok: false,
        command: "build_flush_plan",
        error: `bad config at ${input.config_path}: ${err.message}`,
      };
    }
  }

  const nowMs = Number.isFinite(input.now_ms) ? input.now_ms : Date.now();
  const plan = mod.buildMemoryFlushPlan({ cfg, nowMs });
  if (!plan) {
    return {
      ok: true,
      command: "build_flush_plan",
      native: true,
      disabled: true,
      silent_token: "NO_REPLY",
      relative_path: null,
      soft_threshold_tokens: mod.DEFAULT_MEMORY_FLUSH_SOFT_TOKENS ?? 4000,
      system_prompt: null,
      prompt: null,
    };
  }
  return {
    ok: true,
    command: "build_flush_plan",
    native: true,
    silent_token: "NO_REPLY",
    relative_path: plan.relativePath,
    soft_threshold_tokens: plan.softThresholdTokens,
    system_prompt: plan.systemPrompt,
    prompt: plan.prompt,
  };
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

const launcher = resolveLauncher(input);
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
      case "build_flush_plan":
        resp = await handleBuildFlushPlan(input, launcher);
        break;
      case "agent_run":
        resp = await handleAgentRun(input, launcher);
        break;
      default:
        return fail(`unknown command: ${command}`, command);
    }
    respond(resp);
  } catch (err) {
    fail(err.stack || err.message || String(err), command);
  }
})();
