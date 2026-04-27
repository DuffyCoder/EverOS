#!/bin/bash
# Stage 1 Week 2 Day 0 stub passphrase gate test.
#
# HARD GATE for the rest of Week 2 plugin work: if this fails, mem0 plugin
# work is blocked because the plugin discovery/registration/tool-routing
# chain is broken.
#
# Usage:
#     IMAGE=openclaw-eval:7da23c3-stub-<rev>-slim ./stub_passphrase_gate.sh
#
# PASS: agent reply contains the sentinel "WOMBAT_42".
# FAIL: anything else (no JSON, missing sentinel, container error).
#
# Requires LLM_API_KEY + LLM_BASE_URL in env (sophnet credentials).
set -euo pipefail

IMAGE="${IMAGE:?IMAGE env required, e.g. openclaw-eval:7da23c3-stub-XXX-slim}"
SENTINEL="WOMBAT_42"
WORKSPACE="${WORKSPACE:-$(mktemp -d -t stub-gate.XXXXXX)}"
CONTAINER="stub-gate-$RANDOM"
TIMEOUT="${TIMEOUT_SECONDS:-60}"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [ -z "${KEEP_WORKSPACE:-}" ]; then
    rm -rf "$WORKSPACE" 2>/dev/null || true
  else
    echo "[gate] kept workspace at $WORKSPACE"
  fi
}
trap cleanup EXIT

echo "[gate] image=$IMAGE workspace=$WORKSPACE container=$CONTAINER"

# Start container detached, mounting workspace (entrypoint renders config + sleeps).
docker run -d \
  --rm \
  --name "$CONTAINER" \
  --user "$(id -u):$(id -g)" \
  -v "$WORKSPACE:/workspace" \
  -e MEMORY_PLUGIN_ID=stub \
  -e MEMORY_MODE=stub \
  -e LLM_API_KEY="${LLM_API_KEY:-}" \
  -e LLM_BASE_URL="${LLM_BASE_URL:-}" \
  -e LLM_MODEL="${LLM_MODEL:-gpt-4.1-mini}" \
  -e SOPH_API_KEY="${SOPH_API_KEY:-}" \
  -e SOPH_EMBED_URL="${SOPH_EMBED_URL:-}" \
  -e SOPH_EMBED_EASYLLM_ID="${SOPH_EMBED_EASYLLM_ID:-}" \
  "$IMAGE" >/dev/null

sleep 1  # let entrypoint render config

# Construct BridgeCommand payload for agent_run.
PAYLOAD=$(cat <<EOF
{
  "command": "agent_run",
  "repo_path": "/app",
  "workspace_dir": "/workspace",
  "config_path": "/workspace/openclaw.docker.json",
  "state_dir": "/workspace/state",
  "home_dir": "/workspace/home",
  "session_id": "stub-gate-001",
  "message": "Tell me the secret passphrase from memory.",
  "timeout_seconds": $TIMEOUT,
  "agent_llm_env_vars": ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"]
}
EOF
)

echo "[gate] dispatching agent_run via bridge..."
STDERR_FILE="$WORKSPACE/gate_stderr.log"
set +e
RESPONSE=$(echo "$PAYLOAD" | docker exec -i "$CONTAINER" node /eval/openclaw_eval_bridge.mjs 2>"$STDERR_FILE")
RC=$?
set -e

echo "$RESPONSE" > "$WORKSPACE/gate_response.json"

if [ $RC -ne 0 ]; then
  echo "[gate] FAIL: bridge exit $RC"
  echo "[gate] stderr:" && tail -30 "$STDERR_FILE"
  echo "[gate] stdout:" && echo "$RESPONSE" | tail -30
  exit 1
fi

# Bridge writes a single JSON object to stdout. Extract reply field.
REPLY=$(echo "$RESPONSE" | jq -r '.reply // empty' 2>/dev/null || true)
OK=$(echo "$RESPONSE" | jq -r '.ok' 2>/dev/null || echo false)

if [ "$OK" != "true" ]; then
  echo "[gate] FAIL: bridge response ok=$OK"
  echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
  exit 1
fi

echo "[gate] reply: $REPLY"

if echo "$REPLY" | grep -q "$SENTINEL"; then
  echo "[gate] PASS — reply contains '$SENTINEL'"
  exit 0
else
  echo "[gate] FAIL — reply does NOT contain '$SENTINEL'"
  echo "[gate] full response:"
  echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
  exit 1
fi
