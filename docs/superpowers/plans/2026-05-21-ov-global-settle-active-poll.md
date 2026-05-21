# OV 全局 Settle Active Poll 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前每个 conversation 自己做盲 sleep 的 settle，改成 pipeline 在 Add stage 之后、Search stage 之前调一次 OV server `POST /api/v1/system/wait` 做 active poll。实现真正的"先 ingest 全部完成、再 retrieval"，并且 settle 时长由 OV 内部 queue 状态决定，不再盲等。

**Architecture:**
- adapter 层：BaseAdapter 加 `wait_post_add_settle()` hook (default no-op)；OpenClawAdapter override 实现调 OV `/system/wait`；移除 `_ingest_via_session_bundle` 里 per-conv `settle_sec` 块。
- pipeline 层：`pipeline.py` 在 `post_add_wait_seconds` 之前调 `adapter.wait_post_add_settle()`，确保所有 conv `adapter.add()` 完成后再发起 OV settle，OV settle 完成后再进 Stage 2 Search。
- OV 主仓：**不修改**。`POST /api/v1/system/wait` 后端 `wait_processed()` → `QueueManager.wait_complete()` 已是 active poll (每 0.5s 检查 `is_all_complete`)，覆盖 EMBEDDING/SEMANTIC 等所有 queue，正是我们要的 phase3 vectordb settle 信号。

**Tech Stack:** Python (FastAPI client via aiohttp), pytest + pytest-asyncio (单元测试), bash/docker (smoke3 e2e 验证)。

---

## File Structure

**Modify:**
- `evaluation/src/adapters/base.py` — 加 `wait_post_add_settle` hook
- `evaluation/src/adapters/openclaw/adapter.py` — 实现 hook + 删 per-conv settle
- `evaluation/src/core/pipeline.py` — 调度 hook
- `evaluation/config/systems/openclaw-docker-openviking-session-bundle-noop.yaml` — yaml 配置

**Create:**
- `evaluation/tests/adapters/test_openclaw_post_add_settle.py` — 单元测试
- `evaluation/tests/core/test_pipeline_post_add_settle.py` — pipeline 调度测试

---

## Task 1: BaseAdapter 加 `wait_post_add_settle` hook

**Files:**
- Modify: `evaluation/src/adapters/base.py`

- [ ] **Step 1.1: Write the failing test**

Create `evaluation/tests/adapters/test_base_adapter_hooks.py`:

```python
"""Verify BaseAdapter exposes wait_post_add_settle hook with no-op default."""
import asyncio
import pytest

from evaluation.src.adapters.base import BaseAdapter


class _StubAdapter(BaseAdapter):
    """Minimal adapter that satisfies abstract methods so we can test hooks."""

    async def add(self, conversations, **kwargs):
        return None

    async def search(self, query, conversation_id, index, **kwargs):
        return None


@pytest.mark.asyncio
async def test_wait_post_add_settle_default_is_noop():
    adapter = _StubAdapter(config={})
    result = await adapter.wait_post_add_settle()
    assert result is None
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/parallelism-full-locomo10
PYTHONPATH=. pytest evaluation/tests/adapters/test_base_adapter_hooks.py -v
```

Expected: FAIL with `AttributeError: '_StubAdapter' object has no attribute 'wait_post_add_settle'`

- [ ] **Step 1.3: Add hook to BaseAdapter**

In `evaluation/src/adapters/base.py`, add after `get_answer_timeout` method (around line 127):

```python
    async def wait_post_add_settle(self) -> Any:
        """Optional hook: wait for backend to fully settle after all
        conversations finish ``add()``.

        Default no-op. Adapters that talk to an async backend (e.g. OpenViking
        with phase2 fact-extract + phase3 vectordb/HNSW/dedup) should override
        to issue a server-side "wait until idle" so Stage 2 Search doesn't
        race ingest.

        Called once by Pipeline in the global barrier between Stage 1 Add
        (which is batched over all conversations) and Stage 2 Search. Per-conv
        settle inside ``add()`` cannot guarantee this barrier because
        concurrent conversations on the same backend keep mutating state.

        Returns:
            None when no-op, otherwise an adapter-specific result dict
            (e.g. final queue status) for diagnostics.
        """
        return None
```

