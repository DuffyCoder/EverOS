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

import asyncio
import concurrent.futures
import contextlib
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional


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


@contextlib.contextmanager
def hermes_home_env(path: str):
    """Set ``HERMES_HOME`` for the duration of the block, then restore.

    **Safe only from inside the HermesExecutor worker**, which is
    single-threaded — calling this from multiple threads concurrently races
    on ``os.environ`` and corrupts state.
    """
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


class HermesExecutor:
    """Single-worker executor + async lock — the serialization boundary
    for every hermes provider call.

    Why single-worker: hermes plugins read ``HERMES_HOME`` from
    ``os.environ`` at call time (holographic in particular). If two calls
    ran concurrently with different target homes, one would silently write
    to the wrong sandbox. We force one-at-a-time execution so each call
    owns the env cleanly.

    **Process-wide singleton.** Production code always uses
    :func:`get_hermes_executor` so multiple ``HermesAdapter`` instances in
    the same process still serialize against each other. The class itself
    is public only so unit tests can build a throwaway instance.

    Call sites (adapter): ``initialize``, ``sync_turn``, ``on_session_end``,
    ``prefetch``, ``shutdown`` all flow through ``run()``. Non-hermes work
    (e.g. the shared answer LLM call) bypasses this and can parallelize.
    """

    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hermes"
        )
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._pool, lambda: fn(*args, **kwargs)
            )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


_DEFAULT_EXECUTOR: Optional[HermesExecutor] = None


def get_hermes_executor() -> HermesExecutor:
    """Return the process-wide singleton executor.

    Lazily constructed on first call. All ``HermesAdapter`` instances in
    the same process share this instance, so concurrent adapters still
    serialize against each other — this is the property §3.3.1 demands.
    """
    global _DEFAULT_EXECUTOR
    if _DEFAULT_EXECUTOR is None:
        _DEFAULT_EXECUTOR = HermesExecutor()
    return _DEFAULT_EXECUTOR
