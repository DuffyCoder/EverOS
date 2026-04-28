#!/bin/bash
# Stage 1 Week 3 evermemos plugin passphrase gate.
#
# End-to-end check for the EverMemOS plugin's full retrieval path:
#   container boot → plugin loads → host EverMemOS API pre-indexed
#                  → agent (memory_search) → plugin tool → fetch
#                    host.docker.internal:1995 /api/v1/memories/search
#                  → reply contains WOMBAT_42.
#
# Usage:
#     IMAGE=openclaw-eval:7da23c3-evermemos-<rev>-slim ./evermemos_passphrase_gate.sh
#
# Prerequisites:
#   * Host EverMemOS API server running at localhost:1995
#     (start with `make run` or `uv run python src/run.py` from project root)
#   * Sophnet creds in env (LLM_API_KEY, LLM_BASE_URL)
#
# PASS: agent reply contains WOMBAT_42.
# FAIL: anything else.
set -euo pipefail

IMAGE="${IMAGE:?IMAGE env required, e.g. openclaw-eval:7da23c3-evermemos-XXX-slim}"
SENTINEL="WOMBAT_42"
HOST_API_URL="${HOST_API_URL:-http://localhost:1995}"
WORKSPACE="${WORKSPACE:-$(mktemp -d -t evermemos-gate.XXXXXX)}"
CONTAINER="evermemos-gate-$RANDOM"
TIMEOUT="${TIMEOUT_SECONDS:-180}"
GROUP_ID="evermemos-gate-001"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [ -z "${KEEP_WORKSPACE:-}" ]; then
    rm -rf "$WORKSPACE" 2>/dev/null || true
  else
    echo "[gate] kept workspace at $WORKSPACE"
  fi
}
trap cleanup EXIT

echo "[gate] image=$IMAGE workspace=$WORKSPACE container=$CONTAINER host_api=$HOST_API_URL"

# 1. Pre-flight: ensure host EverMemOS API is reachable.
HOST_HEALTH=$(curl -fsS -m 5 -o /dev/null -w "%{http_code}\n" "$HOST_API_URL/health" 2>/dev/null || echo 000)
if [ "$HOST_HEALTH" != "200" ]; then
  echo "[gate] FAIL: host EverMemOS API not reachable at $HOST_API_URL/health (got $HOST_HEALTH)"
  echo "[gate]   Start it via:  make run  (or: uv run python src/run.py)"
  exit 1
fi
echo "[gate] host EverMemOS /health ✓"

# 2. Pre-index a small conversation that mentions the passphrase. EverMemOS
#    uses async boundary-detection — single message stays in the queue.
#    Multiple messages with at least one boundary trigger flush. We send
#    5 messages to give the extractor enough context.
echo "[gate] indexing fact via host API (5 messages, last with sync_mode for flush)..."
USER_ID="alice_${GROUP_ID}"
TS_BASE=$(date +%s)
ingest_one() {
  local i="$1" content="$2" sync="$3"
  local ts=$(date -u -d @$((TS_BASE + i*60)) +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
  local query=""
  if [ "$sync" = "true" ]; then query="?sync_mode=true"; fi
  curl -fsS -m 60 -X POST -H "content-type: application/json" \
    -d "{
      \"group_id\":\"$GROUP_ID\",
      \"group_name\":\"$GROUP_ID\",
      \"message_id\":\"msg-$i\",
      \"create_time\":\"$ts\",
      \"sender\":\"$USER_ID\",
      \"sender_name\":\"alice\",
      \"role\":\"user\",
      \"content\":\"$content\"
    }" \
    "$HOST_API_URL/api/v1/memories$query" 2>&1
}

