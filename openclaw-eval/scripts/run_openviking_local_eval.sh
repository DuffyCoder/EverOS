#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build.sh [--dry-run] [--yes-reset] RUN_NAME [evaluation arguments...]

Restart the local OpenViking server and run the LoCoMo evaluation.

Options:
  --dry-run    Print the resolved configuration without making changes.
  --yes-reset  Authorize stopping the server and resetting .ovdata.
  -h, --help   Show this help.

Environment overrides:
  OPENVIKING_FORK        OpenViking fork checkout.
  OPENVIKING_PLUGIN_DIR  OpenClaw plugin directory.
  OPENVIKING_CONFIG      OpenViking server configuration.
  OPENVIKING_SERVER_BIN  openviking-server executable.
  EVAL_PYTHON            Python executable for the evaluation CLI.
  EVAL_SYSTEM            Evaluation system id.
  EVAL_LOG_DIR           Directory for local runner logs.
  TCMALLOC_PATH          Optional allocator library to preload.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd -P)"
REPO_PARENT="${REPO_ROOT%/*}"

command -v realpath >/dev/null 2>&1 || die "required command not found: realpath"

canonical_path() {
  realpath -m -- "$1"
}

OPENVIKING_FORK="$(canonical_path "${OPENVIKING_FORK:-$REPO_PARENT/OpenViking-fork}")"
OPENVIKING_PLUGIN_DIR="$(canonical_path "${OPENVIKING_PLUGIN_DIR:-$OPENVIKING_FORK/examples/openclaw-plugin}")"
OPENVIKING_CONFIG="$(canonical_path "${OPENVIKING_CONFIG:-${HOME:-$REPO_ROOT}/.openviking/ov.local.conf}")"
OPENVIKING_SERVER_BIN="$(canonical_path "${OPENVIKING_SERVER_BIN:-$OPENVIKING_FORK/.venv/bin/openviking-server}")"
EVAL_PYTHON="$(canonical_path "${EVAL_PYTHON:-$REPO_ROOT/.venv/bin/python}")"
EVAL_SYSTEM="${EVAL_SYSTEM:-openclaw-docker-openviking-session-bundle-noop}"
EVAL_LOG_DIR="$(canonical_path "${EVAL_LOG_DIR:-$REPO_ROOT/.runlogs}")"
if [[ -n "${TCMALLOC_PATH:-}" ]]; then
  TCMALLOC_PATH="$(canonical_path "$TCMALLOC_PATH")"
else
  TCMALLOC_PATH=""
fi
RESET_TARGET="${OPENVIKING_FORK%/}/.ovdata"

DRY_RUN=0
YES_RESET=0
RUN_NAME=""
EVALUATION_ARGS=()

while (( $# > 0 )); do
  case "$1" in
    -h|--help)
      if [[ -z "$RUN_NAME" ]]; then
        usage
        exit 0
      fi
      EVALUATION_ARGS+=("$1")
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --yes-reset)
      YES_RESET=1
      ;;
    --)
      shift
      if [[ -z "$RUN_NAME" ]]; then
        (( $# > 0 )) || die "RUN_NAME is required"
        RUN_NAME="$1"
        shift
      fi
      EVALUATION_ARGS+=("$@")
      break
      ;;
    -*)
      if [[ -z "$RUN_NAME" ]]; then
        die "unknown runner option before RUN_NAME: $1"
      fi
      EVALUATION_ARGS+=("$1")
      ;;
    *)
      if [[ -z "$RUN_NAME" ]]; then
        RUN_NAME="$1"
      else
        EVALUATION_ARGS+=("$1")
      fi
      ;;
  esac
  shift
done

[[ -n "$RUN_NAME" ]] || {
  usage >&2
  die "RUN_NAME is required"
}

