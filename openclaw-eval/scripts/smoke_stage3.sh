#!/bin/bash
# Stage 3 smoke gate — runs both context-engine plugins end-to-end against
# real openclaw + sophnet. Exits 0 if all gates pass.
#
# Prereqs:
#   - LLM_API_KEY + LLM_BASE_URL (sophnet) in .env or shell
#   - SOPH_API_KEY + SOPH_EMBED_URL + SOPH_EMBED_EASYLLM_ID for embedding
#   - Both images pre-built:
#       openclaw-eval:7da23c3-stub-engine-005ac5c-slim
#       openclaw-eval:7da23c3-install-hypercompositor-5bace1f-slim
#
# Build them with:
#   .venv/bin/python openclaw-eval/harness/build.py --memory-plugin stub-engine
#   .venv/bin/python openclaw-eval/harness/build.py \
#     --memory-plugin memory-core \
#     --install-spec '@psiclawops/hypercompositor@0.9.6' \
#     --install-plugin-id hypercompositor
#
# Usage:
#   ./openclaw-eval/scripts/smoke_stage3.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${LLM_API_KEY:?LLM_API_KEY required (set in .env or shell)}"
: "${LLM_BASE_URL:?LLM_BASE_URL required}"

# ---------------- gate 1: stub-engine ----------------
STUB_IMAGE="${STUB_ENGINE_IMAGE:-openclaw-eval:7da23c3-stub-engine-005ac5c-slim}"
echo "==============================================================="
echo "[smoke] Phase 4 stub-engine gate (image=$STUB_IMAGE)"
echo "==============================================================="
if ! docker image inspect "$STUB_IMAGE" >/dev/null 2>&1; then
  echo "[smoke] FAIL: stub-engine image $STUB_IMAGE not found. Build with:"
  echo "    .venv/bin/python openclaw-eval/harness/build.py --memory-plugin stub-engine"
  exit 1
fi
IMAGE="$STUB_IMAGE" "$REPO_ROOT/openclaw-eval/harness/stub_engine_passphrase_gate.sh"

# ---------------- gate 2: hypercompositor real plugin ----------------
HYPER_IMAGE="${HYPER_IMAGE:-openclaw-eval:7da23c3-install-hypercompositor-5bace1f-slim}"
echo
echo "==============================================================="
echo "[smoke] Phase 5 hypercompositor gate (image=$HYPER_IMAGE)"
echo "==============================================================="
if ! docker image inspect "$HYPER_IMAGE" >/dev/null 2>&1; then
  echo "[smoke] FAIL: hypercompositor image $HYPER_IMAGE not found."
  echo "  Build with: .venv/bin/python openclaw-eval/harness/build.py \\"
  echo "    --memory-plugin memory-core \\"
  echo "    --install-spec '@psiclawops/hypercompositor@0.9.6' \\"
  echo "    --install-plugin-id hypercompositor"
  exit 1
fi

WS=$(mktemp -d -t hyper-smoke.XXXXXX)
CONTAINER="hyper-smoke-$RANDOM"
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; rm -rf "$WS"' EXIT

docker run -d --rm --name "$CONTAINER" --user "$(id -u):$(id -g)" \
  -v "$WS:/workspace" \
  -e MEMORY_PLUGIN_ID=memory-core \
  -e CONTEXT_ENGINE_PLUGIN_ID=hypercompositor \
  -e LLM_API_KEY -e LLM_BASE_URL \
  -e LLM_MODEL="${LLM_MODEL:-gpt-4.1-mini}" \
  -e SOPH_API_KEY -e SOPH_EMBED_URL -e SOPH_EMBED_EASYLLM_ID \
  "$HYPER_IMAGE" >/dev/null
sleep 3

# Two-turn ingest+recall: T1 plant a fact, T2 ask back. Pass criterion =
# T2 reply mentions the planted answer.
P1='{"command":"agent_run","repo_path":"/app","workspace_dir":"/workspace","config_path":"/workspace/openclaw.docker.json","state_dir":"/workspace/state","home_dir":"/workspace/home","session_id":"smoke","message":"My favorite color is teal. Acknowledge briefly.","timeout_seconds":40,"agent_llm_env_vars":["LLM_API_KEY","LLM_BASE_URL","LLM_MODEL","SOPH_API_KEY","SOPH_EMBED_URL","SOPH_EMBED_EASYLLM_ID"]}'
P2='{"command":"agent_run","repo_path":"/app","workspace_dir":"/workspace","config_path":"/workspace/openclaw.docker.json","state_dir":"/workspace/state","home_dir":"/workspace/home","session_id":"smoke","message":"What is my favorite color?","timeout_seconds":40,"agent_llm_env_vars":["LLM_API_KEY","LLM_BASE_URL","LLM_MODEL","SOPH_API_KEY","SOPH_EMBED_URL","SOPH_EMBED_EASYLLM_ID"]}'

T1_RESP=$(echo "$P1" | timeout 90 docker exec -i "$CONTAINER" node /eval/openclaw_eval_bridge.mjs 2>/dev/null || true)
T2_RESP=$(echo "$P2" | timeout 90 docker exec -i "$CONTAINER" node /eval/openclaw_eval_bridge.mjs 2>/dev/null || true)

T1_OK=$(echo "$T1_RESP" | jq -r '.ok' 2>/dev/null || echo false)
T2_OK=$(echo "$T2_RESP" | jq -r '.ok' 2>/dev/null || echo false)
T2_REPLY=$(echo "$T2_RESP" | jq -r '.reply' 2>/dev/null || echo "")

echo "[smoke] T1 ok=$T1_OK"
echo "[smoke] T2 ok=$T2_OK reply: $T2_REPLY"

if [ "$T1_OK" != "true" ] || [ "$T2_OK" != "true" ]; then
  echo "[smoke] FAIL: hypercompositor agent_run did not return ok=true"
  exit 1
fi
if ! echo "$T2_REPLY" | grep -qi "teal"; then
  echo "[smoke] FAIL: T2 reply does not contain 'teal' — engine recall broken"
  exit 1
fi

echo "[smoke] PASS — hypercompositor T2 recalled T1's planted fact"
echo
echo "==============================================================="
echo "[smoke] Stage 3 smoke gate: ALL GREEN"
echo "==============================================================="
