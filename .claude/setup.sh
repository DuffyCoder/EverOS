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

echo "::group::External asset caches (HF Hub + tiktoken)"
# Background: most candidate memory frameworks download embedding models
# from huggingface.co and/or tiktoken BPE encodings from
# openaipublic.blob.core.windows.net at first import. PR#11/12/13 all
# failed smoke on these exact downloads (403 / connection refused) from
# the cloud container under the default "Trusted" network policy.
#
# Strategy: set cache dirs to a routine-scoped location, and if network
# access is allowed at setup time, pre-pull the two most common assets
# (all-MiniLM-L6-v2 + tiktoken o200k_base). If the pre-pull fails, log
# a clear hint and continue — the candidate run may still succeed if the
# cloud env has a route for HF that setup.sh didn't see, and failures
# at smoke time will now carry a pre-diagnosed [asset-download-failed]
# tag rather than looking like a generic crash.

export HF_HOME="${HF_HOME:-${HOME:-/root}/.cache/huggingface}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-$HF_HOME/hub}"
export TIKTOKEN_CACHE_DIR="${TIKTOKEN_CACHE_DIR:-${HOME:-/root}/.cache/tiktoken}"
mkdir -p "$HF_HOME" "$TIKTOKEN_CACHE_DIR"

# Operator escape hatches — if the default CDNs are blocked, point at a mirror.
# The HF_ENDPOINT var is read by huggingface_hub; uv/pip can't help here.
if [[ -n "${HF_ENDPOINT:-}" ]]; then
  echo "  ℹ️  HF_ENDPOINT=${HF_ENDPOINT} (operator override — HF Hub calls routed via mirror)"
fi

# Probe HF + tiktoken CDN egress. Probes are best-effort and do NOT fail the
# setup — a blocked egress is recorded so the routine agent can cite it in
# a [asset-download-failed] PR rather than guessing at the cause.
hf_ok=no
tiktoken_ok=no
if curl -fsSL --max-time 8 -o /dev/null \
      "${HF_ENDPOINT:-https://huggingface.co}/api/models/sentence-transformers/all-MiniLM-L6-v2" 2>/dev/null; then
  hf_ok=yes
fi
if curl -fsSL --max-time 8 -o /dev/null \
      "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken" 2>/dev/null; then
  tiktoken_ok=yes
fi
echo "  HF Hub reachable: $hf_ok"
echo "  tiktoken CDN reachable: $tiktoken_ok"

if [[ "$hf_ok" = yes ]]; then
  # Pre-pull the most common small embedding model so the first candidate
  # run doesn't pay a cold download cost (~90 MB). Non-fatal on failure.
  uv run --with "sentence-transformers>=2.2" python - <<'PY' 2>&1 | tail -3 || \
    echo "  (warm-pull failed — candidate may still work if it tries a different HF mirror)"
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    print("  ✅ pre-pulled all-MiniLM-L6-v2 to $HF_HOME")
except Exception as e:
    print(f"  ⚠️  pre-pull failed: {type(e).__name__}: {str(e)[:120]}")
PY
else
  echo "  ⚠️  HF Hub blocked — candidates needing downloaded embeddings will fail at smoke."
  echo "     Operator fix: switch cloud env Network access to a tier that allows huggingface.co,"
  echo "     OR set HF_ENDPOINT to an allowed mirror in the routine env vars."
fi

if [[ "$tiktoken_ok" = yes ]]; then
  uv run --with "tiktoken" python - <<'PY' 2>&1 | tail -3 || \
    echo "  (tiktoken warm-pull failed — GAM-style candidates may still fail at smoke)"
try:
    import tiktoken
    tiktoken.get_encoding("o200k_base")
    tiktoken.get_encoding("cl100k_base")
    print("  ✅ pre-pulled tiktoken o200k_base + cl100k_base to $TIKTOKEN_CACHE_DIR")
except Exception as e:
    print(f"  ⚠️  tiktoken warm-pull failed: {type(e).__name__}: {str(e)[:120]}")
PY
else
  echo "  ⚠️  openaipublic blob blocked — candidates using tiktoken encodings will fail at smoke."
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
