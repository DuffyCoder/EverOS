#!/usr/bin/env bash
# round_finish.sh — manually finalize a LoCoMo evaluation round.
#
# Two optional actions, run independently or together:
#   --archive NAME   Snapshot the current OpenViking server data + the
#                    matching evaluation/results/locomo-*-NAME directory
#                    into evaluation/archives/NAME-<timestamp>/.
#   --reset          Clean OpenViking namespace (viking + vectordb) and
#                    remove all conv containers so the next round starts
#                    from a clean slate.
#
# Typical pattern after one round:
#   bash evaluation/scripts/round_finish.sh --archive my-round --reset
#
# Why this exists: OpenViking data lives in the openviking container's
# writable layer (/app/data, ~65MB), the host mount /Data/.../.openviking
# only holds config. Plus conv containers are kept (remove_container_on_stop:
# false). Without this script every round inherits the previous round's
# fake-2026 events and dangling vectordb entries.

set -euo pipefail

OV_CONTAINER="${OV_CONTAINER:-openviking}"
OV_HEALTH_URL="${OV_HEALTH_URL:-http://127.0.0.1:1933/health}"
CONV_IMAGE_PREFIX="${CONV_IMAGE_PREFIX:-ghcr.io/duffycoder/openclaw-eval-plugins}"

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCHIVE_ROOT="$REPO_ROOT/evaluation/archives"
RESULTS_ROOT="$REPO_ROOT/evaluation/results"

usage() {
  cat <<'EOF'
Usage:
  bash evaluation/scripts/round_finish.sh [--archive [NAME]] [--reset]

Options:
  --archive [NAME]  Snapshot OV /app/data + matching results/ dir into
                    evaluation/archives/.
                    If NAME is omitted, auto-detect the most recently
                    modified evaluation/results/locomo-*/ dir and use its
                    basename (with leading 'locomo-' stripped) as NAME.
                    If NAME is given, glob locomo-*-NAME to find the
                    results dir (NAME usually = cli --run-name).
  --reset           Wipe OV namespace + vectordb, remove all conv containers.

At least one of --archive / --reset is required. They can be combined; when
both are given, archive runs first, then reset.

Environment:
  OV_CONTAINER       openviking container name (default: openviking)
  OV_HEALTH_URL      OV /health URL (default: http://127.0.0.1:1933/health)
  CONV_IMAGE_PREFIX  conv container image prefix used to select containers to
                     remove (default: ghcr.io/duffycoder/openclaw-eval-plugins)

Examples:
  bash evaluation/scripts/round_finish.sh --archive --reset
      # auto-pick latest results dir, archive it, then reset
  bash evaluation/scripts/round_finish.sh --archive fix3-fix4-full-locomo10
      # archive results matching locomo-*-fix3-fix4-full-locomo10
  bash evaluation/scripts/round_finish.sh --reset
      # reset only, no archive
EOF
}

# ARCHIVE_MODE: "" = not requested, "auto" = auto-detect latest results,
# anything else = literal NAME to glob.
ARCHIVE_MODE=""
DO_RESET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --archive)
      # NAME is optional: if next arg is missing or looks like a flag, auto-detect.
      if [ -z "${2:-}" ] || [[ "$2" == -* ]]; then
        ARCHIVE_MODE="auto"
        shift 1
      else
        ARCHIVE_MODE="$2"
        shift 2
      fi
      ;;
    --reset)
      DO_RESET=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$ARCHIVE_MODE" ] && [ $DO_RESET -eq 0 ]; then
  echo "ERROR: must pass at least one of --archive / --reset" >&2
  usage >&2
  exit 2
fi

log() { echo "[round_finish $(date '+%F %T')] $*"; }

ensure_openviking_running() {
  if ! docker inspect "$OV_CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: container '$OV_CONTAINER' not found" >&2
    exit 1
  fi
}

wait_ov_ready() {
  local timeout=60
  log "waiting for OV /health (timeout ${timeout}s)..."
  local start=$SECONDS
  until curl -sf "$OV_HEALTH_URL" >/dev/null 2>&1; do
    if [ $((SECONDS - start)) -ge $timeout ]; then
      echo "ERROR: OV did not become ready within ${timeout}s" >&2
      exit 1
    fi
    sleep 1
  done
  log "OV ready"
}

