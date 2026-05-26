"""Multi-key LLM API key pool helpers.

Centralizes ``LLM_API_KEY[_<N>]`` env discovery + per-conv dispatch so the
adapter, answer_stage, and llm_judge can't drift as the pool grows.

The pool is read once and cached for the process lifetime — env is treated
as immutable after first call. Tests mutating the env via ``monkeypatch``
must call ``_reset_pool_cache_for_testing()`` so a stale cache doesn't mask
the new env state.
"""
from __future__ import annotations

import hashlib
import os
import re

# Extreme upper bound for the env scan. Actual stop point is dictated by
# _SPARSE_LOOKAHEAD; this only protects against a pathologically dense env.
_MAX_SCAN = 256
# How many consecutive blank suffix slots to tolerate before giving up. Lets
# an operator skip a number (``_3`` blank, ``_4`` set) without losing keys.
_SPARSE_LOOKAHEAD = 4

# Alternate-slot names. Adapters forwarding env into containers MUST filter
# these out so a per-conv key dispatch isn't undermined by leaked alternates.
_ALT_KEY_RE = re.compile(r"^LLM_API_KEY_\d+$")
# Trailing decimal suffix on a conv id drives perfect round-robin dispatch
# when the dataset uses sequential ids (e.g. LoCoMo's ``locomo_<n>``).
_NUMERIC_SUFFIX_RE = re.compile(r"(\d+)$")

_pool_cache: list[str] | None = None


def is_alt_llm_key_var(name: str) -> bool:
    """True if ``name`` is an alternate ``LLM_API_KEY_<N>`` slot."""
    return _ALT_KEY_RE.match(name) is not None


def collect_llm_key_pool() -> list[str]:
    """Return ``[LLM_API_KEY, LLM_API_KEY_2, ...]`` from host env, in order.

    Memoized: first call scans env, subsequent calls return the cached list.
    """
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    keys: list[str] = []
    primary = os.environ.get("LLM_API_KEY", "").strip()
    if primary:
        keys.append(primary)
    consecutive_misses = 0
    for n in range(2, _MAX_SCAN + 1):
        v = os.environ.get(f"LLM_API_KEY_{n}", "").strip()
        if v:
            keys.append(v)
            consecutive_misses = 0
            continue
        consecutive_misses += 1
        if consecutive_misses > _SPARSE_LOOKAHEAD:
            break
    _pool_cache = keys
    return _pool_cache


def count_llm_key_pool() -> int:
    return len(collect_llm_key_pool())


def pick_key_for_conv(conv_id: str) -> str | None:
    """Deterministic per-conv key from the pool.

    1. Trailing decimal suffix → ``<n> % len(pool)`` (perfect round-robin).
    2. Otherwise sha256(conv_id) — stable across processes so retries hit
       the same key (debuggable on the provider dashboard).
    """
    pool = collect_llm_key_pool()
    if not pool:
        return None
    m = _NUMERIC_SUFFIX_RE.search(conv_id or "")
    if m is not None:
        idx = int(m.group(1)) % len(pool)
    else:
        digest = hashlib.sha256((conv_id or "").encode()).digest()
        idx = int.from_bytes(digest[:4], "big") % len(pool)
    return pool[idx]


def _reset_pool_cache_for_testing() -> None:
    """Test-only: drop memoized pool so a monkeypatched env is rescanned."""
    global _pool_cache
    _pool_cache = None