- [ ] **Step 1.4: Run test to verify it passes**

```bash
PYTHONPATH=. pytest evaluation/tests/adapters/test_base_adapter_hooks.py -v
```

Expected: PASS

- [ ] **Step 1.5: Commit**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/parallelism-full-locomo10
git add evaluation/src/adapters/base.py evaluation/tests/adapters/test_base_adapter_hooks.py
git commit -m "feat(adapter): add wait_post_add_settle hook to BaseAdapter

Default no-op hook for adapters to declare post-add backend settle.
Pipeline will call this in the global barrier between Stage 1 Add and
Stage 2 Search to let backends like OpenViking (async fact-extract +
vectordb upsert) signal queue idle before retrieval begins."
```

---

## Task 2: OpenClawAdapter 实现 `wait_post_add_settle`

**Files:**
- Modify: `evaluation/src/adapters/openclaw/adapter.py` (add new method)
- Create: `evaluation/tests/adapters/test_openclaw_post_add_settle.py`

- [ ] **Step 2.1: Write the failing test**

Create `evaluation/tests/adapters/test_openclaw_post_add_settle.py`:

```python
"""Verify OpenClawDockerAdapter.wait_post_add_settle posts to OV /system/wait."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import target under test. The adapter class is large; we only need to
# exercise wait_post_add_settle which only depends on cfg + aiohttp.
from evaluation.src.adapters.openclaw.adapter import OpenClawDockerAdapter


def _make_adapter(post_add_settle_cfg):
    """Construct an adapter with minimal config so we can call the hook.

    The full adapter __init__ does heavy work (repo introspection, manifest
    load). For this unit we patch the parts that touch external state and
    set ``config`` directly. The hook only reads ``self.config['ov_ingest']``.
    """
    adapter = OpenClawDockerAdapter.__new__(OpenClawDockerAdapter)
    adapter.config = {
        "ov_ingest": {
            "base_url": "http://oviking.test:1933",
            "api_key_env": "TEST_OV_KEY",
            "account_id": "default",
            "user_id_template": "{conv_id}",
            "post_add_settle": post_add_settle_cfg,
        }
    }
    return adapter


class _FakeResp:
    def __init__(self, status=200, body='{"status":"ok","result":{"embedding":{"processed":42,"error_count":0,"errors":[]}}}'):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


@pytest.mark.asyncio
async def test_wait_post_add_settle_disabled_returns_none():
    adapter = _make_adapter({"enabled": False})
    os.environ["TEST_OV_KEY"] = "k"
    result = await adapter.wait_post_add_settle()
    assert result is None


@pytest.mark.asyncio
async def test_wait_post_add_settle_no_config_returns_none():
    """When post_add_settle key is missing entirely, hook is a no-op."""
    adapter = OpenClawDockerAdapter.__new__(OpenClawDockerAdapter)
    adapter.config = {"ov_ingest": {"base_url": "http://x", "api_key_env": "K"}}
    result = await adapter.wait_post_add_settle()
    assert result is None


@pytest.mark.asyncio
async def test_wait_post_add_settle_posts_to_system_wait():
    adapter = _make_adapter({"enabled": True, "timeout_sec": 1800, "http_buffer_sec": 60})
    os.environ["TEST_OV_KEY"] = "secret-k"

    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=_FakeResp())
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=fake_session):
        result = await adapter.wait_post_add_settle()

    # POST hit /api/v1/system/wait
    fake_session.post.assert_called_once()
    call_args = fake_session.post.call_args
    assert call_args.args[0].endswith("/api/v1/system/wait")
    # Body carries the OV timeout
    assert call_args.kwargs["json"] == {"timeout": 1800.0}
    # Headers carry api key + tenant
    headers = call_args.kwargs["headers"]
    assert headers["X-API-Key"] == "secret-k"
    assert headers["X-OpenViking-Account"] == "default"

    # Returns parsed result for diagnostics
    assert result is not None
    assert "embedding" in result
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/parallelism-full-locomo10
PYTHONPATH=. pytest evaluation/tests/adapters/test_openclaw_post_add_settle.py -v
```

Expected: FAIL (method not implemented)

- [ ] **Step 2.3: Implement `wait_post_add_settle` in OpenClawDockerAdapter**

In `evaluation/src/adapters/openclaw/adapter.py`, add a new method on the adapter class. Place it next to `get_answer_timeout` or near other adapter-level hooks. The exact insertion location must be inside the `OpenClawDockerAdapter` class and outside any other method:

```python
    async def wait_post_add_settle(self) -> Any:
        """Post-add global barrier: ask OV server to wait until all internal
        queues drain.

        Why not per-conv settle (the old ``settle_sec`` inside
        ``_ingest_via_session_bundle``): per-conv sleep starts when *that*
        conv's tasks complete, but concurrent conv ingest keeps pushing
        new work onto the OV server's embedding/semantic queues. Sleeping
        in conv A while conv B's vectordb upserts are still flying does
        not actually let A's retrieval see settled state — the HNSW index
        is rebuilding under A's feet.

        OV server side: ``POST /api/v1/system/wait`` invokes
        ``QueueManager.wait_complete()`` which polls ``is_all_complete``
        every 0.5s across all registered queues (EMBEDDING, SEMANTIC, ...).
        That's a server-internal active poll, not a fixed sleep. We block
        on this single HTTP call from the pipeline's global barrier, so
        the barrier semantic becomes: "all conv add() returned + OV's own
        worker queues drained" before any conv runs Stage 2 Search.

        Returns the queue status dict for diagnostics, or None when
        disabled.
        """
        import aiohttp
        import os

        ov_cfg = (self.config or {}).get("ov_ingest") or {}
        settle_cfg = ov_cfg.get("post_add_settle") or {}
        if not settle_cfg.get("enabled"):
            return None

        base_url = (ov_cfg.get("base_url") or "").rstrip("/")
        if not base_url:
            return None

        ov_timeout = float(settle_cfg.get("timeout_sec") or 1800.0)
        # Outer HTTP timeout must exceed the OV server's wait timeout —
        # otherwise the client aborts before OV finishes. http_buffer_sec
        # gives the request a generous margin (default 60s).
        http_buffer = float(settle_cfg.get("http_buffer_sec") or 60.0)
        http_timeout_sec = ov_timeout + http_buffer

        api_key = ""
        api_key_env = ov_cfg.get("api_key_env")
        if api_key_env:
            api_key = os.environ.get(api_key_env, "") or ""

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        account_id = ov_cfg.get("account_id") or "default"
        headers["X-OpenViking-Account"] = account_id
        # /system/wait is global across users — pick any user header so the
        # request passes auth. The OV-side wait_processed touches QueueManager
        # directly (not user-scoped), so this header is purely about getting
        # past resolve_identity.
        headers["X-OpenViking-User"] = "default"

        timeout = aiohttp.ClientTimeout(total=http_timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                f"{base_url}/api/v1/system/wait",
                headers=headers,
                json={"timeout": ov_timeout},
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.warning(
                        "OV /system/wait HTTP %d: %s",
                        resp.status, body[:300],
                    )
                    return None
                import json as _json
                try:
                    parsed = _json.loads(body)
                except _json.JSONDecodeError:
                    logger.warning("OV /system/wait non-JSON body: %s", body[:300])
                    return None
                if isinstance(parsed, dict) and "result" in parsed:
                    return parsed.get("result")
                return parsed
```

- [ ] **Step 2.4: Run test to verify it passes**

```bash
PYTHONPATH=. pytest evaluation/tests/adapters/test_openclaw_post_add_settle.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 2.5: Commit**

```bash
git add evaluation/src/adapters/openclaw/adapter.py evaluation/tests/adapters/test_openclaw_post_add_settle.py
git commit -m "feat(openclaw): implement wait_post_add_settle calling OV /system/wait

POST /api/v1/system/wait drives OV QueueManager.wait_complete() which
polls all internal queues (embedding/semantic) every 500ms until idle.
Server-side active poll replaces the per-conv blind sleep that couldn't
hold a global barrier across concurrent ingest."
```

---

## Task 3: 删除 `_ingest_via_session_bundle` 里 per-conv `settle_sec` 块

**Files:**
- Modify: `evaluation/src/adapters/openclaw/adapter.py:1353-1375`

- [ ] **Step 3.1: Read current settle block to confirm exact location**

```bash
sed -n '1344,1380p' evaluation/src/adapters/openclaw/adapter.py
```

Expected: see the `Phase 1 实验` comment block + `settle_sec = float(cfg.get("settle_sec") or 0)` + `if settle_sec > 0:` block ending with `ov_ingest_settle_end` event.

- [ ] **Step 3.2: Delete the entire per-conv settle block**

Use Edit on `evaluation/src/adapters/openclaw/adapter.py`. Find this exact block (after `ov_sdk_ingest_complete` event emit):

```python
        # Phase 1 实验: ingest task=completed 是 OV server 最早的事件,离
        # "retrieval 真正能稳定 hit" 还差: 写 .md → vectordb embedding → HNSW
        # upsert / entity dedup。client 没有 settle 完成信号,只能盲等。
        # smoke3-t1800 实验显示 completed 数 12→31 但 final_context_tokens
        # 反而 -4063 → acc 47% vs baseline 52.5%,根因就是 Stage 3 提前开始
        # 时 OV server 还在 settle 中,HNSW 重建把老索引也搅乱。给一个固定
        # settle window,跑完 sleep 再让 Stage 3 接手。
        settle_sec = float(cfg.get("settle_sec") or 0)
        if settle_sec > 0:
            logger.info(
                "OV ingest settle wait %.0fs for %s (completed=%d failed=%d)",
                settle_sec, conv_id, completed, failed,
            )
            self._append_events(sandbox, [{
                "event": "ov_ingest_settle_start",
                "conversation_id": conv_id,
                "settle_sec": settle_sec,
            }])
            await asyncio.sleep(settle_sec)
            self._append_events(sandbox, [{
                "event": "ov_ingest_settle_end",
                "conversation_id": conv_id,
            }])

```

Replace with (single blank line, preserving spacing between this and next method):

```python

```

- [ ] **Step 3.3: Run existing adapter tests to make sure nothing broke**

```bash
PYTHONPATH=. pytest evaluation/tests/adapters/ -v -x -k "openclaw and not docker_e2e" 2>&1 | head -80
```

Expected: PASS — no test exercised the deleted per-conv settle directly. If anything fails, investigate before continuing.

- [ ] **Step 3.4: Commit**

```bash
git add evaluation/src/adapters/openclaw/adapter.py
git commit -m "refactor(openclaw): remove per-conv settle_sec, defer to global barrier

Per-conv sleep cannot enforce a global barrier — while conv A slept,
conv B's parallel ingest kept mutating OV's embedding/HNSW state.
Moved to pipeline-level wait_post_add_settle (Task 2) which runs once
after all conv finish add()."
```

---

## Task 4: pipeline.py 在 Add 之后调用 `wait_post_add_settle`

**Files:**
- Modify: `evaluation/src/core/pipeline.py:301-338`

- [ ] **Step 4.1: Write the failing test**

Create `evaluation/tests/core/test_pipeline_post_add_settle.py`:

```python
"""Verify Pipeline calls adapter.wait_post_add_settle() after Stage 1 Add."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.src.core.pipeline import Pipeline


