#!/bin/bash
# Render /etc/openclaw/template.json -> /workspace/openclaw.json with the
# correct plugin section based on MEMORY_PLUGIN_ID + MEMORY_MODE, then exec
# the passed CMD (default: sleep infinity).
#
# The harness drives `docker exec` per RPC; this script just bootstraps
# config and keeps the container alive.
#
# v0.7 secret hygiene: yaml/template uses ${LLM_API_KEY} / ${SOPH_API_KEY}
# template strings; openclaw resolves them from this container's env at
# CLI invocation time. This script does NOT substitute secret values into
# the file written to disk.

set -euo pipefail

PLUGIN="${MEMORY_PLUGIN_ID:-memory-core}"
MODE="${MEMORY_MODE:-memory-core}"
WS="${WORKSPACE_DIR:-/workspace}"
TPL="${TEMPLATE_PATH:-/etc/openclaw/template.json}"
OUT="${OPENCLAW_CONFIG_PATH:-/workspace/openclaw.json}"

# Determine plugin allow / slot / entries based on (PLUGIN, MODE).
if [ "$PLUGIN" = "memory-core" ]; then
  PLUGIN_ALLOW='["memory-core"]'
  MEMORY_SLOT='memory-core'
  PLUGIN_ENTRIES='{"memory-core": {"enabled": true}}'
elif [ "$PLUGIN" = "noop" ]; then
  # noop loads memory-core but disables memorySearch via the enabled flag below.
  PLUGIN_ALLOW='["memory-core"]'
  MEMORY_SLOT='memory-core'
  PLUGIN_ENTRIES='{"memory-core": {"enabled": true}}'
else
  PLUGIN_ALLOW='["memory-core","'$PLUGIN'"]'
  MEMORY_SLOT="$PLUGIN"
  PLUGIN_ENTRIES='{"memory-core": {"enabled": false}, "'$PLUGIN'": {"enabled": true}}'
fi

# Stage 3 Phase 1: optionally splice in the context-engine plugin.
# When CONTEXT_ENGINE_PLUGIN_ID is set, we (a) add it to plugins.allow,
# (b) register an enabled entry, (c) emit slots.contextEngine via jq below.
# When unset, no behavioral change vs bundled memory mode (Phase 0
# re-audit confirmed openclaw resolves slots.contextEngine to "legacy"
# default when the slot key is absent from the rendered config).
CONTEXT_ENGINE_PLUGIN="${CONTEXT_ENGINE_PLUGIN_ID:-}"
if [ -n "$CONTEXT_ENGINE_PLUGIN" ]; then
  # Splice ce plugin id into PLUGIN_ALLOW (jq makes the union safe).
  PLUGIN_ALLOW=$(echo "$PLUGIN_ALLOW" | jq --arg ce "$CONTEXT_ENGINE_PLUGIN" '. + [$ce] | unique')
  # Add entry to PLUGIN_ENTRIES — enabled so the loader actually runs
  # the plugin's register() and registerContextEngine() fires.
  PLUGIN_ENTRIES=$(echo "$PLUGIN_ENTRIES" | jq --arg ce "$CONTEXT_ENGINE_PLUGIN" \
    '.[$ce] = {"enabled": true}')
fi

# memorySearch.enabled boolean as JSON literal (not a string).
if [ "$MODE" = "noop" ]; then
  MEMORY_SEARCH_ENABLED='false'
else
  MEMORY_SEARCH_ENABLED='true'
fi

