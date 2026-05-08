#!/bin/bash
# Build + push the plugin-agnostic eval-base layer to a shared registry.
#
# Downstream evaluators pull this image and build local plugin layers on
# top of it; the plugin layer itself is NOT pushed. See
# docs/superpowers/runbooks/stage3-operator.md for the full rationale.
#
# Prereqs:
#   * docker login already done for the target registry
#   * OPENCLAW_REPO_PATH points at a clean OpenClaw checkout
#   * .env at repo root (LLM_API_KEY etc. — only needed for the smoke
#     verification at the end; build itself doesn't read these)
#
# Usage:
#   ./openclaw-eval/scripts/push_eval_base.sh [registry]
#
# Default registry: ghcr.io/duffycoder
#
# Examples:
#   ./openclaw-eval/scripts/push_eval_base.sh
#   ./openclaw-eval/scripts/push_eval_base.sh registry.cn-hangzhou.aliyuncs.com/duffycoder

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REGISTRY="${1:-ghcr.io/duffycoder}"

cd "$REPO_ROOT"

if [ ! -x .venv/bin/python ]; then
  echo "[push_eval_base] ERROR: .venv/bin/python not found — run 'uv sync' first" >&2
  exit 1
fi

echo "==============================================================="
echo "[push_eval_base] target registry: $REGISTRY"
echo "==============================================================="

.venv/bin/python openclaw-eval/harness/build.py \
  --build-eval-base \
  --push-eval-base \
  --registry "$REGISTRY" \
  --variant slim

echo
echo "[push_eval_base] verifying remote tag exists..."
LOCAL_TAG=$(docker images openclaw-eval-base --format '{{.Repository}}:{{.Tag}}' | head -1)
if [ -z "$LOCAL_TAG" ]; then
  echo "[push_eval_base] ERROR: no openclaw-eval-base image found locally after build" >&2
  exit 1
fi
REMOTE_TAG="${REGISTRY%/}/${LOCAL_TAG}"
docker manifest inspect "$REMOTE_TAG" > /dev/null 2>&1 \
  && echo "[push_eval_base] OK: $REMOTE_TAG visible on registry" \
  || echo "[push_eval_base] WARN: docker manifest inspect failed for $REMOTE_TAG (private registry without read access?)"

echo
echo "==============================================================="
echo "[push_eval_base] DONE"
echo "==============================================================="
echo "Downstream evaluators can now run:"
echo "  docker pull $REMOTE_TAG"
echo "  .venv/bin/python openclaw-eval/harness/build.py \\"
echo "    --eval-base-image $REMOTE_TAG \\"
echo "    --install-spec '@openclaw/openviking@<version>' \\"
echo "    --install-plugin-id openviking"
