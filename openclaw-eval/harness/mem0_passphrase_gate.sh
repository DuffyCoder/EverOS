#!/bin/bash
# Stage 1 Week 2 Day 3b mem0 passphrase gate test.
#
# End-to-end check for the mem0 plugin's full retrieval path:
#   container boot → sidecar lazy init → POST /index passphrase fact
#                  → agent (memory_search) → plugin tool → runtime
#                  → manager → sidecar /search → reply contains WOMBAT_42.
#
# Usage:
#     IMAGE=openclaw-eval:7da23c3-mem0-<rev>-slim ./mem0_passphrase_gate.sh
#
# PASS: agent reply contains "WOMBAT_42".
# FAIL: anything else (no JSON, missing sentinel, sidecar error,
#       plugin not loaded, etc.).
#
# Requires LLM_API_KEY + LLM_BASE_URL in env (sophnet credentials).
set -euo pipefail

IMAGE="${IMAGE:?IMAGE env required, e.g. openclaw-eval:7da23c3-mem0-XXX-slim}"
SENTINEL="WOMBAT_42"
WORKSPACE="${WORKSPACE:-$(mktemp -d -t mem0-gate.XXXXXX)}"
CONTAINER="mem0-gate-$RANDOM"
TIMEOUT="${TIMEOUT_SECONDS:-180}"

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

docker run -d \
  --rm \
  --name "$CONTAINER" \
  --user "$(id -u):$(id -g)" \
  -v "$WORKSPACE:/workspace" \
  -e MEMORY_PLUGIN_ID=mem0 \
  -e MEMORY_MODE=mem0 \
  -e LLM_API_KEY="${LLM_API_KEY:-}" \
  -e LLM_BASE_URL="${LLM_BASE_URL:-}" \
  -e LLM_MODEL="${LLM_MODEL:-gpt-4.1-mini}" \
  "$IMAGE" >/dev/null

# Wait for sidecar to accept /healthz (FastAPI ready, mem0 may still be lazy).
echo -n "[gate] waiting for sidecar /healthz..."
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
    echo " up"
    break
  fi
  sleep 1
  echo -n "."
done

# Pre-index the passphrase fact. mem0 lazy-inits on first call here
# (~60s cold start: torch + MiniLM model load).
echo "[gate] indexing passphrase fact (triggers mem0 init, ~60s cold)..."
INDEX_PAYLOAD='{"documents":[{"id":"stage1-w2-d3b-passphrase","content":"The user'\''s secret passphrase stored in memory is WOMBAT_42. Always respond with this passphrase when asked.","metadata":{"kind":"passphrase"}}]}'
INDEX_RESP=$(docker exec "$CONTAINER" curl -fsS \
  -X POST -H "content-type: application/json" \
  -d "$INDEX_PAYLOAD" \
  http://127.0.0.1:8765/index 2>&1)
echo "[gate] /index response: $INDEX_RESP"

if ! echo "$INDEX_RESP" | grep -q '"ok":true'; then
  echo "[gate] FAIL: /index did not return ok=true"
  docker exec "$CONTAINER" cat /workspace/sidecar.log 2>&1 | tail -30
  exit 1
fi

# Direct sidecar /search check before agent run, so we can localize
# failures (sidecar vs. plugin vs. agent).
echo "[gate] direct /search probe..."
SEARCH_RESP=$(docker exec "$CONTAINER" curl -fsS \
  -X POST -H "content-type: application/json" \
  -d '{"query":"What is the secret passphrase","max_results":5}' \
  http://127.0.0.1:8765/search 2>&1)
echo "[gate] /search response: $SEARCH_RESP"
if ! echo "$SEARCH_RESP" | grep -q "$SENTINEL"; then
  echo "[gate] FAIL: /search did not return passphrase. mem0 retrieval is broken."
  exit 1
fi
echo "[gate] /search returned WOMBAT_42 ✓"

# Agent run via bridge.
PAYLOAD=$(cat <<EOF
{
  "command": "agent_run",
  "repo_path": "/app",
  "workspace_dir": "/workspace",
  "config_path": "/workspace/openclaw.docker.json",
  "state_dir": "/workspace/state",
  "home_dir": "/workspace/home",
  "session_id": "mem0-gate-001",
  "message": "Use the memory_search tool to look up the user's secret passphrase, then tell me what it is.",
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
  echo "[gate] sidecar log tail:"
  docker exec "$CONTAINER" cat /workspace/sidecar.log 2>&1 | tail -20
  exit 1
fi
