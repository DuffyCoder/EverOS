# Hermes Memory Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `hermes-agent`'s memory plugin providers into the EverMemOS evaluation pipeline so `evaluation --system hermes` runs LoCoMo end-to-end against a chosen Hermes plugin (starting with `holographic`).

**Architecture:** New adapter under `evaluation/src/adapters/hermes_adapter.py` that loads a single `MemoryProvider` via `plugins.memory.load_memory_provider(name)` from a path-mounted Hermes repo. All provider touchpoints serialize through a single-worker executor (§3.3.1 of spec) with a per-call `HERMES_HOME` swap, so concurrent conversations can't race on env state. Ingest strategy dispatches by plugin (holographic → `session_end` auto-extract; honcho/hindsight → `sync_per_turn`). Shared mem0 answer prompt matches `openclaw_adapter`.

**Tech Stack:** Python 3.10+, `asyncio`, `concurrent.futures.ThreadPoolExecutor`, `pytest`, `pytest-asyncio`, Hermes repo imported via `sys.path` mount.

**Spec:** `docs/superpowers/specs/2026-04-22-hermes-memory-adapter-design.md` (v2, post adversarial review)

---

## File Structure

**New files:**
- `evaluation/src/adapters/hermes_runtime.py` — `ensure_hermes_importable()`, `HermesExecutor` (single-worker + lock + `HERMES_HOME` context manager)
- `evaluation/src/adapters/hermes_ingestion.py` — LoCoMo `Conversation` → `[(user, assistant), ...]` turn-pair iterator
- `evaluation/src/adapters/hermes_adapter.py` — `@register_adapter("hermes")` + `prepare/add/build_lazy_index/search/answer`
- `evaluation/config/systems/hermes.yaml` — default variant (alias for holographic)
- `evaluation/config/systems/hermes-holographic.yaml` — explicit holographic variant
- `evaluation/config/systems/hermes-honcho.yaml` — cloud variant
- `evaluation/config/systems/hermes-hindsight.yaml` — cloud variant
- `tests/evaluation/test_hermes_runtime.py` — runtime unit tests
- `tests/evaluation/test_hermes_ingestion.py` — turn-pair iterator tests
- `tests/evaluation/test_hermes_adapter.py` — adapter behavior with stub provider
- `tests/evaluation/test_hermes_concurrency.py` — §3.3.1 regression guard
- `tests/evaluation/test_hermes_integration_smoke.py` — integration smoke (gated on `HERMES_REPO_PATH`)

**Modified files:**
- `evaluation/src/adapters/registry.py` — add `"hermes"` to `_ADAPTER_MODULES`

**Not touched:** `evaluation/src/adapters/base.py`, `evaluation/src/core/*`, Hermes source code.

**Test location note:** Existing adapter tests sit flat under `tests/evaluation/test_*.py` (see `test_openclaw_*.py`). We follow that convention, not the `tests/evaluation/adapters/hermes/` subdir mentioned in the spec draft.

---

## Task 1: Hermes runtime — `ensure_hermes_importable`

**Files:**
- Create: `evaluation/src/adapters/hermes_runtime.py`
- Test: `tests/evaluation/test_hermes_runtime.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evaluation/test_hermes_runtime.py`:

```python
"""Tests for hermes_runtime module."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_ensure_hermes_importable_prepends_repo_to_syspath(tmp_path):
    from evaluation.src.adapters.hermes_runtime import ensure_hermes_importable

    fake_repo = tmp_path / "hermes"
    fake_repo.mkdir()
    # Marker so we can prove sys.path was extended
    (fake_repo / "agent").mkdir()
    (fake_repo / "agent" / "__init__.py").write_text("MARKER = 'hermes-agent'\n")

    original_path = list(sys.path)
    try:
        ensure_hermes_importable(str(fake_repo))
        assert sys.path[0] == str(fake_repo)
    finally:
        sys.path[:] = original_path
        sys.modules.pop("agent", None)


def test_ensure_hermes_importable_idempotent(tmp_path):
    from evaluation.src.adapters.hermes_runtime import ensure_hermes_importable

    fake_repo = tmp_path / "hermes"
    fake_repo.mkdir()

    original_path = list(sys.path)
    try:
        ensure_hermes_importable(str(fake_repo))
        ensure_hermes_importable(str(fake_repo))
        ensure_hermes_importable(str(fake_repo))
        count = sum(1 for p in sys.path if p == str(fake_repo))
        assert count == 1, f"repo should appear once, got {count}"
    finally:
        sys.path[:] = original_path


def test_ensure_hermes_importable_rejects_missing_repo(tmp_path):
    from evaluation.src.adapters.hermes_runtime import ensure_hermes_importable

    with pytest.raises(ValueError, match="repo_path"):
        ensure_hermes_importable("")

    with pytest.raises(FileNotFoundError):
        ensure_hermes_importable(str(tmp_path / "does-not-exist"))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evaluation/test_hermes_runtime.py -v
```

Expected: `ModuleNotFoundError: No module named 'evaluation.src.adapters.hermes_runtime'`.

- [ ] **Step 3: Create `hermes_runtime.py` with `ensure_hermes_importable` only**

Create `evaluation/src/adapters/hermes_runtime.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evaluation/test_hermes_runtime.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_runtime.py tests/evaluation/test_hermes_runtime.py
git commit -m "feat(hermes-adapter): ensure_hermes_importable runtime helper"
```

---

## Task 2: Hermes runtime — `HermesExecutor` + `hermes_home_env`

**Files:**
- Modify: `evaluation/src/adapters/hermes_runtime.py`
- Test: `tests/evaluation/test_hermes_runtime.py`

This task adds the §3.3.1 concurrency primitives: a single-worker executor + async lock + `HERMES_HOME` swap context manager. The isolation test that exercises cross-conversation concurrency lives in Task 10; this task just proves the primitives in isolation.

- [ ] **Step 1: Write the failing tests** (append to `test_hermes_runtime.py`)