do_archive() {
  local name
  local results_dirs=""

  if [ "$ARCHIVE_MODE" = "auto" ]; then
    local latest
    latest="$(ls -dt "$RESULTS_ROOT"/locomo-*/ 2>/dev/null | head -1)"
    if [ -z "$latest" ]; then
      echo "ERROR: --archive without NAME, but no evaluation/results/locomo-*/ dir found" >&2
      exit 1
    fi
    latest="${latest%/}"
    local base
    base="$(basename "$latest")"
    name="${base#locomo-}"
    results_dirs="$latest"
    log "auto-detect: latest results dir = $base"
    log "auto-detect: archive name = $name"
  else
    name="$ARCHIVE_MODE"
    shopt -s nullglob
    local match=("$RESULTS_ROOT"/locomo-*-"$name")
    shopt -u nullglob
    for rd in "${match[@]}"; do
      results_dirs+="${rd%/}"$'\n'
    done
  fi

  local ts
  ts="$(date '+%Y%m%d-%H%M%S')"
  local archive_dir="$ARCHIVE_ROOT/${name}-${ts}"

  log "=== archive → $archive_dir ==="
  mkdir -p "$archive_dir"

  # Clean up partial archive on any failure (including SIGINT).
  trap 'rm -rf "$archive_dir"' ERR INT

  local git_sha
  git_sha="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

  log "stopping $OV_CONTAINER (for consistent leveldb snapshot)..."
  docker stop "$OV_CONTAINER" >/dev/null

  # Stream tar straight from docker cp to gzip — no temp dir, no double I/O.
  # `/app/data/.` makes docker cp emit content (paths like viking/...) instead
  # of wrapping in a `data/` directory.
  log "tar.gz ← docker cp ${OV_CONTAINER}:/app/data/."
  docker cp "$OV_CONTAINER:/app/data/." - | gzip > "$archive_dir/ov_data.tar.gz"

  log "starting $OV_CONTAINER back up..."
  docker start "$OV_CONTAINER" >/dev/null
  wait_ov_ready

  local ov_size
  ov_size="$(du -sh "$archive_dir/ov_data.tar.gz" | cut -f1)"

  # Copy each resolved results dir with -L (resolve symlinks/bind-mounts
  # into self-contained copies).
  local results_size="(none)"
  if [ -n "$results_dirs" ]; then
    mkdir -p "$archive_dir/results"
    while IFS= read -r rd; do
      [ -z "$rd" ] && continue
      log "cp -rL $(basename "$rd") → results/"
      cp -rL "$rd" "$archive_dir/results/"
    done <<< "$results_dirs"
    results_size="$(du -sh "$archive_dir/results" | cut -f1)"
  else
    log "no matching evaluation/results/locomo-*-${name} (skipping results copy)"
  fi

  cat >"$archive_dir/manifest.txt" <<EOF
round_name: ${name}
archived_at: $(date -Iseconds)
git_sha:    ${git_sha}
ov_data:    ${ov_size}
results:    ${results_size}
host:       $(hostname)
EOF

  # Archive succeeded — disarm the cleanup trap.
  trap - ERR INT

  log "archive complete: $archive_dir"
  log "  ov_data.tar.gz = ${ov_size}"
  log "  results        = ${results_size}"
}

do_reset() {
  log "=== reset ==="

  # `rm -rf -- /path/*` matches nothing harmlessly when /path is empty
  # (the unexpanded glob would fail under -e, but rm with no args also
  # errors; using `find -delete` sidesteps both).
  log "wiping OV namespace + vectordb inside $OV_CONTAINER"
  docker exec "$OV_CONTAINER" sh -c '
    find /app/data/viking/default/user -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    find /app/data/vectordb -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  '

  log "restarting $OV_CONTAINER..."
  docker restart "$OV_CONTAINER" >/dev/null
  wait_ov_ready

  log "removing conv containers (image prefix: $CONV_IMAGE_PREFIX)..."
  local conv_ids
  conv_ids="$(docker ps -a --format '{{.ID}} {{.Image}}' \
              | awk -v p="$CONV_IMAGE_PREFIX" '$2 ~ p {print $1}')"
  if [ -n "$conv_ids" ]; then
    echo "$conv_ids" | xargs -r docker rm -f >/dev/null
    local n
    n="$(wc -l <<< "$conv_ids" | tr -d ' ')"
    log "removed $n conv container(s)"
  else
    log "no conv containers to remove"
  fi

  local user_count conv_count
  user_count="$(docker exec "$OV_CONTAINER" sh -c \
    'ls /app/data/viking/default/user 2>/dev/null | wc -l' | tr -d ' ')"
  conv_count="$(docker ps -a --format '{{.Image}}' \
                | grep -c "$CONV_IMAGE_PREFIX" || true)"
  log "post-reset: OV users=${user_count}  conv containers=${conv_count}"
}

ensure_openviking_running

if [ -n "$ARCHIVE_MODE" ]; then
  do_archive
fi

if [ $DO_RESET -eq 1 ]; then
  do_reset
fi

log "done"