@pytest.mark.asyncio
async def test_pipeline_calls_wait_post_add_settle_when_add_just_completed(tmp_path: Path):
    """When the add stage just ran AND search is in stages, hook is invoked once."""
    adapter = MagicMock()
    adapter.config = {}
    adapter.get_system_info.return_value = {"name": "stub"}
    adapter.build_lazy_index.return_value = None
    adapter.wait_post_add_settle = AsyncMock(return_value={"embedding": {"processed": 10, "error_count": 0, "errors": []}})

    # Stub stages: add returns immediately, search/answer/evaluate noop
    from evaluation.src.core import pipeline as pmod

    pmod.run_add_stage = AsyncMock(return_value={"index": None})
    pmod.run_search_stage = AsyncMock(return_value=[])
    pmod.run_answer_stage = AsyncMock(return_value=[])
    pmod.run_evaluate_stage = AsyncMock(return_value=MagicMock(accuracy=0.0, total_questions=0, correct=0, detailed_results=[], metadata={}))

    pipeline = Pipeline(
        adapter=adapter,
        evaluator=MagicMock(),
        llm_provider=MagicMock(),
        output_dir=tmp_path,
        use_checkpoint=False,
    )

    from evaluation.src.core.data_models import Dataset, Conversation
    dataset = Dataset(
        dataset_name="t",
        conversations=[Conversation(conversation_id="c0", messages=[], speakers=[], metadata={})],
        qa_pairs=[],
        metadata={},
    )

    await pipeline.run(dataset=dataset, stages=["add", "search"])

    # Hook called exactly once because add just completed and search is requested
    adapter.wait_post_add_settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_skips_settle_when_only_add_stage(tmp_path: Path):
    """When search is NOT in stages, no need to wait — saves time on add-only runs."""
    adapter = MagicMock()
    adapter.config = {}
    adapter.get_system_info.return_value = {"name": "stub"}
    adapter.build_lazy_index.return_value = None
    adapter.wait_post_add_settle = AsyncMock(return_value=None)

    from evaluation.src.core import pipeline as pmod
    pmod.run_add_stage = AsyncMock(return_value={"index": None})

    pipeline = Pipeline(
        adapter=adapter,
        evaluator=MagicMock(),
        llm_provider=MagicMock(),
        output_dir=tmp_path,
        use_checkpoint=False,
    )

    from evaluation.src.core.data_models import Dataset, Conversation
    dataset = Dataset(
        dataset_name="t",
        conversations=[Conversation(conversation_id="c0", messages=[], speakers=[], metadata={})],
        qa_pairs=[],
        metadata={},
    )

    await pipeline.run(dataset=dataset, stages=["add"])

    adapter.wait_post_add_settle.assert_not_awaited()
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
PYTHONPATH=. pytest evaluation/tests/core/test_pipeline_post_add_settle.py -v
```

Expected: FAIL — pipeline doesn't yet call the hook.

- [ ] **Step 4.3: Add hook call in pipeline.py**

In `evaluation/src/core/pipeline.py`, locate the `# Post-Add Wait` block at lines 301-338. Add the new global-settle call **before** the existing `post_add_wait_seconds` block. The full replacement for line ~301 (`# Post-Add Wait: for online API systems, wait for backend indexing to complete`) through the existing block end (~line 338) is:

```python
        # Global Post-Add Barrier (Phase 2: ingest-first, retrieve-after):
        # Stage 1 Add is batched over ALL conversations; concurrent ingest
        # finishes its task=completed handshake but the backend's embedding /
        # semantic queues may still be draining. Per-conv settle inside
        # adapter.add() can't hold a barrier across conv. The adapter hook
        # below is called ONCE here, with all conv.add() already returned.
        # Adapters that talk to async backends (OpenViking) implement it to
        # block on a server-side "wait until queues idle" call; default no-op
        # for backends that don't need it.
        if add_just_completed and "search" in stages:
            try:
                settle_result = await self.adapter.wait_post_add_settle()
            except Exception as err:  # noqa: BLE001
                self.logger.warning(
                    "wait_post_add_settle raised (non-fatal): %s", err
                )
                settle_result = None
            if settle_result is not None:
                self.console.print(
                    f"[green]✅ Backend settle complete (queue status: "
                    f"{', '.join(f'{k}={v.get(\"processed\", 0)}' for k, v in settle_result.items() if isinstance(v, dict))})[/green]"
                )
                self.logger.info(
                    "Backend settle returned: %s", settle_result
                )

        # Post-Add Wait: for online API systems, wait for backend indexing to complete
        # Only wait if add just completed
        if add_just_completed:
            wait_seconds = self.adapter.config.get("post_add_wait_seconds", 0)
            if wait_seconds > 0 and "search" in stages:
                self.console.print(
                    f"\n[yellow]⏰ Waiting {wait_seconds}s for backend indexing to complete...[/yellow]"
                )
                self.logger.info(f"⏰ Waiting {wait_seconds}s for backend indexing")

                # Show countdown progress bar
                from rich.progress import (
                    Progress,
                    SpinnerColumn,
                    TextColumn,
                    BarColumn,
                    TimeRemainingColumn,
                )

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.percentage:>3.0f}%"),
                    TimeRemainingColumn(),
                    console=self.console,
                ) as progress:
                    task = progress.add_task(
                        f"⏰ Backend indexing in progress...", total=wait_seconds
                    )
                    for i in range(wait_seconds):
                        time.sleep(1)
                        progress.update(task, advance=1)

                self.console.print(
                    f"[green]✅ Wait completed, ready for search[/green]\n"
                )
                self.logger.info("✅ Post-add wait completed")
```

