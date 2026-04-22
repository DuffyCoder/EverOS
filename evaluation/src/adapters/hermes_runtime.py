"""Hermes runtime utilities for the evaluation adapter.

This module provides three concerns, intentionally grouped because they share
a single trust boundary (the path-mounted hermes repo):

1. ``ensure_hermes_importable(repo_path)`` — prepend the hermes repo to
   ``sys.path`` so ``from plugins.memory import load_memory_provider`` works.
2. ``HermesExecutor`` — single-worker executor + async lock that serializes
   every hermes provider call and swaps ``HERMES_HOME`` per call.
3. ``hermes_home_env(path)`` — context manager for the env swap (used from
   inside the executor worker only).
"""
from __future__ import annotations

import sys
from pathlib import Path


def ensure_hermes_importable(repo_path: str) -> None:
    """Prepend the hermes repo to ``sys.path`` so its packages import cleanly.

    Idempotent — safe to call multiple times. Raises ``ValueError`` on an
    empty path and ``FileNotFoundError`` on a non-existent directory so
    misconfiguration fails loudly at adapter construction rather than later
    with an opaque ImportError.
    """
    if not repo_path:
        raise ValueError("hermes.repo_path is required (yaml) or HERMES_REPO_PATH (env)")
    resolved = Path(repo_path).expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"hermes repo_path does not exist: {resolved}")
    entry = str(resolved)
    if entry not in sys.path:
        sys.path.insert(0, entry)