# memoryFlush controls (Phase 1: agent_replay support).
#
# Env vars (passed by openclaw_docker_adapter._docker_env_for_container
# from yaml openclaw.compaction_overrides):
#   MEMORY_FLUSH_ENABLED                  bool, default "false" (preserves
#                                         legacy behavior — fake flush at
#                                         ingest, no native turn-end flush).
#   MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS    int, optional. softThresholdTokens
#                                         override; openclaw default 4000.
#   MEMORY_FLUSH_RESERVE_TOKENS_FLOOR     int, optional. reserveTokensFloor
#                                         override; openclaw default 20000.
#   MEMORY_FLUSH_FORCE_FLUSH_TRANSCRIPT_BYTES   int|str, optional. byte
#                                         threshold for force flush; openclaw
#                                         default 2MB. Accepts numbers or
#                                         strings ("100kb", "2mb").
#
# Empty values mean "don't override" — the field is omitted from the
# rendered config, so OpenClaw's own defaults from
# extensions/memory-core/src/flush-plan.ts apply.
MEMORY_FLUSH_ENABLED="${MEMORY_FLUSH_ENABLED:-false}"
MEMORY_FLUSH_SOFT="${MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS:-}"
MEMORY_FLUSH_RESERVE="${MEMORY_FLUSH_RESERVE_TOKENS_FLOOR:-}"
MEMORY_FLUSH_FORCE_BYTES="${MEMORY_FLUSH_FORCE_FLUSH_TRANSCRIPT_BYTES:-}"

# Model id/name defaults (can be overridden via env).
LLM_MODEL_ID="${LLM_MODEL:-gpt-4.1-mini}"
LLM_MODEL_NAME="${LLM_MODEL_NAME:-${LLM_MODEL_ID} (sophnet)}"

# Workspace + state dirs must exist before openclaw indexes anything.
mkdir -p "$WS" "$WS/state/memory" "$WS/home" "$WS/memory"

# Render template using jq. `--argjson` for JSON-typed values, `--arg` for strings.
# We do TWO passes: first jq to substitute the JSON-typed sentinel placeholders
# (plugins.allow, plugins.slots.memory, plugins.entries, memorySearch.enabled);
# then a sed pass for string substitutions (LLM_MODEL_ID, LLM_MODEL_NAME, paths).
# We deliberately leave ${LLM_API_KEY} / ${SOPH_API_KEY} / ${LLM_BASE_URL} /
# ${SOPH_EMBED_URL} / ${SOPH_EMBED_EASYLLM_ID} as ${VAR} templates so openclaw
# resolves them from process env at CLI time. This keeps secrets off disk.
# Install-mode (build.py --install-spec): if INSTALL_PLUGIN_ID is set
# and the install dir exists, register it via plugins.load.paths so the
# runtime can find the installed plugin alongside any bundled ones.
# Empty/missing -> jq emits no load section (default behavior).
INSTALL_LOAD_PATHS='[]'
OPENCLAW_HOME_DIR="${OPENCLAW_HOME:-/opt/openclaw}"
if [ -n "${INSTALL_PLUGIN_ID:-}" ]; then
  INSTALL_DIR="${OPENCLAW_HOME_DIR}/extensions/${INSTALL_PLUGIN_ID}"
  if [ -d "$INSTALL_DIR" ]; then
    INSTALL_LOAD_PATHS=$(jq -n --arg p "$INSTALL_DIR" '[$p]')
    echo "[entrypoint] install-mode: plugins.load.paths += $INSTALL_DIR" >&2
  else
    echo "[entrypoint] WARN: INSTALL_PLUGIN_ID=$INSTALL_PLUGIN_ID set but $INSTALL_DIR not found" >&2
  fi
fi
# Stage 3 Phase 5: append each EXTRA_INSTALL_PLUGIN_IDS dir (space-separated)
# so paired plugins (e.g. context-engine + memory) both load.
if [ -n "${EXTRA_INSTALL_PLUGIN_IDS:-}" ]; then
  for extra_id in ${EXTRA_INSTALL_PLUGIN_IDS}; do
    EXTRA_DIR="${OPENCLAW_HOME_DIR}/extensions/${extra_id}"
    if [ -d "$EXTRA_DIR" ]; then
      INSTALL_LOAD_PATHS=$(echo "$INSTALL_LOAD_PATHS" | jq --arg p "$EXTRA_DIR" '. + [$p]')
      echo "[entrypoint] install-mode: plugins.load.paths += $EXTRA_DIR" >&2
    else
      echo "[entrypoint] WARN: extra plugin id '$extra_id' set but $EXTRA_DIR not found" >&2
    fi
  done
fi