- [ ] **Step 4.4: Run test to verify it passes**

```bash
PYTHONPATH=. pytest evaluation/tests/core/test_pipeline_post_add_settle.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 4.5: Run broader pipeline test suite to check no regression**

```bash
PYTHONPATH=. pytest evaluation/tests/core/ -v -x 2>&1 | head -80
```

Expected: All existing pipeline tests still pass.

- [ ] **Step 4.6: Commit**

```bash
git add evaluation/src/core/pipeline.py evaluation/tests/core/test_pipeline_post_add_settle.py
git commit -m "feat(pipeline): call adapter.wait_post_add_settle in global barrier

After Stage 1 Add finishes batching all conversations and before any
Stage 2 Search begins, invoke the new adapter hook so backends with
async ingest can block on server-side queue drain. Wired in addition
to (not replacing) the existing post_add_wait_seconds sleep so both
mechanisms can compose."
```

---

## Task 5: yaml 配置改造

**Files:**
- Modify: `evaluation/config/systems/openclaw-docker-openviking-session-bundle-noop.yaml`

- [ ] **Step 5.1: Replace `settle_sec` with `post_add_settle` block**

In `evaluation/config/systems/openclaw-docker-openviking-session-bundle-noop.yaml`, find the lines containing the `settle_sec` config (around lines 101-107):

```yaml
    # Phase 1 实验: task=completed 之后 OV server 内部还有 write .md →
    # vectordb embedding → HNSW upsert / entity dedup 链需要时间。
    # smoke3-t1800 实测 completed 数 12→31 但 retrieve 到的 context tokens
    # 反而 -4063 → acc -5.4pt,根因就是 Stage 3 提前开始时 OV 还在 settle。
    # 1800s settle 给 OV 足够时间消化 ingest,Stage 3 retrieval 拿到完整索引。
    # 代价: smoke3 总时长 +30min,full10 +30min(per-conv 并发,共享 settle 时间)。
    settle_sec: 1800
