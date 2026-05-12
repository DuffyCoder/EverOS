// Node bridge for the OpenClaw benchmark adapter.
//
// Dispatches BridgeCommand JSON to either:
//   * the real OpenClaw CLI at $OPENCLAW_REPO_PATH/openclaw.mjs (when the
//     env var points at a valid repo), or
//   * built-in stub handlers that still honor the BridgeResponse shape
//     defined in evaluation/src/adapters/openclaw/types.py.
//
// The stub path is always safe to use in CI - contract tests lock the
// response shape - and the native path keeps the wire protocol identical
// so swapping OPENCLAW_REPO_PATH on/off should be transparent to Python
// callers. Smoke validation against the native path is documented in
// docs/plans/2026-04-13-openclaw-benchmark-a.md Task 8 Step 4.

import { readFileSync, existsSync, readdirSync } from "node:fs";
import { readFile, rename } from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";

// --------------------------------------------------------------------
// Form B sidecar routing (mem0/evermemos/zep): when the container ships
// /sidecar/server.py, the bridge bypasses memory-core's CLI for index/
// status because memory-core's plugin entry is disabled in those modes
// and `openclaw memory ...` would fail with "plugin not found". Instead
// we POST to the sidecar's HTTP API directly. On host (no /sidecar/),
// hasSidecar() returns false and this code is dead but harmless. Kept
// here so container/ stays a faithful mirror of evaluation/scripts/.
// --------------------------------------------------------------------
const SIDECAR_SCRIPT = "/sidecar/server.py";
const SIDECAR_BASE_URL = process.env.MEM0_SIDECAR_URL || "http://127.0.0.1:8765";
const SIDECAR_TIMEOUT_MS = Number(process.env.MEM0_SIDECAR_TIMEOUT_MS || 90000);
const SIDECAR_INDEX_TIMEOUT_MS = Number(process.env.MEM0_INDEX_TIMEOUT_MS || 900000);

function hasSidecar() {
  return existsSync(SIDECAR_SCRIPT);
}

async function sidecarRequest(method, urlPath, body, timeoutMs = SIDECAR_TIMEOUT_MS) {
  const url = SIDECAR_BASE_URL + (urlPath.startsWith("/") ? urlPath : `/${urlPath}`);
  const init = {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  };
  const res = await fetch(url, init);
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`sidecar ${method} ${urlPath} returned ${res.status}: ${text.slice(0, 256)}`);
  }
  return text ? JSON.parse(text) : {};
}

// Once /healthz returns ok within a process the sidecar can't unbind, so
// cache the readiness signal to skip the GET on subsequent index/status
// calls.
let _sidecarReady = false;

