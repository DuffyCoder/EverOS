#!/usr/bin/env bash
# Claude Code Routine setup script — auto-bench routine.
#
# Runs ONCE per routine session on the cloud container (output cached ~7 days).
# Keep cheap and idempotent.
set -euo pipefail

# Locate project root.
# When pasted into the cloud-environment "Setup script" field, this script runs
# BEFORE Claude Code launches and BEFORE the repo is cloned, so CLAUDE_PROJECT_DIR
# may not be set yet. In that case pyproject.toml won't exist and we exit cleanly —
# the full setup will run again once CLAUDE_PROJECT_DIR is available (routine start).
cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"
if [[ ! -f pyproject.toml ]]; then
  echo "ℹ️  pyproject.toml not found (CLAUDE_PROJECT_DIR=${CLAUDE_PROJECT_DIR:-unset})."
  echo "   Project-level setup deferred to routine runtime when repo is available."
  exit 0
fi

echo "::group::Git remotes (origin + upstream)"
# The write-eval-adapter skill runs a preflight collision check against
# `upstream/main` (EverMind-AI/EverMemOS) so it doesn't clobber an adapter
# name a human PR already claimed upstream. Cloud clones only have `origin`;
# add `upstream` idempotently here. If it's already set, we leave it alone.
if ! git remote get-url upstream >/dev/null 2>&1; then
  git remote add upstream https://github.com/EverMind-AI/EverMemOS.git
  echo "  ✅ added upstream remote"
else
  echo "  ✅ upstream remote already configured"
fi
# Warm the fetch cache so the first preflight check doesn't pay cold latency.
# Network-fail is non-fatal — the skill will re-fetch and surface the error.
git fetch upstream main --quiet 2>/dev/null || \
  echo "  (upstream fetch failed — skill will retry at collision-check time)"
echo "::endgroup::"

echo "::group::Python dependencies (evaluation-full)"
uv sync --group evaluation-full
echo "::endgroup::"

# EverMemOS docker images are intentionally NOT preloaded. Auto-bench candidates
# are local black-box systems — they never depend on EverOS's MongoDB / ES /
# Milvus / Redis stack. Pulling those images wastes cache and (more importantly)
# misleads the routine agent into thinking EverOS infra is required — past runs
# have tried `docker compose up` and wasted budget on quay.io 403s for etcd.

echo "::group::Candidate scratch dir"
mkdir -p /tmp/candidate
echo "::endgroup::"

echo "::group::Environment sanity"
missing=()
[[ -z "${LLM_API_KEY:-}" ]] && missing+=("LLM_API_KEY")
# Sophnet keys have no stable prefix; only require that the key is non-empty.
export LLM_BASE_URL="${LLM_BASE_URL:-https://www.sophnet.com/api/open-apis/v1}"
echo "  LLM_BASE_URL=${LLM_BASE_URL}"

if (( ${#missing[@]} > 0 )); then
  echo "  ❌ Missing required env vars: ${missing[*]}"
  echo "     Set them at https://claude.ai/code/routines for this routine."
  exit 1
fi
echo "::endgroup::"

echo "::group::Harness boot prereq (.env + MONGODB_HOST)"
# src/common_utils/load_env.py gates CLI startup on a .env file existing AND
# check_env_var=MONGODB_HOST being set — even though auto-bench candidates
# never touch EverOS Mongo. Provide a stub so the harness boots; a real value
# is not needed because no code path actually connects with it.
export MONGODB_HOST="${MONGODB_HOST:-auto-bench-unused-stub}"
if [[ ! -f .env ]] || ! grep -q '^MONGODB_HOST=' .env; then
  {
    echo "# stub written by .claude/setup.sh — auto-bench candidates do not use EverOS Mongo."
    echo "# Real Mongo config lives in the operator's own .env for interactive sessions."
    echo "MONGODB_HOST=${MONGODB_HOST}"
  } >> .env
  echo "  ✅ wrote stub MONGODB_HOST to .env"
else
  echo "  ✅ .env already contains MONGODB_HOST"
fi
echo "::endgroup::"

echo "::group::LoCoMo dataset"
if [[ ! -f evaluation/data/locomo/locomo10.json ]]; then
  echo "  ❌ evaluation/data/locomo/locomo10.json missing"
  echo "     The dataset is committed to the repo; this is unexpected on a clean clone."
  exit 1
fi
echo "  ✅ locomo10.json present ($(wc -c < evaluation/data/locomo/locomo10.json) bytes)"
echo "::endgroup::"

echo "Setup complete."