ingest_one 1 "Hi everyone, kicking off the new project tracking effort." false >/dev/null
ingest_one 2 "I want to settle on a codeword for this project so we can refer to it consistently." false >/dev/null
ingest_one 3 "My preferred project codeword is WOMBAT_42. Please use this codeword whenever summarizing or referencing this project." false >/dev/null
ingest_one 4 "The codeword WOMBAT_42 should be used in all our internal documents." false >/dev/null
INGEST_RESP=$(ingest_one 5 "Confirmed: tracking the project under WOMBAT_42 from now on." true)
echo "[gate] /api/v1/memories final response: ${INGEST_RESP:0:200}..."

# 3. Direct host-side search probe to confirm retrieval works.
#    Try keyword first; if no hit, try hybrid (vector fallback).
echo "[gate] direct host /search probe (keyword)..."
SEARCH_RESP=$(curl -fsS -m 30 -G "$HOST_API_URL/api/v1/memories/search" \
  --data-urlencode "query=preferred project codeword" \
  --data-urlencode "retrieve_method=keyword" \
  --data-urlencode "top_k=5" \
  --data-urlencode "group_id=$GROUP_ID" \
  --data-urlencode "user_id=" 2>&1)
echo "[gate] search keyword response: ${SEARCH_RESP:0:400}"
if ! echo "$SEARCH_RESP" | grep -q "$SENTINEL"; then
  echo "[gate] keyword miss; trying hybrid..."
  SEARCH_RESP=$(curl -fsS -m 30 -G "$HOST_API_URL/api/v1/memories/search" \
    --data-urlencode "query=preferred project codeword" \
    --data-urlencode "retrieve_method=hybrid" \
    --data-urlencode "top_k=5" \
    --data-urlencode "group_id=$GROUP_ID" \
    --data-urlencode "user_id=" 2>&1)
  echo "[gate] search hybrid response: ${SEARCH_RESP:0:400}"
fi
if ! echo "$SEARCH_RESP" | grep -q "$SENTINEL"; then
  echo "[gate] FAIL: host /search did not return passphrase. EverMemOS retrieval is broken."
  exit 1
fi
echo "[gate] host /search returned WOMBAT_42 ✓"

# 4. Spawn container.
docker run -d --rm \
  --name "$CONTAINER" \
  --user "$(id -u):$(id -g)" \
  --add-host=host.docker.internal:host-gateway \
  -v "$WORKSPACE:/workspace" \
  -e MEMORY_PLUGIN_ID=evermemos \
  -e MEMORY_MODE=evermemos \
  -e LLM_API_KEY="${LLM_API_KEY:-}" \
  -e LLM_BASE_URL="${LLM_BASE_URL:-}" \
  -e LLM_MODEL="${LLM_MODEL:-gpt-4.1-mini}" \
  -e EVERMEMOS_API_URL="http://host.docker.internal:1995" \
  -e EVERMEMOS_API_KEY="" \
  "$IMAGE" >/dev/null

sleep 2  # entrypoint config render

# 5. Probe reachability from container side.
echo "[gate] probing host.docker.internal from container..."
if docker exec "$CONTAINER" curl -fsS -m 5 "http://host.docker.internal:1995/health" >/dev/null 2>&1; then
  echo "[gate] container -> host API reachable ✓"
else
  echo "[gate] FAIL: container cannot reach host.docker.internal:1995"
  echo "[gate]   --add-host=host.docker.internal:host-gateway may not be wired correctly"
  exit 1
fi

# 6. Agent run via bridge.
PAYLOAD=$(cat <<EOF
{
  "command": "agent_run",
  "repo_path": "/app",
  "workspace_dir": "/workspace",
  "config_path": "/workspace/openclaw.docker.json",
  "state_dir": "/workspace/state",
  "home_dir": "/workspace/home",
  "session_id": "evermemos-gate-001",
  "message": "What is the user's preferred project codeword? It is stored in EverMemOS group $GROUP_ID.",
  "timeout_seconds": $TIMEOUT,
  "agent_llm_env_vars": ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "EVERMEMOS_API_URL", "EVERMEMOS_API_KEY"]
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
  echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
  exit 1
fi