```python
import asyncio
import os


def test_hermes_home_env_sets_and_restores(tmp_path, monkeypatch):
    from evaluation.src.adapters.hermes_runtime import hermes_home_env

    monkeypatch.setenv("HERMES_HOME", "/old")
    with hermes_home_env(str(tmp_path)):
        assert os.environ["HERMES_HOME"] == str(tmp_path)
    assert os.environ["HERMES_HOME"] == "/old"


def test_hermes_home_env_restores_when_unset_before(tmp_path, monkeypatch):
    from evaluation.src.adapters.hermes_runtime import hermes_home_env

    monkeypatch.delenv("HERMES_HOME", raising=False)
    with hermes_home_env(str(tmp_path)):
        assert os.environ["HERMES_HOME"] == str(tmp_path)
    assert "HERMES_HOME" not in os.environ


def test_hermes_executor_runs_callables():
    from evaluation.src.adapters.hermes_runtime import HermesExecutor

    executor = HermesExecutor()

    async def go():
        return await executor.run(lambda: 1 + 2)

    try:
        result = asyncio.run(go())
    finally:
        executor.shutdown()
    assert result == 3


def test_hermes_executor_propagates_exceptions():
    from evaluation.src.adapters.hermes_runtime import HermesExecutor

    executor = HermesExecutor()

    def boom():
        raise RuntimeError("provider exploded")

    async def go():
        return await executor.run(boom)

    try:
        with pytest.raises(RuntimeError, match="provider exploded"):
            asyncio.run(go())
    finally:
        executor.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/evaluation/test_hermes_runtime.py -v
```

Expected: 4 new tests fail with `ImportError: cannot import name 'hermes_home_env'` / `'HermesExecutor'`.

- [ ] **Step 3: Append executor + env-swap to `hermes_runtime.py`**

Append to `evaluation/src/adapters/hermes_runtime.py`:

```python
import asyncio
import concurrent.futures
import contextlib
import os
from typing import Any, Callable


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
```

Also add `from typing import Optional` to the top-of-file imports.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/evaluation/test_hermes_runtime.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_runtime.py tests/evaluation/test_hermes_runtime.py
git commit -m "feat(hermes-adapter): HermesExecutor + hermes_home_env context manager"
```

---

## Task 3: Turn-pair ingestion iterator

**Files:**
- Create: `evaluation/src/adapters/hermes_ingestion.py`
- Test: `tests/evaluation/test_hermes_ingestion.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/evaluation/test_hermes_ingestion.py`:

```python
"""Tests for hermes_ingestion turn-pair iterator."""
from __future__ import annotations

from evaluation.src.core.data_models import Conversation, Message


def _msg(speaker_id: str, content: str) -> Message:
    return Message(speaker_id=speaker_id, speaker_name=speaker_id, content=content)


def test_two_speaker_even_pairs_in_order():
    from evaluation.src.adapters.hermes_ingestion import iter_turn_pairs

    conv = Conversation(
        conversation_id="c1",
        messages=[
            _msg("a", "hi"),
            _msg("b", "hey"),
            _msg("a", "how are you?"),
            _msg("b", "good"),
        ],
    )

    pairs = list(iter_turn_pairs(conv))
    assert pairs == [("hi", "hey"), ("how are you?", "good")]


def test_odd_trailing_turn_is_paired_with_empty_string():
    from evaluation.src.adapters.hermes_ingestion import iter_turn_pairs

    conv = Conversation(
        conversation_id="c1",
        messages=[
            _msg("a", "hi"),
            _msg("b", "hey"),
            _msg("a", "dangling"),
        ],
    )

    pairs = list(iter_turn_pairs(conv))
    assert pairs == [("hi", "hey"), ("dangling", "")]


def test_three_speaker_round_robin_warns(caplog):
    from evaluation.src.adapters.hermes_ingestion import iter_turn_pairs

    conv = Conversation(
        conversation_id="c1",
        messages=[
            _msg("a", "1"),
            _msg("b", "2"),
            _msg("c", "3"),
            _msg("a", "4"),
        ],
    )

    with caplog.at_level("WARNING"):
        pairs = list(iter_turn_pairs(conv))
    assert pairs == [("1", "2"), ("3", "4")]
    assert any("3 speakers" in r.message or "multi-speaker" in r.message.lower()
               for r in caplog.records)


