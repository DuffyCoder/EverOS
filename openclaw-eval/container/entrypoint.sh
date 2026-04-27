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

# memorySearch.enabled boolean as JSON literal (not a string).
if [ "$MODE" = "noop" ]; then
  MEMORY_SEARCH_ENABLED='false'
else
  MEMORY_SEARCH_ENABLED='true'
fi

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
jq \
  --argjson allow "$PLUGIN_ALLOW" \
  --arg slot "$MEMORY_SLOT" \
  --argjson entries "$PLUGIN_ENTRIES" \
  --argjson enabled "$MEMORY_SEARCH_ENABLED" \
  '
  .plugins.allow = $allow
  | .plugins.slots.memory = $slot
  | .plugins.entries = $entries
  | .agents.defaults.memorySearch.enabled = $enabled
  ' "$TPL" \
  | sed -e "s|\${LLM_MODEL_ID}|$LLM_MODEL_ID|g" \
        -e "s|\${LLM_MODEL_NAME}|$LLM_MODEL_NAME|g" \
        -e "s|\${WORKSPACE_DIR}|$WS|g" \
        -e "s|\${OPENCLAW_STATE_DIR}|$WS/state|g" \
  > "$OUT"

# Validate it parses as JSON.
jq empty "$OUT" 2>&1 || { echo "ERROR: rendered $OUT is not valid JSON" >&2; cat "$OUT" >&2; exit 1; }

echo "[entrypoint] rendered $OUT (plugin=$PLUGIN mode=$MODE memorySearch.enabled=$MEMORY_SEARCH_ENABLED)" >&2

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
