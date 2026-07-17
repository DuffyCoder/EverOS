#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
exec "$SCRIPT_DIR/openclaw-eval/scripts/run_openviking_local_eval.sh" "$@"