async function waitForSidecarReady(maxWaitMs = 60000) {
  if (_sidecarReady) return;
  const deadline = Date.now() + maxWaitMs;
  let lastError = "not yet polled";
  while (Date.now() < deadline) {
    try {
      const res = await sidecarRequest("GET", "/healthz", undefined, 2000);
      if (res?.ok === true) {
        _sidecarReady = true;
        return;
      }
    } catch (err) {
      lastError = err?.message || String(err);
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`sidecar /healthz did not respond within ${maxWaitMs}ms: ${lastError}`);
}

import {
  stripAnsi,
  extractJsonObject,
  extractErrorTail,
  dispatchEngineImport,
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
  // Pass through OPENCLAW_HOME so install-mode plugins (Stage 3 Phase 5)
  // resolve their extension dirs. Bridge's envForSandbox otherwise strips
  // this env, causing plugin loader to fall back to $HOME/.openclaw which
  // doesn't exist in the workspace mount.
  if (process.env.OPENCLAW_HOME) env.OPENCLAW_HOME = process.env.OPENCLAW_HOME;

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

function runLauncher(launcher, args, env, cwd, opts = {}) {
  // Stage 3 Phase 5: hypercompositor + hypermem (and other context-engine
  // plugins with background indexer tasks via setInterval) keep the Node
  // event loop alive after agent --local prints its JSON, so proc.on("close")
  // never fires within a reasonable bound. Add a hard wall-clock kill after
  // `hardTimeoutMs` (default 120s) so the bridge always returns within
  // bounded time. The collected stdout/stderr at kill time is what we
  // already received during streaming, which is enough for JSON tail
  // extraction.
  const hardTimeoutMs = opts.hardTimeoutMs ?? 120_000;
  return new Promise((resolve, reject) => {
    // detached:true puts the spawned node in its own process group. The
    // openclaw CLI forks `openclaw-agent` subprocesses; they inherit the
    // same group. On hard-timeout we send SIGKILL to the whole group via
    // -pgid so the subagent dies even if the parent already exited (which
    // is what hypermem's setInterval-keepalive scenario produces). Without
    // this, the grandchild keeps stderr fd open and proc.on("close") never
    // fires.
    const proc = spawn("node", [launcher, ...args], {
      env,
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });
    // Detached children must be unref'd in case their stdio doesn't close,
    // so the parent (this bridge) can exit too.
    proc.unref();

    let stdout = "";
    let stderr = "";
    let resolved = false;
    let killTimer = null;
    let waitForCloseTimer = null;

    const killGroup = (signal) => {
      try {
        // Negative pid kills the entire process group.
        process.kill(-proc.pid, signal);
      } catch (_) {
        try {
          proc.kill(signal);
        } catch (_) {}
      }
    };

    const finish = (code, killed) => {
      if (resolved) return;
      resolved = true;
      if (killTimer) clearTimeout(killTimer);
      if (waitForCloseTimer) clearTimeout(waitForCloseTimer);
      resolve({ code, stdout, stderr, killed: killed === true });
    };

    proc.stdout.on("data", (b) => (stdout += b.toString()));
    proc.stderr.on("data", (b) => (stderr += b.toString()));
    proc.on("close", (code) => finish(code, false));
    proc.on("error", (err) => {
      if (!resolved) reject(err);
    });

    killTimer = setTimeout(() => {
      killGroup("SIGTERM");
      waitForCloseTimer = setTimeout(() => {
        killGroup("SIGKILL");
        // Last-ditch: resolve even if "close" still doesn't fire (defensive).
        setTimeout(() => finish(null, true), 1_000);
      }, 2_000);
    }, hardTimeoutMs);
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

async function handleIndexViaSidecar(input) {
  // Walk <workspace>/memory/*.md and POST the relative paths to the
  // sidecar's /sync. The sidecar reads each file and pushes it through
  // the upstream memory SDK (mem0 / evermemos / zep). This replaces the
  // memory-core CLI ingest path for Form B plugins.
  const workspaceDir = input.workspace_dir;
  const memoryDir = path.join(workspaceDir, "memory");
  let sessionFiles = [];
  try {
    sessionFiles = readdirSync(memoryDir)
      .filter((name) => name.endsWith(".md"))
      .map((name) => `memory/${name}`)
      .sort();
  } catch {
    // memory dir absent — nothing to ingest, sidecar /sync should still ok
  }
  try {
    await waitForSidecarReady();
    const data = await sidecarRequest("POST", "/sync", {
      reason: "eval_index",
      force: true,
      session_files: sessionFiles,
    }, SIDECAR_INDEX_TIMEOUT_MS);
    // The sidecar returns HTTP 200 even on logical failures — e.g. a
    // failed force-reset surfaces as `{ok:false, error:...}`. Without
    // this check the pipeline would continue with stale/duplicated
    // memory state, masking the upstream failure.
    if (data && data.ok === false) {
      return {
        ok: false,
        command: "index",
        error: `sidecar /sync reported failure: ${data.error || "unknown"}`,
        sidecar: { ingested: data.ingested ?? 0 },
      };
    }
    return {
      ok: true,
      command: "index",
      flush_epoch: epochSeconds(),
      index_epoch: epochSeconds(),
      input_artifacts: sessionFiles,
      output_artifacts: [],
      sidecar: { ingested: data?.ingested ?? null },
    };
  } catch (err) {
    return {
      ok: false,
      command: "index",
      error: `sidecar index failed: ${err?.message || err}`,
    };
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
  if (hasSidecar()) {
    return await handleIndexViaSidecar(input);
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

async function handleStatusViaSidecar(input) {
  try {
    await waitForSidecarReady();
    const stats = await sidecarRequest("GET", "/stats");
    return {
      ok: true,
      command: "status",
      settled: stats?.dirty === false,
      files: Number(stats?.files || 0),
      chunks: Number(stats?.chunks || 0),
      backend: stats?.provider || "mem0",
      active_artifacts: [],
    };
  } catch (err) {
    return {
      ok: false,
      command: "status",
      error: `sidecar status failed: ${err?.message || err}`,
    };
  }
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
  if (hasSidecar()) {
    return await handleStatusViaSidecar(input);
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

  // Hard timeout = agent's own --timeout + buffer for hypercompositor-style
  // background indexers. Without this, `proc.on("close")` never fires and
  // the bridge appears to hang from the caller's perspective.
  const hardTimeoutMs = (Number(input.timeout_seconds ?? 180) + 15) * 1000;
  const { code, stdout, stderr, killed } = await runLauncher(launcher, args, env, cwd, { hardTimeoutMs });

  // openclaw agent --local --json puts the JSON on stderr; stdout is empty.
  // Fall back to stdout if stderr is empty (e.g. behavior changes upstream).
  const merged = stripAnsi(stderr || "") || stripAnsi(stdout || "");

  // Try to parse before short-circuiting on non-zero exit. Stage 3 plugins
  // (hypercompositor + hypermem) keep background indexer timers alive after
  // stopReason: "stop", forcing runLauncher to SIGTERM/SIGKILL the proc.
  // The JSON tail is already complete on stderr by then, so accept it as
  // the canonical response rather than blaming the kill on the agent.
  const parsed = extractJsonObject(merged);

  if (!parsed) {
    return {
      ok: false,
      command: "agent_run",
      error: extractErrorTail(merged) ||
        (killed ? "agent killed by hard timeout before JSON arrived" :
                  `exit ${code}: no JSON object (with payloads+meta) found in stderr`),
    };
  }

  // We have a parsed agent response. Non-zero exit codes from a clean
  // agent run (i.e. JSON present) are typically from forced termination
  // due to lingering background timers; surface them via `killed` rather
  // than failing the whole call.

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
    forced_terminate: killed === true,
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

// Stage 3 Phase 3: engine_import_history bridge handler.
//
// Stub mode (no launcher): returns a deterministic shape so contract tests
// can pass without the openclaw runtime. This is also the path used when
// OPENCLAW_REPO_PATH is unset (e.g. CI without the repo cloned).
//
// Native mode (launcher present): production wiring is intentionally
// deferred. Empirical finding (2026-05-02): openclaw's compiled `dist/` is
// a flat hashed bundle (e.g. registry-D4L8wbCo.js) and does NOT expose
// `resolveContextEngine` / `resolveRuntimePluginRegistry` via the public
// `exports` map (255 exports surveyed; only `loadConfig` is reachable from
// `dist/index.js`). To run the engine in-process we'd need either:
//   (a) an upstream patch adding stable `./context-engine` or
//       `./eval-harness/import-history` exports, or
//   (b) brittle hash-discovery (glob `dist/registry-*.js` and import the
//       munged `r` symbol — rebuild-fragile),
// or (c) ingest by replaying messages through the existing `agent_run`
// CLI pipeline (each message becomes a turn, engine's afterTurn fires
// natively — but burns LLM tokens).
//
// Path (a) is the right answer; tracked in plan Phase 3 follow-up. Until
// then, native mode returns ok:false with a clear marker so adapters can
// branch. The dispatcher unit tests still cover the precedence contract.
async function handleEngineImportHistory(input, launcher) {
  const sessionId = String(input.session_id ?? "");
  const messages = Array.isArray(input.messages) ? input.messages : [];

  if (!launcher) {
    // Stub path — used by Phase 4 smoke gate where the engine is loaded
    // by openclaw's normal CLI startup (not via this RPC). Returning
    // ok:true with method_used=stub keeps the wire shape contract.
    return {
      ok: true,
      command: "engine_import_history",
      native: false,
      method_used: "stub",
      message_count: messages.length,
      session_id: sessionId,
    };
  }

  // Native production path is deferred (see header comment). Surface the
  // status explicitly so adapters/tests don't silently assume success.
  return {
    ok: false,
    command: "engine_import_history",
    native: true,
    method_used: null,
    error:
      "engine_import_history native path not yet wired — openclaw " +
      "dist/ does not expose resolveContextEngine via stable exports. " +
      "Consider replaying messages via agent_run in the meantime.",
  };
}

async function handleArchiveSession(input) {
  // Archive an openclaw session jsonl so the next agent_run with the same
  // session_id starts with an empty transcript. Memory under
  // <state_dir>/agents/main/sessions/<sid>.jsonl is renamed to
  // <sid>.jsonl.<ts>; memory/*.md files are untouched, so memory persists
  // across the archive. Mirrors reference openclaw-eval/eval.py reset_session.
  const sessionId = input.session_id;
  const stateDir = input.state_dir;
  if (!sessionId || !stateDir) {
    return {
      ok: false,
      command: "archive_session",
      error: "session_id and state_dir are required",
    };
  }
  const src = path.join(stateDir, "agents", "main", "sessions", `${sessionId}.jsonl`);
  if (!existsSync(src)) {
    return {
      ok: false,
      command: "archive_session",
      reason: "no transcript",
      session_id: sessionId,
    };
  }
  const dst = `${src}.${epochSeconds()}`;
  try {
    await rename(src, dst);
    return {
      ok: true,
      command: "archive_session",
      session_id: sessionId,
      archived_to: path.basename(dst),
    };
  } catch (err) {
    return {
      ok: false,
      command: "archive_session",
      session_id: sessionId,
      error: err?.message || String(err),
    };
  }
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
      case "engine_import_history":
        resp = await handleEngineImportHistory(input, launcher);
        break;
      case "archive_session":
        resp = await handleArchiveSession(input);
        break;
      default:
        return fail(`unknown command: ${command}`, command);
    }
    respond(resp);
  } catch (err) {
    fail(err.stack || err.message || String(err), command);
  }
})();