```

Replace with:

```yaml
    # Phase 2 (ingest-first, retrieve-after): per-conv settle 移到 pipeline
    # 全局 barrier。POST /api/v1/system/wait 让 OV server 自己 active poll
    # 所有 queue (embedding/semantic) 直到 idle 再返回,比固定 sleep 精确,
    # 且发生在所有 conv.add() 完成之后,真正实现 "ingest 全部结束才 retrieval"。
    # smoke3-t1800-settle1800 per-conv 实测: cat=1 单跳 acc -22pt (因 OV consolidation
    # 抽象化具体 fact 在 conv A settle 期间被并发 conv 干扰)。本配置走全局
    # barrier,期望 cat=1 恢复 + cat=2 多跳保持收益。
    # OV server 端 wait_processed 每 500ms poll is_all_complete; timeout_sec
    # 是 server 端 wait 的最大时长 (超时 OV 会 raise TimeoutError, client 端
    # 我们将其视为非致命 warning 继续往下)。
    post_add_settle:
      enabled: true
      timeout_sec: 1800
      http_buffer_sec: 60
```

- [ ] **Step 5.2: Sanity check — yaml parses**

```bash
python3 -c "
import yaml
with open('evaluation/config/systems/openclaw-docker-openviking-session-bundle-noop.yaml') as f:
    cfg = yaml.safe_load(f)