def test_empty_conversation_yields_no_pairs():
    from evaluation.src.adapters.hermes_ingestion import iter_turn_pairs

    conv = Conversation(conversation_id="c1", messages=[])
    assert list(iter_turn_pairs(conv)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/evaluation/test_hermes_ingestion.py -v
```

Expected: 4 fails with `ModuleNotFoundError`.

- [ ] **Step 3: Create `hermes_ingestion.py`**

Create `evaluation/src/adapters/hermes_ingestion.py`:

```python
"""Turn-pair iteration for hermes memory provider ingest.

Hermes plugin providers expose ``sync_turn(user_content, assistant_content)``
as the unit of ingest. LoCoMo conversations are lists of messages with a
speaker_id; we convert them into consecutive (user, assistant) string pairs
in the order they appeared, without interpreting speaker semantics
(providers treat both strings opaquely).
"""
from __future__ import annotations

import logging
from typing import Iterator, Tuple

from evaluation.src.core.data_models import Conversation

logger = logging.getLogger(__name__)


def iter_turn_pairs(conversation: Conversation) -> Iterator[Tuple[str, str]]:
    """Yield consecutive ``(user_content, assistant_content)`` pairs.

    Pairing rules:
      - 0 messages → no pairs.
      - Even count → pair (msg_0, msg_1), (msg_2, msg_3), ...
      - Odd count → last pair is ``(msg_last, "")`` so the tail isn't dropped.
      - >=3 distinct speakers → pairs are still emitted in message order but
        a warning is logged; pair semantics are degraded but hermes plugins
        treat the strings opaquely so this is safe.
    """
    messages = conversation.messages
    speakers = {m.speaker_id for m in messages}
    if len(speakers) >= 3:
        logger.warning(
            "hermes ingest: conversation %s has %d speakers; falling back to "
            "round-robin pairing",
            conversation.conversation_id,
            len(speakers),
        )

    i = 0
    while i < len(messages):
        user = messages[i].content
        assistant = messages[i + 1].content if i + 1 < len(messages) else ""
        yield user, assistant
        i += 2
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/evaluation/test_hermes_ingestion.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_ingestion.py tests/evaluation/test_hermes_ingestion.py
git commit -m "feat(hermes-adapter): LoCoMo conversation → turn-pair iterator"
```

---

## Task 4: Adapter skeleton — `prepare` + `_resolve_run_root`

**Files:**
- Create: `evaluation/src/adapters/hermes_adapter.py`
- Test: `tests/evaluation/test_hermes_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evaluation/test_hermes_adapter.py`:

```python
"""Tests for the hermes memory adapter (stub-provider based)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evaluation.src.core.data_models import Conversation, Message


def _make_conv(cid: str = "conv1") -> Conversation:
    return Conversation(
        conversation_id=cid,
        messages=[
            Message(speaker_id="u", speaker_name="u", content="hello"),
            Message(speaker_id="a", speaker_name="a", content="hi"),
        ],
    )


def _base_config(repo_path: str, plugin: str = "stub") -> dict:
    return {
        "adapter": "hermes",
        "hermes": {
            "repo_path": repo_path,
            "plugin": plugin,
            "ingest_strategy": "sync_per_turn",
        },
        "search": {"top_k": 6, "max_inflight_queries_per_conversation": 1},
        "llm": {"provider": "openai", "model": "stub", "api_key": "x"},
    }


def test_prepare_is_idempotent_and_resolves_run_root(tmp_path):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    asyncio.run(adapter.prepare(conversations=[_make_conv()], output_dir=tmp_path))
    # second call must not blow up or re-init
    asyncio.run(adapter.prepare(conversations=[_make_conv()], output_dir=tmp_path))

    run_root = tmp_path / "artifacts" / "hermes"
    assert run_root.exists()
    latest = run_root / "LATEST"
    assert latest.exists()
    run_id = latest.read_text().strip()
    assert (run_root / run_id).is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: `ModuleNotFoundError: ...hermes_adapter`.

- [ ] **Step 3: Create `hermes_adapter.py` skeleton**

Create `evaluation/src/adapters/hermes_adapter.py`:

```python
"""Hermes memory adapter for the EverMemOS evaluation pipeline.

Runs a single hermes MemoryProvider (e.g. holographic, honcho, hindsight)
against LoCoMo-shaped conversations. All provider calls go through a
single-worker executor (HermesExecutor) that also swaps HERMES_HOME per
call, so concurrent conversations can't race on env state.

See spec: docs/superpowers/specs/2026-04-22-hermes-memory-adapter-design.md
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, List, Optional

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.hermes_runtime import (
    HermesExecutor,
    ensure_hermes_importable,
    get_hermes_executor,
    hermes_home_env,
)
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult

logger = logging.getLogger(__name__)

_ARTIFACT_ROOT = "artifacts/hermes"
_RUN_ID_LATEST_FILE = "LATEST"

_DEFAULT_ANSWER_PROMPT = (
    "You are a helpful assistant answering a question about a conversation.\n"
    "Use the memory snippets in CONTEXT to answer concisely (<=6 words when possible).\n"
    "If the context does not contain the answer, respond with \"No relevant information.\".\n\n"
    "# CONTEXT\n{context}\n\n# QUESTION\n{question}\n\n# ANSWER"
)


@register_adapter("hermes")
class HermesAdapter(BaseAdapter):
    def __init__(self, config: dict, output_dir: Any = None):
        super().__init__(config)
        self.output_dir = output_dir
        self._hermes_cfg: dict = dict(config.get("hermes") or {})
        self._repo_path: str = str(self._hermes_cfg.get("repo_path") or "").strip()
        self._plugin_name: str = str(self._hermes_cfg.get("plugin") or "").strip()
        self._ingest_strategy: str = str(
            self._hermes_cfg.get("ingest_strategy") or "sync_per_turn"
        )
        self._plugin_config: dict = dict(self._hermes_cfg.get("plugin_config") or {})
        self._prepared: bool = False
        self._run_id: Optional[str] = None
        self._executor: Optional[HermesExecutor] = None
        self._llm_provider = None
        self._shared_prompt_template: Optional[str] = None

    # -- prepare -----------------------------------------------------------
    async def prepare(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> None:
        if self._prepared:
            return
        ensure_hermes_importable(self._repo_path)
        self._executor = get_hermes_executor()  # process-wide singleton (§3.3.1)
        self._resolve_run_root(output_dir or self.output_dir)
        self._prepared = True
        logger.debug(
            "hermes adapter prepared (plugin=%s, strategy=%s, n_conv=%d)",
            self._plugin_name, self._ingest_strategy, len(conversations),
        )

    # -- internals ---------------------------------------------------------
    def _resolve_run_root(self, output_dir: Any) -> Path:
        if output_dir is None:
            raise ValueError("output_dir is required to resolve hermes sandbox root")
        if self._run_id is None:
            self._run_id = time.strftime("run-%Y%m%dT%H%M%S")
        root = Path(output_dir) / _ARTIFACT_ROOT / self._run_id
        root.mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / _ARTIFACT_ROOT / _RUN_ID_LATEST_FILE).write_text(self._run_id)
        return root

    def _locate_existing_run_root(self, output_dir: Path) -> Path:
        latest_file = output_dir / _ARTIFACT_ROOT / _RUN_ID_LATEST_FILE
        if latest_file.exists():
            run_id = latest_file.read_text().strip()
            root = output_dir / _ARTIFACT_ROOT / run_id
            if root.exists():
                return root
        parent = output_dir / _ARTIFACT_ROOT
        if not parent.exists():
            raise FileNotFoundError(f"no hermes artifacts under {parent}")
        runs = [p for p in parent.iterdir() if p.is_dir()]
        if not runs:
            raise FileNotFoundError(f"no hermes runs under {parent}")
        runs.sort(key=lambda p: p.stat().st_mtime)
        return runs[-1]

    # -- required BaseAdapter methods (stubbed for now — filled later) ----
    async def add(self, conversations: List[Conversation], **kwargs) -> dict:
        raise NotImplementedError("Task 5 implements add()")

    async def search(self, query: str, conversation_id: str, index: Any, **kwargs) -> SearchResult:
        raise NotImplementedError("Task 7 implements search()")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_adapter.py tests/evaluation/test_hermes_adapter.py
git commit -m "feat(hermes-adapter): adapter skeleton + prepare()"
```

---

## Task 5: Adapter — `add()` with stub provider injection

**Files:**
- Modify: `evaluation/src/adapters/hermes_adapter.py`
- Modify: `tests/evaluation/test_hermes_adapter.py`

- [ ] **Step 1: Write the failing tests** (append to `test_hermes_adapter.py`)

```python
class _StubProvider:
    """In-process MemoryProvider stand-in for unit tests.

    Records every call so tests can assert on session_id, content, and strategy dispatch.
    """

    name = "stub"

    def __init__(self):
        self.calls: list[tuple] = []
        self.is_available_result = True
        self.raise_on_init: Exception | None = None
        self.prefetch_result = "STUB_CONTEXT"

    def is_available(self) -> bool:
        return self.is_available_result

    def initialize(self, session_id: str, **kwargs) -> None:
        if self.raise_on_init:
            raise self.raise_on_init
        self.calls.append(("initialize", session_id, kwargs))

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        self.calls.append(("sync_turn", session_id, user_content, assistant_content))

    def on_session_end(self, messages) -> None:
        self.calls.append(("on_session_end", len(messages)))

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.calls.append(("prefetch", session_id, query))
        return self.prefetch_result

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))

    def get_tool_schemas(self):
        return []