jq \
  --argjson allow "$PLUGIN_ALLOW" \
  --arg slot "$MEMORY_SLOT" \
  --argjson entries "$PLUGIN_ENTRIES" \
  --argjson enabled "$MEMORY_SEARCH_ENABLED" \
  --argjson loadPaths "$INSTALL_LOAD_PATHS" \
  --arg ceSlot "$CONTEXT_ENGINE_PLUGIN" \
  --argjson flushEnabled "$MEMORY_FLUSH_ENABLED" \
  --arg flushSoft "$MEMORY_FLUSH_SOFT" \
  --arg flushReserve "$MEMORY_FLUSH_RESERVE" \
  --arg flushForceBytes "$MEMORY_FLUSH_FORCE_BYTES" \
  '
  .plugins.allow = $allow
  | .plugins.slots.memory = $slot
  | .plugins.entries = $entries
  | .agents.defaults.memorySearch.enabled = $enabled
  | .agents.defaults.compaction.memoryFlush.enabled = $flushEnabled
  | (if $flushSoft != ""
       then .agents.defaults.compaction.memoryFlush.softThresholdTokens = ($flushSoft | tonumber)
       else . end)
  | (if $flushReserve != ""
       then .agents.defaults.compaction.reserveTokensFloor = ($flushReserve | tonumber)
       else . end)
  | (if $flushForceBytes != ""
       then .agents.defaults.compaction.memoryFlush.forceFlushTranscriptBytes = $flushForceBytes
       else . end)
  | (if ($loadPaths | length) > 0 then .plugins.load.paths = $loadPaths else . end)
  | (if ($ceSlot | length) > 0 then .plugins.slots.contextEngine = $ceSlot else . end)
  ' "$TPL" \
  | sed -e "s|\${LLM_MODEL_ID}|$LLM_MODEL_ID|g" \
        -e "s|\${LLM_MODEL_NAME}|$LLM_MODEL_NAME|g" \
        -e "s|\${WORKSPACE_DIR}|$WS|g" \
        -e "s|\${OPENCLAW_STATE_DIR}|$WS/state|g" \
  > "$OUT"

# Validate it parses as JSON.
jq empty "$OUT" 2>&1 || { echo "ERROR: rendered $OUT is not valid JSON" >&2; cat "$OUT" >&2; exit 1; }

echo "[entrypoint] rendered $OUT (plugin=$PLUGIN mode=$MODE memorySearch.enabled=$MEMORY_SEARCH_ENABLED memoryFlush.enabled=$MEMORY_FLUSH_ENABLED soft=$MEMORY_FLUSH_SOFT reserve=$MEMORY_FLUSH_RESERVE force_bytes=$MEMORY_FLUSH_FORCE_BYTES)" >&2

# Optional plugin sidecar (e.g. mem0 FastAPI server). Started in
# background so the container's main CMD (sleep infinity) keeps running
# and the harness can docker exec openclaw against the same container.
SIDECAR_PID=""
if [ -x /sidecar/venv/bin/python ] && [ -f /sidecar/server.py ]; then
  SIDECAR_PORT="${MEM0_PORT:-8765}"
  SIDECAR_LOG="${WS}/sidecar.log"
  echo "[entrypoint] starting plugin sidecar on 127.0.0.1:${SIDECAR_PORT} (log=${SIDECAR_LOG})" >&2
  /sidecar/venv/bin/python -m uvicorn server:app \
    --app-dir /sidecar \
    --host 127.0.0.1 \
    --port "${SIDECAR_PORT}" \
    --log-level warning \
    > "${SIDECAR_LOG}" 2>&1 &
  SIDECAR_PID=$!
  echo "[entrypoint] sidecar pid=${SIDECAR_PID}" >&2
fi

cleanup() {
  if [ -n "$SIDECAR_PID" ] && kill -0 "$SIDECAR_PID" 2>/dev/null; then
    kill "$SIDECAR_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT TERM INT

# Run main command (sleep infinity by default) in background and wait so
# the trap above fires on docker stop / SIGTERM. Exec'ing would replace
# this script and detach the trap.
"$@" &
MAIN_PID=$!
wait "$MAIN_PID"
