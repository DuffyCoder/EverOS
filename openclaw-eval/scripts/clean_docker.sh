#!/bin/bash
# Stage 3 operator script — safely reclaim docker disk after long eval runs.
#
# Today's bring-up hit ENOSPC twice (build cache + duplicate eval images).
# This script gets the system back to a usable state without nuking the
# eval images you actually want to keep.
#
# Behavior (default — safe, idempotent):
#   1. Stop + rm any running eval containers (those tagged with our naming).
#   2. Drop dangling (untagged) images.
#   3. Drop builder cache older than 24h.
#   4. Report disk reclaimed.
#
# With --aggressive: also drops every openclaw-eval:* image except the most
# recent stub-engine + hypercompositor + memclaw + memory-core ones.
# Use after big rebuild loops to cap image count.
#
# With --nuke: docker system prune -af --volumes (your funeral). Includes
# pulled base layers; subsequent builds redownload everything.
#
# Usage:
#   ./openclaw-eval/scripts/clean_docker.sh
#   ./openclaw-eval/scripts/clean_docker.sh --aggressive
#   ./openclaw-eval/scripts/clean_docker.sh --nuke

set -euo pipefail

MODE="${1:-default}"

before_bytes() {
  docker system df --format '{{.Size}}' 2>/dev/null | head -1 || echo "?"
}

echo "[clean] disk before: $(df -h / | awk 'NR==2 {print $4}') free"

# Step 1 — kill running eval containers (be conservative — match by image name).
echo "[clean] step 1: stopping running eval containers..."
running=$(docker ps --filter "ancestor=openclaw-eval:*" --format '{{.ID}}' 2>/dev/null || true)
# also match by name prefix from our smoke gates
running="$running $(docker ps --format '{{.ID}} {{.Names}}' \
  | grep -E 'stub-engine-gate|hyper-(probe|run|mt|mt2|mt3|r[0-9]+)|memclaw-(probe|run)' \
  | awk '{print $1}' || true)"
running=$(echo "$running" | tr ' ' '\n' | sort -u | grep -v '^$' || true)
if [ -n "$running" ]; then
  echo "$running" | xargs -r docker rm -f 2>&1 | head -10
else
  echo "[clean]   (none running)"
fi

# Step 2 — dangling images.
echo "[clean] step 2: dropping dangling images..."
docker image prune -f 2>&1 | tail -3

# Step 3 — build cache older than 24h.
echo "[clean] step 3: dropping build cache > 24h..."
docker builder prune -f --filter 'until=24h' 2>&1 | tail -3

case "$MODE" in
  --aggressive)
    echo "[clean] step 4 (aggressive): dropping older eval images..."
    # Keep most recent of each plugin family.
    keep=()
    for prefix in stub-engine memory-core mem0 evermemos install-hypercompositor install-memclaw-context-engine; do
      newest=$(docker image ls "openclaw-eval:*" --format '{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}' \
        | grep "$prefix" | sort -k2 -r | head -1 | cut -f1 || true)
      [ -n "$newest" ] && keep+=("$newest")
    done
    if [ "${#keep[@]}" -gt 0 ]; then
      printf '[clean]   keep: %s\n' "${keep[@]}"
    fi
    docker image ls "openclaw-eval:*" --format '{{.Repository}}:{{.Tag}}' | while read -r tag; do
      keep_match=0
      for k in "${keep[@]:-}"; do
        [ "$tag" = "$k" ] && keep_match=1 && break
      done
      [ "$keep_match" -eq 0 ] && docker rmi "$tag" 2>&1 | head -1
    done
    ;;
  --nuke)
    echo "[clean] step 4 (NUKE): docker system prune -af --volumes..."
    docker system prune -af --volumes 2>&1 | tail -5
    ;;
  default)
    : # nothing extra
    ;;
  *)
    echo "[clean] WARN: unknown mode '$MODE' — running default cleanup only"
    ;;
esac

echo "[clean] disk after:  $(df -h / | awk 'NR==2 {print $4}') free"
docker image ls openclaw-eval --format '{{.Repository}}:{{.Tag}}' | sort