ov = cfg['openclaw']['ov_ingest']
assert 'settle_sec' not in ov, 'settle_sec should be removed'
assert ov['post_add_settle']['enabled'] is True
assert ov['post_add_settle']['timeout_sec'] == 1800
print('yaml ok')
"
```

Expected: `yaml ok`

- [ ] **Step 5.3: Commit**

```bash
git add evaluation/config/systems/openclaw-docker-openviking-session-bundle-noop.yaml
git commit -m "config(openclaw-ov): switch settle_sec to post_add_settle block

Per-conv blind sleep replaced with pipeline-global active poll via
OV /api/v1/system/wait. Same total wall budget (1800s) but now spent
once across all conv instead of N times per conv."
```

---

## Task 6: 跑 smoke3 e2e 验证

**Files:**
- Run: scripts that already exist (`scripts/round_finish.sh` etc.)

- [ ] **Step 6.1: Archive previous run + reset OV state**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/parallelism-full-locomo10
# round_finish.sh archives current OV state + resets user namespace + vectordb
bash scripts/round_finish.sh
```

Expected: archive .tar.gz file created under archives/, OV state reset.

- [ ] **Step 6.2: Trigger smoke3 run with new config**

Use the existing smoke launch script (the same one used for smoke3-t1800-settle1800). If the script name is `scripts/run_smoke3.sh`:

```bash
nohup bash scripts/run_smoke3.sh > /tmp/smoke3-t1800-global-settle.log 2>&1 &
echo "started pid=$!"
```

If a different script is used, inspect `scripts/` for the most recent smoke3 entrypoint pattern.

- [ ] **Step 6.3: Wait for completion (background poll)**

The smoke3 run usually takes ~50-60min with settle. Poll the log for the eval Stage 4 final accuracy line, or just wait for the script's exit notification (background job will be signaled).

- [ ] **Step 6.4: Verify barrier semantics in events.jsonl**

```bash
# Pick any conv's events.jsonl from the latest run
LATEST=$(ls -td evaluation/results/*smoke3*/ | head -1)
EVENTS=$(find "$LATEST" -name events.jsonl | head -1)

# 1. Confirm the old per-conv settle events are GONE
grep -c "ov_ingest_settle_start\|ov_ingest_settle_end" "$EVENTS" || echo "0 (expected)"
```

Expected: `0 (expected)` — no per-conv settle event.

- [ ] **Step 6.5: Verify global settle observed in pipeline.log**

```bash
grep -E "Backend settle returned|wait_post_add_settle" "$LATEST/pipeline.log" | head
```

Expected: at least one line showing `Backend settle returned: {...embedding...}`.

- [ ] **Step 6.6: Compare accuracy vs baseline + previous settle1800 run**

```bash
LATEST=$(ls -td evaluation/results/*smoke3*/ | head -1)
cat "$LATEST/benchmark_summary.json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print('acc =', data.get('eval_result', {}).get('accuracy', 'n/a'))
print('per-cat:')
for k, v in (data.get('per_category') or {}).items():
    print(f'  cat={k}: acc={v.get(\"accuracy\", \"n/a\")}, n={v.get(\"n\", \"?\")}')"
```

Expected: cat=1 acc recovers (baseline ~52%); cat=2 retains gains from settle1800 (>8%).

- [ ] **Step 6.7: Final commit (notes only)**

If a docs note is warranted, append to `docs/design/2026-05-21-phase2-global-settle.md` (create) summarizing the smoke3 result. Otherwise no commit needed for this step.

---

## Self-Review Checklist

- **Spec coverage:** Tasks 1-5 cover (1) hook interface, (2) impl, (3) remove old path, (4) pipeline wire-in, (5) yaml. Task 6 is e2e verification.
- **Placeholder scan:** All code blocks are concrete. No "TBD" / "appropriate". Test assertions specify exact endpoint paths and JSON keys.
- **Type consistency:** Hook signature `async def wait_post_add_settle(self) -> Any` matches across BaseAdapter, OpenClawAdapter, and pipeline caller. Config key `post_add_settle.{enabled, timeout_sec, http_buffer_sec}` is the same in yaml, adapter impl, and tests.
- **OV main repo:** zero modifications — uses only the already-shipped `POST /api/v1/system/wait`.