print_resolved_configuration() {
  printf '%s\n' \
    "repository: $REPO_ROOT" \
    "fork: $OPENVIKING_FORK" \
    "plugin: $OPENVIKING_PLUGIN_DIR" \
    "config: $OPENVIKING_CONFIG" \
    "server: $OPENVIKING_SERVER_BIN" \
    "log: $EVAL_LOG_DIR/ov-server.log" \
    "log-dir: $EVAL_LOG_DIR" \
    "python: $EVAL_PYTHON" \
    "system: $EVAL_SYSTEM" \
    "tcmalloc: ${TCMALLOC_PATH:-<disabled>}" \
    "reset-target: $RESET_TARGET" \
    "run-name: $RUN_NAME"
  printf 'evaluation-args:'
  if (( ${#EVALUATION_ARGS[@]} > 0 )); then
    printf ' %q' "${EVALUATION_ARGS[@]}"
  fi
  printf '\n'
}

if (( DRY_RUN )); then
  print_resolved_configuration
  exit 0
fi

if (( ! YES_RESET )); then
  if [[ ! -t 0 ]]; then
    die "non-interactive runs require --yes-reset before destructive work"
  fi

  printf '%s\n' \
    "This stops the OpenViking server on port 1933 and resets:" \
    "  $RESET_TARGET"
  read -r -p "Type exactly 'yes' to continue: " confirmation
  [[ "$confirmation" == "yes" ]] || die "reset not confirmed; pass --yes-reset or type exactly 'yes'"
fi

require_directory() {
  [[ -d "$1" ]] || die "required directory does not exist: $1"
}

require_file() {
  [[ -f "$1" ]] || die "required file does not exist: $1"
}

require_executable() {
  [[ -x "$1" && ! -d "$1" ]] || die "required executable does not exist or is not executable: $1"
}

[[ "$OPENVIKING_FORK" != "/" ]] || die "refusing unsafe OpenViking fork root: /"
require_directory "$OPENVIKING_FORK"
if [[ ! -f "$OPENVIKING_FORK/pyproject.toml" || ! -d "$OPENVIKING_FORK/openviking" ]]; then
  die "OpenViking fork is missing checkout markers (pyproject.toml and openviking/): $OPENVIKING_FORK"
fi
[[ ! -L "$RESET_TARGET" ]] || die "refusing symlink reset target: $RESET_TARGET"

reject_inside_reset_target() {
  local variable_name="$1"
  local candidate="$2"
  if [[ "$candidate" == "$RESET_TARGET" || "$candidate" == "$RESET_TARGET/"* ]]; then
    die "$variable_name is inside reset target $RESET_TARGET: $candidate"
  fi
}

reject_inside_reset_target OPENVIKING_PLUGIN_DIR "$OPENVIKING_PLUGIN_DIR"
reject_inside_reset_target OPENVIKING_CONFIG "$OPENVIKING_CONFIG"
reject_inside_reset_target OPENVIKING_SERVER_BIN "$OPENVIKING_SERVER_BIN"
reject_inside_reset_target EVAL_PYTHON "$EVAL_PYTHON"
reject_inside_reset_target EVAL_LOG_DIR "$EVAL_LOG_DIR"
if [[ -n "$TCMALLOC_PATH" ]]; then
  reject_inside_reset_target TCMALLOC_PATH "$TCMALLOC_PATH"
fi

require_directory "$OPENVIKING_PLUGIN_DIR"
require_file "$OPENVIKING_CONFIG"
require_executable "$OPENVIKING_SERVER_BIN"
require_executable "$EVAL_PYTHON"
if [[ -n "$TCMALLOC_PATH" ]]; then
  require_file "$TCMALLOC_PATH"
fi

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

for required_command in ss rm mkdir curl setsid nohup env sleep tail kill; do
  require_command "$required_command"
done
if [[ ! -d "$OPENVIKING_PLUGIN_DIR/node_modules" ]]; then
  require_command npm
fi

OPENVIKING_LOG="$EVAL_LOG_DIR/ov-server.log"
printf '%s\n' '[build] restarting source OpenViking server (port 1933)...'
inspect_openviking_listener() {
  local listener_output
  local listener_line
  local listener_remainder
  local listener_pid
  local line_pid_count
  local -a listener_fields

  if ! listener_output="$(ss -H -ltnp 'sport = :1933')"; then
    die "ss failed while inspecting the listener on port 1933"
  fi

  LISTENER_PID=""
  while IFS= read -r listener_line; do
    [[ -n "${listener_line//[[:space:]]/}" ]] || continue
    read -r -a listener_fields <<<"$listener_line"
    if (( ${#listener_fields[@]} < 5 )) \
      || [[ "${listener_fields[0]}" != "LISTEN" ]] \
      || [[ "${listener_fields[3]}" != *":1933" ]]; then
      die "unexpected listener endpoint returned by ss for port 1933"
    fi

    line_pid_count=0
    listener_remainder="$listener_line"
    while [[ "$listener_remainder" == *"pid="* ]]; do
      listener_remainder="${listener_remainder#*pid=}"
      if [[ ! "$listener_remainder" =~ ^([1-9][0-9]*)([^0-9]|$) ]]; then
        die "unsafe listener PID returned by ss for port 1933"
      fi
      listener_pid="${BASH_REMATCH[1]}"
      [[ "$listener_pid" != "1" ]] || die "unsafe listener PID returned by ss for port 1933"
      ((line_pid_count += 1))
      if [[ -n "$LISTENER_PID" && "$LISTENER_PID" != "$listener_pid" ]]; then
        die "multiple listener PIDs returned by ss for port 1933"
      fi
      LISTENER_PID="$listener_pid"
      listener_remainder="${listener_remainder#"$listener_pid"}"
    done
    (( line_pid_count > 0 )) || die "listener on port 1933 has no safe PID"
  done <<<"$listener_output"
}

inspect_openviking_listener
OPENVIKING_PID="$LISTENER_PID"

fork_commit="$(git -C "$OPENVIKING_FORK" rev-parse --short HEAD 2>/dev/null || printf unknown)"
fork_branch="$(git -C "$OPENVIKING_FORK" branch --show-current 2>/dev/null || true)"
printf '[build] fork @ %s (%s)\n' "$fork_commit" "${fork_branch:-detached}"
printf '[build] run-name: %s\n' "$RUN_NAME"

if [[ ! -d "$OPENVIKING_PLUGIN_DIR/node_modules" ]]; then
  printf '%s\n' '[build] first run: installing plugin runtime dependencies...'
  (
    cd -- "$OPENVIKING_PLUGIN_DIR"
    npm install --omit=dev --omit=optional --no-audit --no-fund
  )
fi

inspect_openviking_listener
OPENVIKING_PID="$LISTENER_PID"

if [[ -n "$OPENVIKING_PID" ]]; then
  if ! kill "$OPENVIKING_PID" 2>/dev/null; then
    die "failed to stop OpenViking listener PID $OPENVIKING_PID"
  fi

  listener_released=0
  for _ in {1..20}; do
    inspect_openviking_listener
    if [[ -z "$LISTENER_PID" ]]; then
      listener_released=1
      break
    fi
    if [[ "$LISTENER_PID" != "$OPENVIKING_PID" ]]; then
      die "port 1933 changed from PID $OPENVIKING_PID to PID $LISTENER_PID while stopping"
    fi
    sleep 0.25
  done
  (( listener_released )) || die "listener did not release port 1933 after stopping PID $OPENVIKING_PID"
fi

rm -rf -- "$RESET_TARGET"
mkdir -p -- "$RESET_TARGET"
mkdir -p -- "$EVAL_LOG_DIR"

server_environment=(
  env
  -u HTTP_PROXY
  -u HTTPS_PROXY
  -u http_proxy
  -u https_proxy
  -u ALL_PROXY
  -u all_proxy
)
if [[ -n "$TCMALLOC_PATH" ]]; then
  server_environment+=("LD_PRELOAD=$TCMALLOC_PATH")
fi

(
  cd -- "$OPENVIKING_FORK"
  exec setsid nohup "${server_environment[@]}" \
    "$OPENVIKING_SERVER_BIN" \
    --host 0.0.0.0 \
    --port 1933 \
    --config "$OPENVIKING_CONFIG" \
    >"$OPENVIKING_LOG" 2>&1 </dev/null
) &
SERVER_LAUNCH_PID=$!

server_healthy=0
for _ in {1..40}; do
  if ! kill -0 "$SERVER_LAUNCH_PID" 2>/dev/null; then
    die "server launch process exited before health was confirmed (PID $SERVER_LAUNCH_PID)"
  fi
  if health_response="$(curl -fsS -m 3 http://127.0.0.1:1933/health 2>/dev/null)"; then
    inspect_openviking_listener
    if [[ -n "$LISTENER_PID" ]]; then
      if [[ "$LISTENER_PID" != "$SERVER_LAUNCH_PID" ]]; then
        die "healthy port 1933 listener PID $LISTENER_PID does not match new server PID $SERVER_LAUNCH_PID"
      fi
      server_healthy=1
      printf '[build] OpenViking server healthy: %s\n' "$health_response"
      break
    fi
  fi
  sleep 2
done
if (( ! server_healthy )); then
  printf '[build] ERROR: OpenViking server health and new listener was not confirmed; see %s\n' \
    "$OPENVIKING_LOG" >&2
  tail -30 -- "$OPENVIKING_LOG" >&2 || true
  exit 1
fi

export OPENCLAW_EXTRA_MOUNTS="$OPENVIKING_PLUGIN_DIR:/app/extensions/openviking:ro"
printf '[build] plugin mount: %s\n' "$OPENCLAW_EXTRA_MOUNTS"

cd -- "$REPO_ROOT"
printf '[build] starting evaluation: system=%s run-name=%s\n' "$EVAL_SYSTEM" "$RUN_NAME"
exec "$EVAL_PYTHON" -m evaluation.cli \
  --dataset locomo \
  --system "$EVAL_SYSTEM" \
  --run-name "$RUN_NAME" \
  "${EVALUATION_ARGS[@]}"