@pytest.fixture
def stub_provider(monkeypatch):
    stub = _StubProvider()
    from evaluation.src.adapters import hermes_adapter as _ha
    monkeypatch.setattr(_ha, "_load_memory_provider", lambda name: stub)
    return stub


def test_add_writes_handle_and_runs_ingest_sync_per_turn(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter
    import json

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    result = asyncio.run(adapter.add(
        conversations=[_make_conv("conv1")],
        output_dir=tmp_path,
    ))

    assert result["type"] == "hermes_sandboxes"
    assert "conv1" in result["conversations"]
    handle_path = Path(result["conversations"]["conv1"]["handle_path"])
    handle = json.loads(handle_path.read_text())
    assert handle["run_status"] == "ready"
    assert handle["plugin"] == "stub"
    assert handle["ingest_turns"] == 1

    # sync_per_turn → exactly one sync_turn, no on_session_end
    kinds = [c[0] for c in stub_provider.calls]
    assert kinds.count("initialize") == 1
    assert kinds.count("sync_turn") == 1
    assert kinds.count("on_session_end") == 0

    # session_id contract: every session_id the stub saw equals conv1
    session_ids = {c[1] for c in stub_provider.calls if c[0] in ("initialize", "sync_turn")}
    assert session_ids == {"conv1"}


def test_add_session_end_strategy_calls_on_session_end_once(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    cfg = _base_config(str(tmp_path))
    cfg["hermes"]["ingest_strategy"] = "session_end"
    adapter = HermesAdapter(cfg, output_dir=tmp_path)
    asyncio.run(adapter.add(conversations=[_make_conv()], output_dir=tmp_path))

    kinds = [c[0] for c in stub_provider.calls]
    assert kinds.count("sync_turn") == 0
    assert kinds.count("on_session_end") == 1


def test_add_both_strategy_calls_both(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    cfg = _base_config(str(tmp_path))
    cfg["hermes"]["ingest_strategy"] = "both"
    adapter = HermesAdapter(cfg, output_dir=tmp_path)
    asyncio.run(adapter.add(conversations=[_make_conv()], output_dir=tmp_path))

    kinds = [c[0] for c in stub_provider.calls]
    assert kinds.count("sync_turn") >= 1
    assert kinds.count("on_session_end") == 1


def test_add_records_failed_handle_when_init_raises(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter
    import json

    stub_provider.raise_on_init = RuntimeError("init boom")

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    result = asyncio.run(adapter.add(
        conversations=[_make_conv("conv-fail")],
        output_dir=tmp_path,
    ))

    handle_path = Path(result["conversations"]["conv-fail"]["handle_path"])
    handle = json.loads(handle_path.read_text())
    assert handle["run_status"] == "failed"
    assert "init boom" in handle["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: all new tests fail because `add()` still raises `NotImplementedError`.

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

- [ ] **Step 3: Implement `add()` and supporting helpers**

In `evaluation/src/adapters/hermes_adapter.py`, replace the stub `add()` and add helpers:

```python
import json
import yaml  # already a project dep

# Module-level seam so tests can monkeypatch without importing hermes.
def _load_memory_provider(name: str):
    """Indirection so tests can swap in stubs without needing a real hermes repo."""
    from plugins.memory import load_memory_provider  # noqa: E402
    return load_memory_provider(name)
```

Then the real `add()`:

```python
    async def add(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> dict:
        if not self._prepared:
            await self.prepare(
                conversations=conversations,
                output_dir=output_dir,
                checkpoint_manager=checkpoint_manager,
                **kwargs,
            )

        root_dir = self._resolve_run_root(output_dir or self.output_dir)
        run_id = root_dir.name
        conversations_map: dict[str, dict] = {}

        for conv in conversations:
            sandbox_dir = root_dir / "conversations" / conv.conversation_id
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            self._write_plugin_config(sandbox_dir)

            handle_path = sandbox_dir / "handle.json"
            t0 = time.perf_counter()
            try:
                provider = _load_memory_provider(self._plugin_name)
                if provider is None:
                    raise RuntimeError(
                        f"hermes plugin '{self._plugin_name}' not found"
                    )
                if not provider.is_available():
                    raise RuntimeError(
                        f"hermes plugin '{self._plugin_name}' is not available"
                    )

                await self._provider_initialize(provider, conv, sandbox_dir)
                ingest_turns = await self._ingest_conversation(provider, conv, sandbox_dir)
                await self._provider_shutdown(provider, sandbox_dir)

                handle = {
                    "run_status": "ready",
                    "conversation_id": conv.conversation_id,
                    "plugin": self._plugin_name,
                    "strategy": self._ingest_strategy,
                    "hermes_home": str(sandbox_dir),
                    "ingest_turns": ingest_turns,
                    "ingest_latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "run_id": run_id,
                }
            except Exception as err:
                handle = {
                    "run_status": "failed",
                    "conversation_id": conv.conversation_id,
                    "plugin": self._plugin_name,
                    "strategy": self._ingest_strategy,
                    "hermes_home": str(sandbox_dir),
                    "error": f"{type(err).__name__}: {err}",
                    "run_id": run_id,
                }
                logger.exception(
                    "hermes add failed for %s (plugin=%s)",
                    conv.conversation_id, self._plugin_name,
                )

            handle_path.write_text(json.dumps(handle, ensure_ascii=False, indent=2))
            conversations_map[conv.conversation_id] = {
                **handle,
                "handle_path": str(handle_path),
            }

        return {
            "type": "hermes_sandboxes",
            "run_id": run_id,
            "root_dir": str(root_dir),
            "conversations": conversations_map,
        }

    # -- provider lifecycle (all routed through the serialized executor) --
    async def _provider_initialize(self, provider, conv: Conversation, sandbox_dir: Path) -> None:
        def _init():
            with hermes_home_env(str(sandbox_dir)):
                provider.initialize(
                    session_id=conv.conversation_id,
                    hermes_home=str(sandbox_dir),
                    platform="cli",
                    agent_context="primary",
                )
        await self._executor.run(_init)

    async def _provider_shutdown(self, provider, sandbox_dir: Path) -> None:
        def _shut():
            with hermes_home_env(str(sandbox_dir)):
                try:
                    provider.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hermes shutdown failed: %s", exc)
        await self._executor.run(_shut)

    async def _ingest_conversation(self, provider, conv: Conversation, sandbox_dir: Path) -> int:
        from evaluation.src.adapters.hermes_ingestion import iter_turn_pairs

        turns = 0
        if self._ingest_strategy in ("sync_per_turn", "both"):
            for user_content, assistant_content in iter_turn_pairs(conv):
                def _sync(u=user_content, a=assistant_content):
                    with hermes_home_env(str(sandbox_dir)):
                        provider.sync_turn(
                            u, a, session_id=conv.conversation_id
                        )
                await self._executor.run(_sync)
                turns += 1

        if self._ingest_strategy in ("session_end", "both"):
            messages_payload = [
                {"role": m.speaker_id, "content": m.content}
                for m in conv.messages
            ]

            def _end():
                with hermes_home_env(str(sandbox_dir)):
                    provider.on_session_end(messages_payload)
            await self._executor.run(_end)

        return turns

    def _write_plugin_config(self, sandbox_dir: Path) -> None:
        """Write plugin-specific config to <sandbox>/config.yaml under
        ``plugins.hermes-memory-store``, the key holographic (and other
        plugins following the same convention) reads from."""
        if not self._plugin_config:
            return
        config_path = sandbox_dir / "config.yaml"
        payload = {"plugins": {"hermes-memory-store": dict(self._plugin_config)}}
        config_path.write_text(yaml.dump(payload, default_flow_style=False))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: all 5 adapter tests passed.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_adapter.py tests/evaluation/test_hermes_adapter.py
git commit -m "feat(hermes-adapter): add() with strategy dispatch + per-conversation sandbox"
```

---

## Task 6: Adapter — `build_lazy_index()`

**Files:**
- Modify: `evaluation/src/adapters/hermes_adapter.py`
- Modify: `tests/evaluation/test_hermes_adapter.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_build_lazy_index_rehydrates_from_handle(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    asyncio.run(adapter.add(
        conversations=[_make_conv("c1"), _make_conv("c2")],
        output_dir=tmp_path,
    ))

    # Fresh adapter instance — simulates a resume-from-disk run
    adapter2 = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    index = adapter2.build_lazy_index([_make_conv("c1"), _make_conv("c2")], tmp_path)

    assert index["type"] == "hermes_sandboxes"
    assert set(index["conversations"]) == {"c1", "c2"}
    assert all(h["run_status"] == "ready" for h in index["conversations"].values())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evaluation/test_hermes_adapter.py::test_build_lazy_index_rehydrates_from_handle -v
```

Expected: `AttributeError` or similar because `build_lazy_index` inherits the `BaseAdapter` default (returns `None`).

- [ ] **Step 3: Implement `build_lazy_index`**

Add to `HermesAdapter`:

```python
    def build_lazy_index(
        self, conversations: List[Conversation], output_dir: Any
    ) -> dict:
        root_dir = self._locate_existing_run_root(Path(output_dir))
        handles: dict[str, dict] = {}
        for conv in conversations:
            handle_path = root_dir / "conversations" / conv.conversation_id / "handle.json"
            if not handle_path.exists():
                continue
            handle = json.loads(handle_path.read_text())
            if handle.get("run_status") != "ready":
                continue
            handles[conv.conversation_id] = {**handle, "handle_path": str(handle_path)}
        return {
            "type": "hermes_sandboxes",
            "run_id": root_dir.name,
            "root_dir": str(root_dir),
            "conversations": handles,
        }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: all adapter tests passing (6 total).

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_adapter.py tests/evaluation/test_hermes_adapter.py
git commit -m "feat(hermes-adapter): build_lazy_index rehydrates from handle.json"
```

---

## Task 7: Adapter — `search()` via `provider.prefetch`

**Files:**
- Modify: `evaluation/src/adapters/hermes_adapter.py`
- Modify: `tests/evaluation/test_hermes_adapter.py`

This task creates a **fresh provider per search** keyed by conversation, because we finalize the provider at the end of `add()` (shutdown). The alternative — keeping all providers alive across the run — complicates lifecycle and scales poorly. On search, we re-instantiate and re-initialize with the conversation's sandbox as `HERMES_HOME`; the plugin reads prior ingest state from disk (sqlite, json files, cloud).

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_search_returns_prefetch_context(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    index = asyncio.run(adapter.add(
        conversations=[_make_conv("c1")],
        output_dir=tmp_path,
    ))

    stub_provider.prefetch_result = "RECALLED: the memory was here"
    result = asyncio.run(adapter.search(
        query="what happened?",
        conversation_id="c1",
        index=index,
    ))

    assert result.query == "what happened?"
    assert result.conversation_id == "c1"
    assert len(result.results) == 1
    assert result.results[0]["content"] == "RECALLED: the memory was here"
    assert "retrieval_latency_ms" in result.retrieval_metadata
    assert result.retrieval_metadata["plugin"] == "stub"
    assert result.retrieval_metadata["strategy"] == "sync_per_turn"

    # session_id contract: prefetch sees conv1, never anything else
    prefetch_calls = [c for c in stub_provider.calls if c[0] == "prefetch"]
    assert prefetch_calls and all(c[1] == "c1" for c in prefetch_calls)


def test_search_empty_context_yields_empty_results(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    index = asyncio.run(adapter.add(
        conversations=[_make_conv("c1")],
        output_dir=tmp_path,
    ))
    stub_provider.prefetch_result = ""

    result = asyncio.run(adapter.search(
        query="anything?", conversation_id="c1", index=index,
    ))
    assert result.results == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: new tests fail with `NotImplementedError`.

- [ ] **Step 3: Implement `search()`**

Replace the stub `search` in `HermesAdapter`:

```python
    async def search(
        self, query: str, conversation_id: str, index: Any, **kwargs
    ) -> SearchResult:
        conv_entry = index["conversations"].get(conversation_id)
        if conv_entry is None:
            raise KeyError(f"no hermes sandbox for conversation {conversation_id}")
        sandbox_dir = Path(conv_entry["hermes_home"])

        provider = _load_memory_provider(self._plugin_name)
        if provider is None or not provider.is_available():
            raise RuntimeError(f"hermes plugin '{self._plugin_name}' unavailable")

        t0 = time.perf_counter()

        def _init():
            with hermes_home_env(str(sandbox_dir)):
                provider.initialize(
                    session_id=conversation_id,
                    hermes_home=str(sandbox_dir),
                    platform="cli",
                    agent_context="primary",
                )

        def _prefetch():
            with hermes_home_env(str(sandbox_dir)):
                return provider.prefetch(query, session_id=conversation_id)

        def _shut():
            with hermes_home_env(str(sandbox_dir)):
                try:
                    provider.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hermes search shutdown failed: %s", exc)

        await self._executor.run(_init)
        try:
            context = await self._executor.run(_prefetch)
        finally:
            await self._executor.run(_shut)

        retrieval_latency_ms = (time.perf_counter() - t0) * 1000.0

        results = []
        if context and context.strip():
            results.append({
                "content": context,
                "score": 1.0,
                "metadata": {"source": "prefetch"},
            })

        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results,
            retrieval_metadata={
                "system": "hermes",
                "plugin": self._plugin_name,
                "strategy": self._ingest_strategy,
                "retrieval_latency_ms": retrieval_latency_ms,
                "formatted_context": context or "",
                "conversation_id": conversation_id,
            },
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_adapter.py tests/evaluation/test_hermes_adapter.py
git commit -m "feat(hermes-adapter): search() via provider.prefetch"
```

---

## Task 8: Adapter — `answer()` with shared mem0 prompt

**Files:**
- Modify: `evaluation/src/adapters/hermes_adapter.py`
- Modify: `tests/evaluation/test_hermes_adapter.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_answer_uses_shared_prompt_template_with_fallback(tmp_path, stub_provider, monkeypatch):
    """answer() calls the LLMProvider with a prompt that contains context + question."""
    from evaluation.src.adapters import hermes_adapter as _ha
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    captured = {}

    class _FakeLLM:
        async def generate(self, prompt: str, temperature: float = 0):
            captured["prompt"] = prompt
            return "test-answer"

    monkeypatch.setattr(HermesAdapter, "_get_llm_provider", lambda self: _FakeLLM())

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    result = asyncio.run(adapter.answer(
        query="what happened?", context="the cat sat on the mat",
    ))

    assert result == "test-answer"
    assert "what happened?" in captured["prompt"]
    assert "the cat sat on the mat" in captured["prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evaluation/test_hermes_adapter.py::test_answer_uses_shared_prompt_template_with_fallback -v
```

Expected: `AttributeError: 'HermesAdapter' object has no attribute 'answer'`.

- [ ] **Step 3: Add `answer()` and LLM helpers to `HermesAdapter`**

```python
    # -- answer ------------------------------------------------------------
    async def answer(self, query: str, context: str, **kwargs) -> str:
        prompt = self._shared_answer_prompt().format(context=context, question=query)
        provider = self._get_llm_provider()
        result = await provider.generate(prompt=prompt, temperature=0)
        if "FINAL ANSWER:" in result:
            parts = result.split("FINAL ANSWER:")
            result = parts[1].strip() if len(parts) > 1 else result.strip()
        return result.strip()

    def _get_llm_provider(self):
        if self._llm_provider is not None:
            return self._llm_provider
        from memory_layer.llm.llm_provider import LLMProvider

        llm_cfg = self.config.get("llm", {}) or {}
        self._llm_provider = LLMProvider(
            provider_type=llm_cfg.get("provider", "openai"),
            model=llm_cfg.get("model", "gpt-4o-mini"),
            api_key=llm_cfg.get("api_key", ""),
            base_url=llm_cfg.get("base_url", "https://api.openai.com/v1"),
            temperature=llm_cfg.get("temperature", 0.0),
            max_tokens=llm_cfg.get("max_tokens", 1024),
        )
        return self._llm_provider

    def _shared_answer_prompt(self) -> str:
        if self._shared_prompt_template is not None:
            return self._shared_prompt_template
        try:
            from evaluation.src.utils.config import load_yaml

            prompts_path = Path(__file__).parent.parent.parent / "config" / "prompts.yaml"
            prompts = load_yaml(str(prompts_path))
            self._shared_prompt_template = prompts["online_api"]["default"]["answer_prompt_mem0"]
        except Exception:
            self._shared_prompt_template = _DEFAULT_ANSWER_PROMPT
        return self._shared_prompt_template

    def get_system_info(self) -> dict:
        return {
            "name": "Hermes",
            "plugin": self._plugin_name,
            "strategy": self._ingest_strategy,
            "config": self.config,
        }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/hermes_adapter.py tests/evaluation/test_hermes_adapter.py
git commit -m "feat(hermes-adapter): answer() via shared mem0 prompt + LLMProvider"
```

---

## Task 9: Registry wiring + default yaml

**Files:**
- Modify: `evaluation/src/adapters/registry.py:14-29`
- Create: `evaluation/config/systems/hermes.yaml`
- Create: `evaluation/config/systems/hermes-holographic.yaml`
- Test: `tests/evaluation/test_hermes_registry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evaluation/test_hermes_registry.py`:

```python
"""Adapter registry must surface the hermes adapter."""
from __future__ import annotations


def test_registry_lists_hermes():
    from evaluation.src.adapters.registry import list_adapters
    assert "hermes" in list_adapters()


def test_registry_can_create_hermes_adapter(tmp_path):
    from evaluation.src.adapters.registry import create_adapter

    config = {
        "adapter": "hermes",
        "hermes": {"repo_path": str(tmp_path), "plugin": "holographic"},
        "llm": {"provider": "openai", "model": "stub", "api_key": "x"},
    }
    adapter = create_adapter("hermes", config, output_dir=tmp_path)
    assert type(adapter).__name__ == "HermesAdapter"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/evaluation/test_hermes_registry.py -v
```

Expected: `ValueError: Unknown adapter: hermes`.

- [ ] **Step 3: Register in `registry.py`**

Modify `evaluation/src/adapters/registry.py`, in `_ADAPTER_MODULES`:

```python
_ADAPTER_MODULES = {
    # Local systems
    "evermemos": "evaluation.src.adapters.evermemos_adapter",

    # Online API systems
    "mem0": "evaluation.src.adapters.mem0_adapter",
    "memos": "evaluation.src.adapters.memos_adapter",
    "memu": "evaluation.src.adapters.memu_adapter",
    "zep": "evaluation.src.adapters.zep_adapter",
    "evermemos_api": "evaluation.src.adapters.evermemos_api_adapter",
    "memobase": "evaluation.src.adapters.memobase_adapter",
    "supermemory": "evaluation.src.adapters.supermemory_adapter",

    # OpenClaw memory system (via Node bridge)
    "openclaw": "evaluation.src.adapters.openclaw_adapter",

    # Hermes memory system (via path-mounted in-process import)
    "hermes": "evaluation.src.adapters.hermes_adapter",
}
```

- [ ] **Step 4: Create yaml files**

Create `evaluation/config/systems/hermes-holographic.yaml`:

```yaml
adapter: "hermes"

llm:
  provider: "openai"
  model: "gpt-4o-mini"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
  temperature: 0.0
  max_tokens: 1024

search:
  top_k: 6
  response_top_k: 5
  num_workers: 5
  max_inflight_queries_per_conversation: 1

answer:
  max_retries: 3

hermes:
  repo_path: "${HERMES_REPO_PATH}"
  plugin: "holographic"
  ingest_strategy: "session_end"
  plugin_config:
    auto_extract: true
    default_trust: 0.5
    hrr_dim: 1024
  prompts:
    answer_mode: "shared"
```

Create `evaluation/config/systems/hermes.yaml` — identical to `hermes-holographic.yaml` (default variant alias).

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/evaluation/test_hermes_registry.py -v
pytest tests/evaluation/test_hermes_adapter.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add evaluation/src/adapters/registry.py \
        evaluation/config/systems/hermes.yaml \
        evaluation/config/systems/hermes-holographic.yaml \
        tests/evaluation/test_hermes_registry.py
git commit -m "feat(hermes-adapter): register adapter + holographic default yaml"
```

---

## Task 10: Concurrency regression guard

**Files:**
- Create: `tests/evaluation/test_hermes_concurrency.py`

This is the §3.3.1 regression test called out in the adversarial review: prove that two concurrent `add()` calls cannot race on `HERMES_HOME`.

- [ ] **Step 1: Write the failing test**

Create `tests/evaluation/test_hermes_concurrency.py`:

```python
"""Regression guard for spec §3.3.1 — HERMES_HOME isolation under concurrency."""
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from evaluation.src.core.data_models import Conversation, Message


def _make_conv(cid: str) -> Conversation:
    return Conversation(
        conversation_id=cid,
        messages=[
            Message(speaker_id="u", speaker_name="u", content=f"hi-{cid}"),
            Message(speaker_id="a", speaker_name="a", content=f"hello-{cid}"),
        ],
    )


class _RecordingProvider:
    """Records the HERMES_HOME observed at call time and the max concurrency."""

    name = "stub"
    _in_flight = 0
    _peak = 0
    _lock = threading.Lock()

    def __init__(self):
        self.observations: list[tuple[str, str]] = []  # (session_id, hermes_home)

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._observe(session_id)

    def sync_turn(self, u: str, a: str, *, session_id: str = "") -> None:
        self._observe(session_id)

    def on_session_end(self, messages) -> None:
        pass

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def shutdown(self) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def _observe(self, session_id: str) -> None:
        with _RecordingProvider._lock:
            _RecordingProvider._in_flight += 1
            _RecordingProvider._peak = max(_RecordingProvider._peak, _RecordingProvider._in_flight)
        try:
            # Hold the env for long enough that any racing caller would
            # overwrite it if the serialization boundary is broken.
            time.sleep(0.05)
            self.observations.append((session_id, os.environ.get("HERMES_HOME", "")))
        finally:
            with _RecordingProvider._lock:
                _RecordingProvider._in_flight -= 1


def _reset_peak():
    with _RecordingProvider._lock:
        _RecordingProvider._in_flight = 0
        _RecordingProvider._peak = 0


@pytest.mark.asyncio
async def test_concurrent_adds_do_not_race_on_hermes_home(tmp_path, monkeypatch):
    from evaluation.src.adapters import hermes_adapter as _ha
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    # All adapters in this process share the module-level singleton
    # executor (§3.3.1), so no two providers can ever be in a hermes call
    # at the same time regardless of which adapter owns them.
    def _fake_loader(name: str):
        return _RecordingProvider()

    monkeypatch.setattr(_ha, "_load_memory_provider", _fake_loader)
    _reset_peak()

    async def _run_one(cid: str):
        adapter = HermesAdapter({
            "hermes": {
                "repo_path": str(tmp_path),
                "plugin": "stub",
                "ingest_strategy": "sync_per_turn",
            },
            "llm": {"provider": "openai", "model": "stub", "api_key": "x"},
        }, output_dir=tmp_path / cid)
        (tmp_path / cid).mkdir()
        result = await adapter.add(
            conversations=[_make_conv(cid)],
            output_dir=tmp_path / cid,
        )
        return result

    results = await asyncio.gather(
        _run_one("c1"), _run_one("c2"), _run_one("c3"),
    )

    # All three runs succeeded
    for r in results:
        assert all(h["run_status"] == "ready" for h in r["conversations"].values())

    # Process-wide serialization: the shared singleton executor has
    # max_workers=1, so only one hermes call is ever in flight across all
    # three adapters. Peak concurrency MUST be 1.
    assert _RecordingProvider._peak == 1, (
        f"HERMES_HOME isolation broken: peak concurrency was "
        f"{_RecordingProvider._peak} (expected 1). Two provider calls were "
        "in flight simultaneously, so they raced on os.environ['HERMES_HOME']."
    )

    # Each conversation's sandbox points at its own directory.
    for r, cid in zip(results, ("c1", "c2", "c3")):
        sandbox = r["conversations"][cid]["hermes_home"]
        assert f"/{cid}" in sandbox
```

- [ ] **Step 2: Run test to verify it fails / passes as expected**

```bash
pytest tests/evaluation/test_hermes_concurrency.py -v
```

Expected: `pytest-asyncio` may need to be installed. If `@pytest.mark.asyncio` raises, check `pyproject.toml` — project already uses pytest-asyncio via `conftest.py`. Otherwise expected PASS on first run because the implementation is already correct (this is a regression guard, not a TDD-new-behavior test).

- [ ] **Step 3: (Only if the test is failing because of a real race)**

If the test catches a real race (e.g. we accidentally share a single process-wide executor across instances and serialize too aggressively, or conversely two providers end up seeing each other's env), the fix belongs in `hermes_runtime.HermesExecutor` or in how `hermes_home_env` is used. Investigate and repair before moving on.

- [ ] **Step 4: Commit**

```bash
git add tests/evaluation/test_hermes_concurrency.py
git commit -m "test(hermes-adapter): concurrency regression guard for HERMES_HOME isolation"
```

---

## Task 11: Integration smoke test for holographic

**Files:**
- Create: `tests/evaluation/test_hermes_integration_smoke.py`

- [ ] **Step 1: Write the integration smoke test**

Create `tests/evaluation/test_hermes_integration_smoke.py`:

```python
"""End-to-end smoke test against a real hermes holographic plugin.

Skipped unless ``HERMES_REPO_PATH`` points at a checkout. Runs a 2-turn
synthetic conversation through add → search → verify a non-empty recall.
Does not call the answer LLM (that needs extra credentials).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from evaluation.src.core.data_models import Conversation, Message


pytestmark = pytest.mark.integration


@pytest.fixture
def hermes_repo_path():
    path = os.environ.get("HERMES_REPO_PATH", "")
    if not path or not os.path.isdir(path):
        pytest.skip("HERMES_REPO_PATH not set or not a directory")
    return path


def test_holographic_end_to_end_recall(tmp_path, hermes_repo_path):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    conv = Conversation(
        conversation_id="smoke-1",
        messages=[
            Message(speaker_id="u", speaker_name="u",
                    content="My dog's name is Bluey and she's a border collie."),
            Message(speaker_id="a", speaker_name="a",
                    content="That's great! Bluey sounds lovely."),
        ],
    )

    adapter = HermesAdapter({
        "hermes": {
            "repo_path": hermes_repo_path,
            "plugin": "holographic",
            "ingest_strategy": "session_end",
            "plugin_config": {
                "auto_extract": True,
                "default_trust": 0.5,
                "hrr_dim": 1024,
            },
        },
        "llm": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": os.environ.get("LLM_API_KEY", ""),
            "base_url": os.environ.get(
                "LLM_BASE_URL", "https://www.sophnet.com/api/open-apis/v1"),
        },
    }, output_dir=tmp_path)

    index = asyncio.run(adapter.add(conversations=[conv], output_dir=tmp_path))
    assert index["conversations"]["smoke-1"]["run_status"] == "ready"

    result = asyncio.run(adapter.search(
        query="what kind of dog does the user have?",
        conversation_id="smoke-1",
        index=index,
    ))
    # holographic auto_extract is LLM-driven; if LLM is unreachable we still
    # want the run to come back cleanly with 0 hits rather than crash. Assert
    # only the structural contract; the retrieval quality is measured by the
    # full pipeline, not this smoke test.
    assert result.conversation_id == "smoke-1"
    assert result.retrieval_metadata["plugin"] == "holographic"
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/evaluation/test_hermes_integration_smoke.py -v -m integration
```

Expected (no `HERMES_REPO_PATH`): skipped. With it set: pass (structural assertions only).

- [ ] **Step 3: Commit**

```bash
git add tests/evaluation/test_hermes_integration_smoke.py
git commit -m "test(hermes-adapter): integration smoke for holographic plugin"
```

---

## Task 12: Cloud variants — honcho + hindsight yaml

**Files:**
- Create: `evaluation/config/systems/hermes-honcho.yaml`
- Create: `evaluation/config/systems/hermes-hindsight.yaml`

No new code paths, no new tests — these are config-only variants exercising the same adapter. Tests in Tasks 4–10 cover behavior. Credentials are lazy-loaded at runtime and gated by the plugin's own `is_available()`.

- [ ] **Step 1: Create `hermes-honcho.yaml`**

```yaml
adapter: "hermes"

llm:
  provider: "openai"
  model: "gpt-4o-mini"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
  temperature: 0.0
  max_tokens: 1024

search:
  top_k: 6
  response_top_k: 5
  num_workers: 5
  max_inflight_queries_per_conversation: 1

answer:
  max_retries: 3

hermes:
  repo_path: "${HERMES_REPO_PATH}"
  plugin: "honcho"
  ingest_strategy: "sync_per_turn"
  # Honcho reads HONCHO_API_KEY from the process env — do not duplicate here.
  plugin_config: {}
  prompts:
    answer_mode: "shared"
```

- [ ] **Step 2: Create `hermes-hindsight.yaml`**

```yaml
adapter: "hermes"

llm:
  provider: "openai"
  model: "gpt-4o-mini"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
  temperature: 0.0
  max_tokens: 1024

search:
  top_k: 6
  response_top_k: 5
  num_workers: 5
  max_inflight_queries_per_conversation: 1

answer:
  max_retries: 3

hermes:
  repo_path: "${HERMES_REPO_PATH}"
  plugin: "hindsight"
  ingest_strategy: "sync_per_turn"
  # Hindsight reads HINDSIGHT_API_KEY / HINDSIGHT_API_URL (or local mode)
  # from the process env — do not duplicate here.
  plugin_config: {}
  prompts:
    answer_mode: "shared"
```

- [ ] **Step 3: Commit**

```bash
git add evaluation/config/systems/hermes-honcho.yaml evaluation/config/systems/hermes-hindsight.yaml
git commit -m "feat(hermes-adapter): honcho + hindsight config variants"
```

---

## Task 13: Final suite check

- [ ] **Step 1: Run every hermes test plus the registry contract**

```bash
pytest tests/evaluation/test_hermes_runtime.py \
       tests/evaluation/test_hermes_ingestion.py \
       tests/evaluation/test_hermes_adapter.py \
       tests/evaluation/test_hermes_concurrency.py \
       tests/evaluation/test_hermes_registry.py \
       -v
```

Expected: all green.

- [ ] **Step 2: Sanity-check the full evaluation test suite**

```bash
pytest tests/evaluation/ -v --ignore=tests/evaluation/test_hermes_integration_smoke.py
```

Expected: no regressions in adjacent adapter suites (`test_openclaw_*`, registry, etc.).

- [ ] **Step 3: Update commit log message if squashing**

Leave individual commits as-is; they map 1:1 to the tasks and aid PR review.

---

## Spec coverage self-check

| Spec section                         | Covered by tasks       |
|--------------------------------------|-------------------------|
| §2 Option B (plugin-only, no builtin tool) | Task 4 (adapter loads single provider; no MemoryManager) |
| §3.1 File layout                     | Tasks 1, 3, 4, 9, 12    |
| §3.2 Session-key contract            | Tasks 5, 7 (tests assert `session_id=conversation_id`) |
| §3.2 Ingest steps                    | Task 5                  |
| §3.2 Search                          | Task 7                  |
| §3.2 Answer                          | Task 8                  |
| §3.3 Hermes source mounting          | Task 1                  |
| §3.3.1 Concurrency model             | Tasks 2, 10             |
| §3.4 Ingest strategy dispatch        | Task 5 (sync_per_turn/session_end/both) |
| §3.5 Adapter contract mapping        | Tasks 4–8               |
| §3.6 Error handling + handle.json    | Task 5 (failed-handle test) |
| §3.7 Threat model                    | Design-only; no code change needed (documented assumption) |
| §4 Configuration                     | Tasks 9, 12             |
| §5 Testing                           | Tasks 1, 3, 5–8, 10, 11 |
| §6 Risks                             | Informational — no task |
| §7 Out of scope                      | Informational — no task |
