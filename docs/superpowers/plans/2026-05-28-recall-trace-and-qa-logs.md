# Recall Trace + QA Logs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"评测跑完后看到某题答错 → 5 分钟内拿到全链路日志报告"成为肌肉记忆。两手抓:(A)给 OV 召回链路加结构化 trace 让未来 log 可机读;(B)写 `qa_logs` 工具从既有 dataset/eval_results/session jsonl/ov-server.log/.ovdata 反向重建任意一道(或一组)错题的全链路。

**Architecture:**
- Part A — 在 OpenViking-fork (新分支 `feat/recall-trace`,从 main 5b3c68c5 起) 给 server retriever 和 openclaw-plugin 加 trace_id/question_id 串联 + 单题结构化 summary + 可选 candidate breakdown。
- Part B — 在 evermemos worktree (新分支 `feat/qa-logs-tools`,从当前 HEAD 1467534 起) 加 `evaluation/tools/qa_logs/`,默认行为是"扫 latest run 的所有错题逐道生成 6 章节报告",指定 `--qid` 时单题生成。
- 两 Part 互相增强但**互不阻塞**:trace 落地后 `qa_logs` 优先读 trace artifact;trace 没落地时 `qa_logs` 走 grep 启发式。
- **不做**:自动根因码分类、横向跨题对比、批量错题归因汇总——这些后续按需再加,本 plan 只交付"看清一题"。

**Tech Stack:** Python 3.12 (pytest/asyncio/pydantic),TypeScript 5 (vitest),FastAPI,LoCoMo dataset 格式,OV `ovdata` 文件树,Docker 编排。

---

## File Structure

### Part A: OpenViking-fork @ `feat/recall-trace`

| 路径 | 责任 |
|---|---|
| `openviking/observability/recall_trace.py` (new) | `RecallTraceEmitter` — 单题 trace 累积器,LEVEL 1-4 控制;支持落 log 行或 artifact JSONL |
| `openviking/retrieve/hierarchical_retriever.py` (modify) | 在 `retrieve()` 入口/中段/出口 emit `recall_start` / `vector_topk` / `rerank_topk` / `server_summary` |
| `openviking/server/routers/search.py` (modify) | 解析 `X-OV-Trace-Id` / `X-OV-Question-Id` / `X-OV-Conv-Id` headers,初始化 emitter,放入 RequestContext |
| `openviking/server/routers/trace.py` (new) | `POST /api/v1/trace/append` 给 plugin 写 trace 用 |
| `openviking/server/identity.py` (modify) | RequestContext 加 `trace_id` / `qid` / `conv_id` 字段 |
| `tests/observability/test_recall_trace.py` (new) | emitter level filtering、JSONL 序列化、artifact 写入 |
| `tests/api_test/retrieval/test_recall_trace_integration.py` (new) | e2e:发 find 请求带 trace headers,验证 ov-server.log 含 `recall_start` 行 |
| `examples/openclaw-plugin/auto-recall.ts` (modify) | `before_prompt_build` 生成 trace_id;调 `client.find` 时带 trace headers;emit `plugin_summary` 含 picked/injected/skipped |
| `examples/openclaw-plugin/memory-ranking.ts` (modify) | 把 `rankForInjection` 拆成 `computeRankBreakdown()` 返回 `{base, leafBoost, eventBoost, prefBoost, overlapBoost, total}`,LEVEL 3 时 emit |
| `examples/openclaw-plugin/client.ts` (modify) | `find()` 接受 trace headers;新增 `appendTrace(traceId, event)` |
| `examples/openclaw-plugin/index.ts` (modify) | 从 OpenClaw ctx 拿 question_id (env var `OV_CURRENT_QUESTION_ID` 兜底);贯穿到 auto-recall |
| `examples/openclaw-plugin/tests/recall-trace.test.ts` (new) | vitest:plugin_summary 结构、picked vs injected 差集 |

### Part B: evermemos worktree @ `feat/qa-logs-tools`

| 路径 | 责任 |
|---|---|
| `evaluation/src/adapters/openclaw_docker_adapter.py` (modify) | 评测 Stage 3 调 agent_run 时把 `question_id` 通过 docker env `OV_CURRENT_QUESTION_ID` 注入容器;评测结束抓 `docker logs <cid>` 落 `<run>/artifacts/openclaw/<conv>/plugin-stdout.log` |
| `evaluation/tools/__init__.py` (new) | 空文件,标识 package |
| `evaluation/tools/qa_logs/__init__.py` (new) | 暴露 `run_qa_logs(qid, run_name, ...)` 和 `__main__` |
| `evaluation/tools/qa_logs/__main__.py` (new) | `python -m evaluation.tools.qa_logs ...` 入口,转发到 cli.main |
| `evaluation/tools/qa_logs/cli.py` (new) | argparse;`--qid` / `--run-name` 都可选;`--qid` 缺省时扫 eval_results 所有 wrong qid;`--run-name` 缺省时按 mtime 取 latest run;qid normalize(`locomo_7_qa_9` → `locomo_7_qa9`) |
| `evaluation/tools/qa_logs/dataset_loader.py` (new) | LoCoMo dataset 加载,按 qid 抽取 question/golden/evidence/对话原文 |
| `evaluation/tools/qa_logs/eval_results.py` (new) | 解析 `eval_results.json` 拿 generated_answer/judge;支持"列出所有 wrong qid" |
| `evaluation/tools/qa_logs/session_jsonl.py` (new) | 在 session jsonl 中定位 qa 对应的 user message、relevant-memories block、assistant reply |
| `evaluation/tools/qa_logs/ov_log_grep.py` (new) | 按时间窗 + owner_user_id 流式 grep ov-server.log;识别 `Enqueued embedding` / `hierarchical_retriever` / `Telemetry summary` 三类行 |
| `evaluation/tools/qa_logs/ingest_reconstructor.py` (new) | 从 dataset evidence 反查 .ovdata + ov-server.log Enqueued 行,验证 golden 是否入库 |
| `evaluation/tools/qa_logs/recall_reconstructor.py` (new) | 从 ov-server.log recall 窗口拼出 vector_topN + rerank_topN + returned;若 trace artifact 存在则优先读 |
| `evaluation/tools/qa_logs/injection_recon.py` (new) | 从 session jsonl 注入文本 + .ovdata 反查 picked URIs |
| `evaluation/tools/qa_logs/report.py` (new) | 渲染 markdown report 6 章节(题目+证据+ingest+archive+inject+recall+judge) |
| `tests/tools/qa_logs/test_dataset_loader.py` (new) | TDD:对 fixtures dataset 拉 qid 正确 |
| `tests/tools/qa_logs/test_cli.py` (new) | TDD:`--qid` / `--run-name` 缺省时的默认值解析 + qid normalize |
| `tests/tools/qa_logs/test_session_jsonl.py` (new) | TDD:定位 qa user message + 注入解析 |
| `tests/tools/qa_logs/test_ov_log_grep.py` (new) | TDD:固定 fixture log,验证窗口过滤 |
| `tests/tools/qa_logs/test_e2e_qa9.py` (new) | TDD:用 locomo_7_qa9 真实数据,验证 report 6 章节内容完整 |

---

## Part A: OpenViking-fork — Recall Trace 基础设施

### Phase A0: 准备分支

- [ ] **Step 1: 在 OpenViking-fork 签出新分支**

```bash
cd /Data3/shutong.shan/memory/refs/OpenViking-fork
git checkout main
git pull origin main
git checkout -b feat/recall-trace
```

- [ ] **Step 2: 确认 baseline 测试可跑**

```bash
cd /Data3/shutong.shan/memory/refs/OpenViking-fork
.venv/bin/pytest tests/api_test/retrieval/test_find.py -q
```

Expected: 全部 PASS (作为 baseline)

---

### Phase A1: trace headers 串联 + LEVEL 1 server summary

**Files:**
- Modify: `openviking/server/identity.py`
- Modify: `openviking/server/routers/search.py`
- Create: `openviking/observability/recall_trace.py`
- Modify: `openviking/retrieve/hierarchical_retriever.py:92-225`
- Test: `tests/observability/test_recall_trace.py`

---

#### Task A1.1: RequestContext 加 trace 字段

- [ ] **Step 1: 写测试**

Create `tests/server/test_identity_trace_fields.py`:
```python
from openviking.server.identity import RequestContext, Role

def test_request_context_default_trace_fields_none():
    ctx = RequestContext(
        account_id="default", user_id="locomo_7", agent_id="main", role=Role.USER
    )
    assert ctx.trace_id is None
    assert ctx.question_id is None
    assert ctx.conv_id is None

def test_request_context_accepts_trace_fields():
    ctx = RequestContext(
        account_id="default", user_id="locomo_7", agent_id="main", role=Role.USER,
        trace_id="t-123", question_id="locomo_7_qa9", conv_id="locomo_7",
    )
    assert ctx.trace_id == "t-123"
    assert ctx.question_id == "locomo_7_qa9"
    assert ctx.conv_id == "locomo_7"
```

- [ ] **Step 2: 跑测试看 fail**

```bash
.venv/bin/pytest tests/server/test_identity_trace_fields.py -q
```

Expected: FAIL `unexpected keyword argument 'trace_id'`

- [ ] **Step 3: 改 RequestContext**

Modify `openviking/server/identity.py`: 在 `RequestContext` 数据类(pydantic BaseModel 或 dataclass,按现有结构)加三个 Optional 字段:
```python
trace_id: Optional[str] = None
question_id: Optional[str] = None
conv_id: Optional[str] = None
```

- [ ] **Step 4: 跑测试看 pass**

```bash
.venv/bin/pytest tests/server/test_identity_trace_fields.py -q
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add openviking/server/identity.py tests/server/test_identity_trace_fields.py
git commit -m "feat(observability): add trace_id/question_id/conv_id to RequestContext"
```

---

#### Task A1.2: search router 解析 X-OV-* headers

- [ ] **Step 1: 写测试**

Create `tests/api_test/retrieval/test_trace_header_parsing.py`:
```python
import pytest
from fastapi.testclient import TestClient
from openviking.server.app import create_app

@pytest.fixture
def client():
    return TestClient(create_app())

def test_find_accepts_trace_headers(client):
    headers = {
        "X-API-Key": "test-key",
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "locomo_7",
        "X-OpenViking-Agent": "main",
        "X-OV-Trace-Id": "t-abc",
        "X-OV-Question-Id": "locomo_7_qa9",
        "X-OV-Conv-Id": "locomo_7",
    }
    r = client.post("/api/v1/search/find",
                    json={"query": "test", "target_uri": "viking://user/memories", "limit": 1},
                    headers=headers)
    assert r.status_code == 200
```

- [ ] **Step 2: 跑测试看 pass(header 应该自动忽略多余)**

```bash
.venv/bin/pytest tests/api_test/retrieval/test_trace_header_parsing.py -q
```

Expected: PASS (header 多余无碍)

- [ ] **Step 3: 在 `openviking/server/auth.py` 的 `get_request_context` 读 headers,赋给 ctx**

定位 `get_request_context` 函数(应在 server/auth.py 或 server/identity.py),在构造 RequestContext 时加:
```python
trace_id=request.headers.get("X-OV-Trace-Id"),
question_id=request.headers.get("X-OV-Question-Id"),
conv_id=request.headers.get("X-OV-Conv-Id"),
```

- [ ] **Step 4: 改测试断言 ctx 真的被填**

加一个 test 直接调 `get_request_context` mock Request,断言返回的 ctx.trace_id == "t-abc"。

- [ ] **Step 5: 跑测试**

```bash
.venv/bin/pytest tests/api_test/retrieval/test_trace_header_parsing.py tests/server -q
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add openviking/server/auth.py tests/api_test/retrieval/test_trace_header_parsing.py tests/server/
git commit -m "feat(observability): parse X-OV-Trace-Id/Question-Id/Conv-Id headers into RequestContext"
```

---

#### Task A1.3: RecallTraceEmitter 模块

- [ ] **Step 1: 写测试**

Create `tests/observability/test_recall_trace.py`:
```python
import json
from openviking.observability.recall_trace import RecallTraceEmitter, TraceLevel

def test_emitter_level_0_drops_all(caplog):
    em = RecallTraceEmitter(trace_id="t1", qid="q1", conv_id="c1", level=TraceLevel.OFF)
    em.emit("recall_start", {"query": "x"})
    em.emit("vector_topk", {"rank": 0})
    assert "[recall_trace]" not in caplog.text

def test_emitter_level_1_only_emits_start_and_summary(caplog):
    import logging
    caplog.set_level(logging.INFO)
    em = RecallTraceEmitter(trace_id="t1", qid="q1", conv_id="c1", level=TraceLevel.SUMMARY)
    em.emit("recall_start", {"query": "x"})
    em.emit("vector_topk", {"rank": 0})
    em.emit("server_summary", {"returned": 24})
    msgs = [r.message for r in caplog.records if "[recall_trace]" in r.message]
    stages = [json.loads(m.split("[recall_trace]", 1)[1])["stage"] for m in msgs]
    assert stages == ["recall_start", "server_summary"]

def test_emitter_includes_trace_keys(caplog):
    import logging
    caplog.set_level(logging.INFO)
    em = RecallTraceEmitter(trace_id="t-abc", qid="locomo_7_qa9", conv_id="locomo_7",
                            level=TraceLevel.SUMMARY)
    em.emit("recall_start", {"query": "When did?"})
    line = [r.message for r in caplog.records if "[recall_trace]" in r.message][0]
    payload = json.loads(line.split("[recall_trace]", 1)[1])
    assert payload["trace_id"] == "t-abc"
    assert payload["qid"] == "locomo_7_qa9"
    assert payload["conv_id"] == "locomo_7"
    assert payload["stage"] == "recall_start"
    assert payload["query"] == "When did?"
```

- [ ] **Step 2: 跑测试看 fail**

```bash
.venv/bin/pytest tests/observability/test_recall_trace.py -q
```

Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 实现 emitter**

Create `openviking/observability/recall_trace.py`:
```python
"""Structured recall trace emitter.

Emits single-line JSON to logger with stable schema so downstream tools
(qa_logs) can grep by trace_id and reconstruct the recall chain.

Levels:
    OFF (0)     — nothing
    SUMMARY (1) — recall_start + *_summary only (default)
    CANDIDATE (2) — + per-candidate vector_topk and rerank_topk rows
    BREAKDOWN (3) — + rank_breakdown (plugin) and similar
    FULL (4)    — + budget_decision rows
"""
import json
import logging
from enum import IntEnum
from typing import Any, Dict, Optional

logger = logging.getLogger("openviking.observability.recall_trace")


class TraceLevel(IntEnum):
    OFF = 0
    SUMMARY = 1
    CANDIDATE = 2
    BREAKDOWN = 3
    FULL = 4


_STAGE_MIN_LEVEL = {
    "recall_start": TraceLevel.SUMMARY,
    "server_summary": TraceLevel.SUMMARY,
    "plugin_summary": TraceLevel.SUMMARY,
    "vector_topk": TraceLevel.CANDIDATE,
    "rerank_topk": TraceLevel.CANDIDATE,
    "rank_breakdown": TraceLevel.BREAKDOWN,
    "budget_decision": TraceLevel.FULL,
}


class RecallTraceEmitter:
    def __init__(
        self,
        trace_id: Optional[str],
        qid: Optional[str],
        conv_id: Optional[str],
        level: TraceLevel = TraceLevel.SUMMARY,
    ):
        self.trace_id = trace_id or "no-trace"
        self.qid = qid or "unknown"
        self.conv_id = conv_id or "unknown"
        self.level = level

    def emit(self, stage: str, payload: Dict[str, Any]) -> None:
        if self.level == TraceLevel.OFF:
            return
        min_level = _STAGE_MIN_LEVEL.get(stage, TraceLevel.FULL)
        if self.level < min_level:
            return
        record = {
            "trace_id": self.trace_id,
            "qid": self.qid,
            "conv_id": self.conv_id,
            "stage": stage,
            **payload,
        }
        logger.info("[recall_trace] %s", json.dumps(record, ensure_ascii=False, default=str))


def emitter_from_env_and_ctx(ctx) -> RecallTraceEmitter:
    """Factory: read OV_RECALL_TRACE_LEVEL env, build emitter from ctx."""
    import os
    try:
        level = TraceLevel(int(os.environ.get("OV_RECALL_TRACE_LEVEL", "1")))
    except (ValueError, KeyError):
        level = TraceLevel.SUMMARY
    return RecallTraceEmitter(
        trace_id=getattr(ctx, "trace_id", None),
        qid=getattr(ctx, "question_id", None),
        conv_id=getattr(ctx, "conv_id", None),
        level=level,
    )
```

- [ ] **Step 4: 跑测试看 pass**

```bash
.venv/bin/pytest tests/observability/test_recall_trace.py -q
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add openviking/observability/recall_trace.py tests/observability/test_recall_trace.py
git commit -m "feat(observability): add RecallTraceEmitter with level-gated structured JSON emit"
```

---

#### Task A1.4: hierarchical_retriever 接入 emitter,emit recall_start + server_summary

- [ ] **Step 1: 写集成测试**

Create `tests/api_test/retrieval/test_recall_trace_integration.py`:
```python
import json
import logging
import os
import pytest
from fastapi.testclient import TestClient
from openviking.server.app import create_app


def test_find_emits_recall_start_and_server_summary(monkeypatch, caplog):
    monkeypatch.setenv("OV_RECALL_TRACE_LEVEL", "1")
    caplog.set_level(logging.INFO, logger="openviking.observability.recall_trace")
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/search/find",
        json={"query": "deborah father", "target_uri": "viking://user/memories", "limit": 5},
        headers={
            "X-API-Key": "test-key",
            "X-OpenViking-Account": "default",
            "X-OpenViking-User": "locomo_7",
            "X-OpenViking-Agent": "main",
            "X-OV-Trace-Id": "t-int-1",
            "X-OV-Question-Id": "locomo_7_qa9",
            "X-OV-Conv-Id": "locomo_7",
        },
    )
    assert r.status_code == 200
    trace_lines = [
        rec.message for rec in caplog.records if "[recall_trace]" in rec.message
    ]
    stages = []
    for line in trace_lines:
        payload = json.loads(line.split("[recall_trace]", 1)[1])
        assert payload["trace_id"] == "t-int-1"
        assert payload["qid"] == "locomo_7_qa9"
        stages.append(payload["stage"])
    assert "recall_start" in stages
    assert "server_summary" in stages
```

- [ ] **Step 2: 跑测试看 fail**

```bash
.venv/bin/pytest tests/api_test/retrieval/test_recall_trace_integration.py -q
```

Expected: FAIL — `recall_start not in stages`

- [ ] **Step 3: 在 `hierarchical_retriever.py:retrieve()` 入口和返回前接入 emitter**

Modify `openviking/retrieve/hierarchical_retriever.py`,在 `retrieve(...)` 函数第 112-115 行附近(t0 = time.monotonic() 之后)插入:
```python
from openviking.observability.recall_trace import emitter_from_env_and_ctx
trace = emitter_from_env_and_ctx(ctx)
trace.emit("recall_start", {
    "query": query.query[:500],
    "query_chars": len(query.query),
    "target_dirs": target_dirs,
    "limit": limit,
    "threshold": effective_threshold,
    "mode": mode,
})
```

在第 222-227 行(`return QueryResult(...)` 之前)插入:
```python
trace.emit("server_summary", {
    "duration_ms": round((time.monotonic() - t0) * 1000, 2),
    "vector_returned": len(global_results),
    "passed_threshold": len(final),
    "returned_top": [(m.uri, round(m.score, 4)) for m in final[:10]],
    "rerank_used": self._rerank_client is not None and mode == RetrieverMode.THINKING,
})
```

- [ ] **Step 4: 跑测试看 pass**

```bash
.venv/bin/pytest tests/api_test/retrieval/test_recall_trace_integration.py -q
```

Expected: PASS

- [ ] **Step 5: 跑现有 retrieval 套件 — 不能 regress**

```bash
.venv/bin/pytest tests/api_test/retrieval/ -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add openviking/retrieve/hierarchical_retriever.py tests/api_test/retrieval/test_recall_trace_integration.py
git commit -m "feat(retrieve): emit recall_start and server_summary trace events"
```

---

### Phase A2: plugin 端 trace_id 生成 + plugin_summary emit

#### Task A2.1: client.ts 发 trace headers + appendTrace 方法

**Files:**
- Modify: `examples/openclaw-plugin/client.ts:386-430`
- Test: `examples/openclaw-plugin/tests/recall-trace.test.ts`

- [ ] **Step 1: 写 vitest**

Create `examples/openclaw-plugin/tests/recall-trace.test.ts`:
```ts
import { describe, it, expect, vi } from "vitest";
import { OpenVikingClient } from "../client.js";

describe("OpenVikingClient.find with trace headers", () => {
  it("includes X-OV-Trace-Id when traceContext provided", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", result: { memories: [] } }),
      text: async () => "",
    });
    global.fetch = fetchSpy as any;
    const c = new OpenVikingClient({ baseUrl: "http://x", apiKey: "k", isolateUserScopeByAgent: false, isolateAgentScopeByUser: false });
    await c.find("q", { targetUri: "viking://user/memories", limit: 5 }, "main", {
      traceId: "t-1", questionId: "locomo_7_qa9", convId: "locomo_7",
    });
    const [, opts] = fetchSpy.mock.calls[0];
    const headers = opts.headers as Record<string, string>;
    expect(headers["X-OV-Trace-Id"]).toBe("t-1");
    expect(headers["X-OV-Question-Id"]).toBe("locomo_7_qa9");
    expect(headers["X-OV-Conv-Id"]).toBe("locomo_7");
  });
});
```

- [ ] **Step 2: 跑测试看 fail**

```bash
cd /Data3/shutong.shan/memory/refs/OpenViking-fork/examples/openclaw-plugin
npx vitest run tests/recall-trace.test.ts
```

Expected: FAIL `find takes 3 args`

- [ ] **Step 3: 改 client.ts 加可选 traceContext 参数**

Modify `client.ts:find()` 签名加 `traceContext?: { traceId?: string; questionId?: string; convId?: string }`,在 request() 调用前合并 headers:
```ts
const traceHeaders: Record<string, string> = {};
if (traceContext?.traceId) traceHeaders["X-OV-Trace-Id"] = traceContext.traceId;
if (traceContext?.questionId) traceHeaders["X-OV-Question-Id"] = traceContext.questionId;
if (traceContext?.convId) traceHeaders["X-OV-Conv-Id"] = traceContext.convId;
return this.request<FindResult>("/api/v1/search/find", {
  method: "POST",
  body: JSON.stringify(body),
  headers: traceHeaders,
}, agentId);
```

并保证 `request()` merge 外部 headers 到内部生成的 headers。

- [ ] **Step 4: 跑测试 pass**

```bash
npx vitest run tests/recall-trace.test.ts
```

- [ ] **Step 5: Commit**

```bash
git add examples/openclaw-plugin/client.ts examples/openclaw-plugin/tests/recall-trace.test.ts
git commit -m "feat(plugin): client.find accepts trace context to propagate X-OV-* headers"
```

---

#### Task A2.2: auto-recall.ts 生成 trace_id + emit plugin_summary

**Files:**
- Modify: `examples/openclaw-plugin/auto-recall.ts:160-260`
- Test: append to `examples/openclaw-plugin/tests/recall-trace.test.ts`

- [ ] **Step 1: 写测试**

在 `tests/recall-trace.test.ts` 追加:
```ts
import { buildAutoRecallContext } from "../auto-recall.js";
import { randomUUID } from "node:crypto";

describe("buildAutoRecallContext plugin_summary", () => {
  it("emits picked vs injected vs skipped_by_budget", async () => {
    const verbose = vi.fn();
    const fakeClient = {
      find: vi.fn().mockResolvedValue({
        memories: [
          { uri: "u/a", abstract: "Aaa", level: 2, score: 0.95, category: "preferences" },
          { uri: "u/b", abstract: "Bbb", level: 2, score: 0.71, category: "events" },
        ],
        resources: [],
      }),
      read: vi.fn(async (u: string) => `full content for ${u}`.repeat(50)),
    };
    const cfg = {
      autoRecall: true, recallLimit: 2, recallScoreThreshold: 0.1,
      recallMaxInjectedChars: 200, recallPreferAbstract: false,
      recallResources: false, baseUrl: "http://x",
    } as any;
    await buildAutoRecallContext({
      cfg, client: fakeClient as any, agentId: "main",
      queryText: "When did Deborah's father pass away?",
      logger: { warn: () => {} } as any, verbose,
      traceContext: { traceId: "t1", questionId: "qa9", convId: "c7" },
    });
    const summaryCalls = verbose.mock.calls
      .map((c) => c[0])
      .filter((s) => typeof s === "string" && s.includes("plugin_summary"));
    expect(summaryCalls.length).toBe(1);
    const payload = JSON.parse(summaryCalls[0].split("plugin_summary ", 1)[1]);
    expect(payload.trace_id).toBe("t1");
    expect(payload.qid).toBe("qa9");
    expect(Array.isArray(payload.picked_uris)).toBe(true);
    expect(Array.isArray(payload.injected_uris)).toBe(true);
    expect(Array.isArray(payload.skipped_by_budget)).toBe(true);
  });
});
```

- [ ] **Step 2: 跑测试 fail**

```bash
npx vitest run tests/recall-trace.test.ts
```

- [ ] **Step 3: 改 auto-recall.ts**

(a) `buildAutoRecallContext` 加 `traceContext?: { traceId: string; questionId: string; convId: string }` 参数。
(b) 两个 `client.find(...)` 调用都加第 4 个参数 `traceContext`。
(c) `pickMemoriesForInjection` 返回后,在 `buildMemoryLinesWithBudget` 之前先记下 `pickedUris = memories.map(m => m.uri)`。
(d) `buildMemoryLinesWithBudget` 返回后,从 `memoryLines` 反查 `injectedUris`(line 含 abstract 头,匹配 memories 中的 abstract 前 50 chars)。
(e) `skippedByBudget = pickedUris.filter(u => !injectedUris.includes(u))`。
(f) emit:
```ts
verbose?.(
  `openviking: plugin_summary ${JSON.stringify({
    trace_id: traceContext?.traceId ?? "no-trace",
    qid: traceContext?.questionId ?? "unknown",
    conv_id: traceContext?.convId ?? "unknown",
    candidates_received: allMemories.length,
    after_threshold: processed.length,
    picked_uris: pickedUris,
    injected_uris: injectedUris,
    skipped_by_budget: skippedByBudget,
    total_chars: memoryLines.join("\n").length,
    char_budget: cfg.recallMaxInjectedChars,
  })}`,
);
```

更稳健做法:让 `buildMemoryLinesWithBudget` 改返回 `{ lines, estimatedTokens, accepted: string[], rejected: string[] }`,直接拿到 accepted/rejected URI 列表(避免 abstract 反查)——这是 cleanest 实现。需要更新该函数签名 + 现有调用方。

- [ ] **Step 4: 跑测试 pass + 跑现有 plugin 测试不 regress**

```bash
npx vitest run
```

- [ ] **Step 5: Commit**

```bash
git add examples/openclaw-plugin/auto-recall.ts examples/openclaw-plugin/tests/recall-trace.test.ts
git commit -m "feat(plugin): emit plugin_summary with picked/injected/skipped_by_budget"
```

---

#### Task A2.3: index.ts 从 OpenClaw ctx + env 拿 question_id 传到 auto-recall

**Files:**
- Modify: `examples/openclaw-plugin/index.ts` (定位 before_prompt_build / autoRecall 调用点)

- [ ] **Step 1: 找到 before_prompt_build 或 autoRecall 触发点**

```bash
grep -n "buildAutoRecallContext\|before_prompt_build" examples/openclaw-plugin/index.ts | head -10
```

- [ ] **Step 2: 在调用点生成 traceContext**

在该位置:
```ts
const traceContext = {
  traceId: randomUUID(),
  questionId: process.env.OV_CURRENT_QUESTION_ID ?? ctx.metadata?.question_id ?? "unknown",
  convId: process.env.OV_CURRENT_CONV_ID ?? ctx.metadata?.conv_id ?? session.userId ?? "unknown",
};
const ctxResult = await buildAutoRecallContext({
  cfg, client, agentId: session.agentId, queryText, logger, verbose,
  traceContext,
});
```

`randomUUID` 来自 `import { randomUUID } from "node:crypto";`。

- [ ] **Step 3: smoke 测试 — 起 server 跑一个 dummy find,看 plugin verbose 日志含 trace_id**

跑 OpenViking-fork 的 dev server,用 curl 模拟 plugin 调用(不需要 docker,只需要 server)。

```bash
# Terminal 1
.venv/bin/openviking-server --host 0.0.0.0 --port 1933 &
# Terminal 2  
OV_RECALL_TRACE_LEVEL=1 \
curl -s -X POST http://127.0.0.1:1933/api/v1/search/find \
  -H 'X-API-Key: test' \
  -H 'X-OpenViking-User: locomo_7' \
  -H 'X-OV-Trace-Id: smoke-1' \
  -H 'X-OV-Question-Id: locomo_7_qa9' \
  -H 'X-OV-Conv-Id: locomo_7' \
  -d '{"query":"test","target_uri":"viking://user/memories","limit":3}'
# Verify server log has recall_start + server_summary lines with trace_id=smoke-1
```

- [ ] **Step 4: Commit**

```bash
git add examples/openclaw-plugin/index.ts
git commit -m "feat(plugin): propagate question_id/conv_id from env or ctx to trace headers"
```

---

### Phase A3: LEVEL 2 — server emit vector_topk + rerank_topk

**Files:**
- Modify: `openviking/retrieve/hierarchical_retriever.py:155-205`
- Test: extend `tests/api_test/retrieval/test_recall_trace_integration.py`

- [ ] **Step 1: 写测试**

在已有 integration test 中加 case:
```python
def test_find_level_2_emits_per_candidate(monkeypatch, caplog):
    monkeypatch.setenv("OV_RECALL_TRACE_LEVEL", "2")
    caplog.set_level(logging.INFO, logger="openviking.observability.recall_trace")
    client = TestClient(create_app())
    r = client.post("/api/v1/search/find", json={...},
                    headers={..., "X-OV-Trace-Id": "t2", ...})
    stages = [json.loads(rec.message.split("[recall_trace]", 1)[1])["stage"]
              for rec in caplog.records if "[recall_trace]" in rec.message]
    assert stages.count("vector_topk") > 0
    assert stages.count("rerank_topk") > 0
```

- [ ] **Step 2: 跑测试 fail**

- [ ] **Step 3: 在 `_global_vector_search` 之后(global_results 拿到时)和 `_prepare_initial_candidates` 之后(query_scores 拿到时)emit candidates**

在 `retrieve()` 函数 line 165 附近(DEBUG block 之后)插入:
```python
for i, r in enumerate(global_results):
    trace.emit("vector_topk", {
        "rank": i,
        "uri": r.get("uri", ""),
        "vector_score": round(float(r.get("_score", 0.0)), 4),
        "level": r.get("level", -1),
    })
```

并在 `_prepare_initial_candidates` 返回后,新增 emit:
```python
for i, c in enumerate(sorted(initial_candidates, key=lambda x: x.get("_score", 0), reverse=True)):
    trace.emit("rerank_topk", {
        "rank": i,
        "uri": c.get("uri", ""),
        "rerank_score": round(float(c.get("_score", 0.0)), 4),
        "level": c.get("level", -1),
    })
```

- [ ] **Step 4: 跑测试 pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(retrieve): emit per-candidate vector_topk + rerank_topk at LEVEL 2"
```

---

### Phase A4: LEVEL 3 — plugin emit rank_breakdown

**Files:**
- Modify: `examples/openclaw-plugin/memory-ranking.ts:210-220`

- [ ] **Step 1: 写测试**

```ts
import { rankForInjection, _computeRankBreakdownForTest, buildRecallQueryProfile } from "../memory-ranking.js";

it("computeRankBreakdown returns all boost components", () => {
  const item = {
    uri: "viking://...deborah_father_passed.md",
    abstract: "time: 2023-01-27 ... my dad passed away two days ago...",
    level: 2,
    score: 0.7107,
    category: "events",
  };
  const profile = buildRecallQueryProfile("When did Deborah's father pass away?");
  const bd = _computeRankBreakdownForTest(item, profile);
  expect(bd.base_score).toBeCloseTo(0.7107, 4);
  expect(bd.leaf_boost).toBe(0.12);
  expect(bd.event_boost).toBe(0.1);
  expect(bd.pref_boost).toBe(0);
  expect(bd.overlap_boost).toBeCloseTo(0.2, 2);
  expect(bd.total).toBeCloseTo(1.1307, 3);
});
```

- [ ] **Step 2: 跑测试 fail**

- [ ] **Step 3: 重构 `rankForInjection`**

```ts
export function _computeRankBreakdownForTest(item, profile) {
  const base_score = clampScore(item.score);
  const leaf_boost = isLeafLikeMemory(item) ? 0.12 : 0;
  const event_boost = profile.wantsTemporal && isEventMemory(item) ? 0.1 : 0;
  const pref_boost = profile.wantsPreference && isPreferencesMemory(item) ? 0.08 : 0;
  const abstract = (item.abstract ?? item.overview ?? "").trim();
  const overlap_boost = lexicalOverlapBoost(profile.tokens, `${item.uri} ${abstract}`);
  return { base_score, leaf_boost, event_boost, pref_boost, overlap_boost,
           total: base_score + leaf_boost + event_boost + pref_boost + overlap_boost };
}

function rankForInjection(item, profile) {
  return _computeRankBreakdownForTest(item, profile).total;
}
```

并在 `pickMemoriesForInjection` 排序循环外加 emitter 注入(如 LEVEL >= 3 则对 sorted 24 个 emit `rank_breakdown`),这部分需要 emitter 接口(由 plugin 调用方传入 `emitTrace` 回调)。

为避免 plugin 端引入新依赖,做法:在 `auto-recall.ts` 内拿到 `processed` 之后、调 `pickMemoriesForInjection` 之前,自己用 `_computeRankBreakdownForTest` 计算并 verbose 输出。

- [ ] **Step 4: 跑测试 pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(plugin): expose rank breakdown components; emit at LEVEL 3"
```

---

### Phase A5: trace artifact 文件落地(可选/最后做)

**Files:**
- Create: `openviking/server/routers/trace.py`
- Create: `openviking/observability/trace_writer.py`

- [ ] **Step 1: 实现 `POST /api/v1/trace/append`**

接收 `{trace_id, events: [...]}`,append 到 `${OV_RECALL_TRACE_DIR}/{trace_id}.jsonl`。

- [ ] **Step 2: client.ts 加 `appendTrace(traceId, events)`**

- [ ] **Step 3: plugin emit 时同时 buffer 到本地 array,recall 结束时一次性 appendTrace 上传**

- [ ] **Step 4: Commit + integration smoke**

---

## Part B: evermemos worktree — qa_logs 工具

### Phase B0: 准备分支 + adapter env 注入

- [ ] **Step 1: 在 worktree 签出新分支**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/openviking-local-eval
git checkout -b feat/qa-logs-tools
```

- [ ] **Step 2: 改 `openclaw_docker_adapter.py` — 在 docker run 时把 question_id/conv_id 注入 env**

定位 `run_agent` 或 docker 子进程构造点(grep `docker run|client.containers.run`):
```bash
grep -n "client.containers.run\|docker run\|env\[" evaluation/src/adapters/openclaw_docker_adapter.py | head -10
```

在 env dict 加:
```python
env["OV_CURRENT_QUESTION_ID"] = question_id
env["OV_CURRENT_CONV_ID"] = conv_id
env["OV_RECALL_TRACE_LEVEL"] = os.environ.get("OV_RECALL_TRACE_LEVEL", "1")
```

- [ ] **Step 3: 加 docker logs 捕获**

在 agent_run 容器 detach 后,加 `docker logs -f <cid>` 重定向到 `<run>/artifacts/openclaw/<conv>/plugin-stdout.log`(用 subprocess.Popen 后台),agent_run 结束时 join。

- [ ] **Step 4: smoke 跑 1 conv 验证**

```bash
.venv/bin/python -m evaluation.cli --dataset locomo --system openclaw-docker-openviking-session-bundle-noop --run-name trace-smoke --from-conv 7 --to-conv 8 --limit-qa 3
ls evaluation/results/.../trace-smoke/artifacts/openclaw/locomo_7/plugin-stdout.log
grep "plugin_summary" evaluation/results/.../trace-smoke/artifacts/openclaw/locomo_7/plugin-stdout.log
```

Expected: 3 行 plugin_summary,每行含 trace_id/qid/picked_uris/injected_uris/skipped_by_budget。

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/adapters/openclaw_docker_adapter.py
git commit -m "feat(eval): inject OV_CURRENT_QUESTION_ID into agent docker and capture plugin stdout"
```

---

### Phase B1: qa_logs 框架 + dataset/eval_results 模块

#### Task B1.1: 包结构 + CLI(默认 latest run + all errors,qid normalize)

- [ ] **Step 1: 创建包**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/openviking-local-eval
mkdir -p evaluation/tools/qa_logs tests/tools/qa_logs
touch evaluation/tools/__init__.py
touch evaluation/tools/qa_logs/__init__.py
touch tests/tools/qa_logs/__init__.py
```

- [ ] **Step 2: 写测试 — CLI 默认值 + qid normalize**

Create `tests/tools/qa_logs/test_cli.py`:
```python
from pathlib import Path
from evaluation.tools.qa_logs.cli import parse_args, normalize_qid, resolve_latest_run


def test_normalize_qid_underscore_form_to_canonical():
    # 用户友好输入(带下划线)→ dataset/eval_results 实际格式(无下划线)
    assert normalize_qid("locomo_7_qa_9") == "locomo_7_qa9"
    assert normalize_qid("locomo_7_qa9") == "locomo_7_qa9"  # 兼容直传
    assert normalize_qid("locomo_10_qa_123") == "locomo_10_qa123"

def test_normalize_qid_rejects_bad_form():
    import pytest
    with pytest.raises(ValueError):
        normalize_qid("foo")
    with pytest.raises(ValueError):
        normalize_qid("locomo_7_question_9")

def test_cli_qid_underscore_is_normalized():
    args = parse_args(["--qid", "locomo_7_qa_9", "--run-name", "main-noproxy-c4"])
    assert args.qid == "locomo_7_qa9"
    assert args.run_name == "main-noproxy-c4"
    assert args.qid_mode == "single"

def test_cli_qid_omitted_means_all_errors():
    args = parse_args(["--run-name", "main-noproxy-c4"])
    assert args.qid is None
    assert args.qid_mode == "all_errors"

def test_cli_run_name_omitted_means_latest(tmp_path, monkeypatch):
    # 构造两个 results 目录,一个新一个旧
    results = tmp_path / "evaluation" / "results"
    old = results / "locomo-sys-old"
    new = results / "locomo-sys-fresh"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "eval_results.json").write_text("{}")
    (new / "eval_results.json").write_text("{}")
    import os, time
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    os.utime(new, (time.time(), time.time()))
    monkeypatch.chdir(tmp_path)
    rn = resolve_latest_run(results_root=results)
    assert rn == "fresh"  # 剥 "locomo-sys-" 前缀

def test_cli_run_name_omitted_with_no_results_raises(tmp_path):
    import pytest
    empty = tmp_path / "no_results"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_latest_run(results_root=empty)
```

- [ ] **Step 3: 跑测试看 fail**

```bash
.venv/bin/pytest tests/tools/qa_logs/test_cli.py -q
```

Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 4: 实现 cli.py**

Create `evaluation/tools/qa_logs/cli.py`:
```python
"""CLI for qa_logs: reconstruct full-link log report for a single qa
or all wrong qa of a given run.

Default behavior:
  python -m evaluation.tools.qa_logs           # all wrong qa, latest run
  python -m evaluation.tools.qa_logs --qid locomo_7_qa_9
  python -m evaluation.tools.qa_logs --run-name main-noproxy-c4
"""
import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_SYSTEM = "openclaw-docker-openviking-session-bundle-noop"
DEFAULT_DATASET_PREFIX = "locomo"
_QID_CANONICAL_RE = re.compile(r"^(locomo_\d+)_qa(\d+)$")
_QID_INPUT_RE = re.compile(r"^(locomo_\d+)_qa_?(\d+)$")


def normalize_qid(qid: str) -> str:
    """Accept either user-friendly `locomo_7_qa_9` or canonical `locomo_7_qa9`;
    return canonical form used in dataset and eval_results.json."""
    m = _QID_INPUT_RE.match(qid)
    if not m:
        raise ValueError(
            f"bad qid: {qid!r} (expected locomo_<conv>_qa<idx> or locomo_<conv>_qa_<idx>)"
        )
    return f"{m.group(1)}_qa{m.group(2)}"


def resolve_latest_run(
    results_root: Path,
    dataset_prefix: str = DEFAULT_DATASET_PREFIX,
    system: str = DEFAULT_SYSTEM,
) -> str:
    """Pick latest-mtime run dir under results_root, return its run-name
    (strip `<dataset>-<system>-` prefix)."""
    if not results_root.exists():
        raise FileNotFoundError(f"results root not found: {results_root}")
    candidates = [
        p for p in results_root.iterdir()
        if p.is_dir() and p.name.startswith(f"{dataset_prefix}-{system}-")
    ]
    if not candidates:
        # Fallback: any directory under results
        candidates = [p for p in results_root.iterdir() if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"no run dirs under {results_root}")
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    prefix = f"{dataset_prefix}-{system}-"
    name = latest.name
    return name[len(prefix):] if name.startswith(prefix) else name


@dataclass
class CLIArgs:
    qid: Optional[str]               # canonical (no underscore between qa and idx); None == all errors
    qid_mode: str                    # "single" or "all_errors"
    run_name: str
    system: str
    ov_log: Optional[str]
    ovdata: Optional[str]
    dataset: Optional[str]
    results_root: Path
    out: Optional[str]


def _default_results_root() -> Path:
    # Resolves relative to current working directory; main wrapper can override
    return Path("evaluation/results")


def parse_args(argv: Optional[list[str]] = None) -> CLIArgs:
    p = argparse.ArgumentParser(prog="qa_logs")
    p.add_argument("--qid", default=None,
                   help="qa id, e.g. locomo_7_qa_9. Omit to process all wrong qa of the run.")
    p.add_argument("--run-name", default=None,
                   help="run name suffix. Omit to pick latest by mtime under --results-root.")
    p.add_argument("--system", default=DEFAULT_SYSTEM)
    p.add_argument("--ov-log", default=None,
                   help="path to ov-server.log; defaults to .runlogs/ov-server.log")
    p.add_argument("--ovdata", default=None,
                   help="path to OpenViking-fork .ovdata root")
    p.add_argument("--dataset", default=None,
                   help="path to LoCoMo dataset json; auto-discovered if omitted")
    p.add_argument("--results-root", default=None,
                   help="evaluation/results parent; auto-discovered if omitted")
    p.add_argument("--out", default=None,
                   help="output dir; defaults to reports/qa_logs/<run-name>/<qid>/")
    ns = p.parse_args(argv)

    results_root = Path(ns.results_root) if ns.results_root else _default_results_root()

    qid_canonical = normalize_qid(ns.qid) if ns.qid else None
    qid_mode = "single" if qid_canonical else "all_errors"

    if ns.run_name:
        run_name = ns.run_name
    else:
        run_name = resolve_latest_run(results_root, system=ns.system)

    return CLIArgs(
        qid=qid_canonical, qid_mode=qid_mode,
        run_name=run_name, system=ns.system,
        ov_log=ns.ov_log, ovdata=ns.ovdata, dataset=ns.dataset,
        results_root=results_root, out=ns.out,
    )
```

- [ ] **Step 5: 跑测试 pass**

```bash
.venv/bin/pytest tests/tools/qa_logs/test_cli.py -q
```

Expected: 5 passed

- [ ] **Step 6: 加 `__main__.py`**

Create `evaluation/tools/qa_logs/__main__.py`:
```python
from evaluation.tools.qa_logs.cli import parse_args


def main() -> int:
    args = parse_args()
    print(f"[qa_logs] resolved --run-name={args.run_name} (mode={args.qid_mode}, "
          f"qid={args.qid or 'ALL_ERRORS'})")
    # Phase B3 后,此处分派到 single-qa 或 all-errors-loop
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: smoke 跑 — 默认值能解析到 latest run**

```bash
cd /Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/openviking-local-eval
.venv/bin/python -m evaluation.tools.qa_logs --qid locomo_7_qa_9
# Expected output:
# [qa_logs] resolved --run-name=main-noproxy-c4 (or whichever latest), 
#          mode=single, qid=locomo_7_qa9
```

- [ ] **Step 8: Commit**

```bash
git add evaluation/tools/ tests/tools/qa_logs/
git commit -m "feat(tools): scaffold qa_logs package; CLI defaults to latest-run + all-errors; normalize qid underscore form"
```

---

#### Task B1.2: dataset_loader — 从 LoCoMo JSON 拿 question/golden/evidence

- [ ] **Step 1: 测试**

Create `tests/tools/qa_logs/test_dataset_loader.py`:
```python
import json, pytest
from pathlib import Path
from evaluation.tools.qa_logs.dataset_loader import load_qa, QAFields

@pytest.fixture
def fixture_dataset(tmp_path):
    data = [{
        "sample_id": "locomo_7",
        "qa": [
            {"question": "When did Deborah's father pass away?",
             "answer": "2023-01-25", "category": 3,
             "evidence": ["D2:1"]}
        ],
        "conversation": {"session_2_date_time": "2023-01-27 (Friday)",
                         "session_2": [
                             {"speaker": "Deborah", "text": "My dad passed away two days ago...", "dia_id": "D2:1"}
                         ]},
    }]
    p = tmp_path / "ds.json"
    p.write_text(json.dumps(data))
    return p

def test_load_qa_by_qid(fixture_dataset):
    qa = load_qa(fixture_dataset, qid="locomo_7_qa0")
    assert qa.conv == "locomo_7"
    assert qa.question == "When did Deborah's father pass away?"
    assert qa.golden == "2023-01-25"
    assert qa.category == 3
    assert qa.evidence_turns[0].speaker == "Deborah"
    assert "passed away" in qa.evidence_turns[0].text
```

- [ ] **Step 2: 跑 fail**

- [ ] **Step 3: 实现**

Create `evaluation/tools/qa_logs/dataset_loader.py`:
```python
import json
import re
from dataclasses import dataclass
from pathlib import Path

@dataclass
class EvidenceTurn:
    session_idx: int
    dia_id: str
    speaker: str
    text: str
    timestamp: str | None

@dataclass
class QAFields:
    conv: str
    qid: str
    question: str
    golden: str
    category: int
    evidence_turns: list[EvidenceTurn]

_QID_RE = re.compile(r"^(?P<conv>locomo_\d+)_qa(?P<idx>\d+)$")

def load_qa(dataset_path: Path, qid: str) -> QAFields:
    m = _QID_RE.match(qid)
    if not m:
        raise ValueError(f"bad qid: {qid}")
    conv, idx = m.group("conv"), int(m.group("idx"))
    data = json.loads(Path(dataset_path).read_text())
    sample = next(s for s in data if s["sample_id"] == conv)
    qa = sample["qa"][idx]
    evid_turns = _resolve_evidence(sample.get("conversation", {}), qa.get("evidence", []))
    return QAFields(conv=conv, qid=qid,
                    question=qa["question"], golden=str(qa.get("answer", "")),
                    category=int(qa.get("category", -1)),
                    evidence_turns=evid_turns)

def _resolve_evidence(conv_dict, evidence_ids):
    turns = []
    for ev in evidence_ids:
        sess_part, dia_part = ev.split(":", 1) if ":" in ev else (ev, "")
        sess_idx = int(sess_part.lstrip("D"))
        session_turns = conv_dict.get(f"session_{sess_idx}", [])
        session_ts = conv_dict.get(f"session_{sess_idx}_date_time")
        for t in session_turns:
            if t.get("dia_id", "").endswith(f":{dia_part}") or t.get("dia_id") == ev:
                turns.append(EvidenceTurn(
                    session_idx=sess_idx, dia_id=t.get("dia_id", ev),
                    speaker=t.get("speaker", ""), text=t.get("text", ""),
                    timestamp=session_ts,
                ))
    return turns
```

(根据真实 LoCoMo 格式微调,evidence 字段格式可能是不同形式;先按假设实现,在 e2e 测试调通后再迭代。)

- [ ] **Step 4: 跑 pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(qa_logs): dataset_loader extracts question/golden/evidence by qid"
```

---

#### Task B1.3: eval_results loader

- [ ] **Step 1: 测试**

```python
from evaluation.tools.qa_logs.eval_results import load_eval_result, list_wrong_qids
def test_load_eval_result(tmp_path):
    p = tmp_path / "eval_results.json"
    p.write_text(json.dumps({"results": [
        {"question_id": "locomo_7_qa9", "generated_answer": "No info",
         "judge": {"correct": False}, "latency_ms": 9411}
    ]}))
    r = load_eval_result(p, "locomo_7_qa9")
    assert r.generated == "No info"
    assert r.correct is False
    assert r.latency_ms == 9411
```

- [ ] **Step 2-4: 实现 + pass**

```python
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class EvalResult:
    generated: str
    correct: bool
    latency_ms: int | None

def load_eval_result(path: Path, qid: str) -> EvalResult:
    data = json.loads(Path(path).read_text())
    for r in data.get("results", []):
        if r.get("question_id") == qid:
            return EvalResult(
                generated=r.get("generated_answer", ""),
                correct=bool(r.get("judge", {}).get("correct", False)),
                latency_ms=r.get("latency_ms"),
            )
    raise KeyError(qid)

def list_wrong_qids(path: Path) -> list[str]:
    """For default --qid=all_errors mode: return every wrong qid in this run."""
    data = json.loads(Path(path).read_text())
    return [
        r["question_id"]
        for r in data.get("results", [])
        if not bool(r.get("judge", {}).get("correct", False))
    ]
```

测试也加一条:
```python
def test_list_wrong_qids(tmp_path):
    p = tmp_path / "eval_results.json"
    p.write_text(json.dumps({"results": [
        {"question_id": "q1", "judge": {"correct": True}},
        {"question_id": "q2", "judge": {"correct": False}},
        {"question_id": "q3", "judge": {"correct": False}},
    ]}))
    assert list_wrong_qids(p) == ["q2", "q3"]
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(qa_logs): eval_results loader + list_wrong_qids for default all-errors mode"
```

---

### Phase B2: session jsonl + ov-server.log 解析

#### Task B2.1: session_jsonl module

- [ ] **Step 1: 测试 fixture jsonl**

Create `tests/tools/qa_logs/fixtures/sample_session.jsonl.1779869918`:
(用真实 qa9 的几行简化版)

```python
def test_find_user_message_for_qid_returns_text_and_timestamp(fixture_session_jsonl):
    msg = find_user_message(fixture_session_jsonl, qid_idx=9)
    assert "When did Deborah's father" in msg.text
    assert msg.unix_ts_ms == 1779869911196
    assert msg.injected_bullets and len(msg.injected_bullets) == 5
```

- [ ] **Step 2-4: 实现**

Create `evaluation/tools/qa_logs/session_jsonl.py`:
```python
import json
import re
from dataclasses import dataclass
from pathlib import Path

@dataclass
class InjectedBullet:
    abstract_head: str  # first 80 chars
    text: str
    chars: int

@dataclass  
class AgentTurn:
    text: str
    unix_ts_ms: int
    injected_bullets: list[InjectedBullet]
    assistant_text: str | None
    assistant_thinking: str | None

_BULLET_RE = re.compile(r"^- \[\] ", re.MULTILINE)

def find_user_message(jsonl_path: Path, qid_idx: int) -> AgentTurn:
    """Locate qa N-th user turn in the session jsonl."""
    user_count = -1
    user_rec = None
    assistant_rec = None
    with jsonl_path.open() as f:
        recs = [json.loads(l) for l in f if l.strip()]
    for i, r in enumerate(recs):
        if r.get("type") == "message" and r.get("message",{}).get("role") == "user":
            user_count += 1
            if user_count == qid_idx:
                user_rec = r
                for j in range(i+1, len(recs)):
                    if recs[j].get("type") == "message" and recs[j].get("message",{}).get("role") == "assistant":
                        assistant_rec = recs[j]
                        break
                break
    if not user_rec:
        raise IndexError(qid_idx)
    text = user_rec["message"]["content"][0]["text"]
    bullets = _parse_bullets(text)
    ts_ms = user_rec["message"]["timestamp"]
    return AgentTurn(
        text=text, unix_ts_ms=ts_ms,
        injected_bullets=bullets,
        assistant_text=_extract_assistant_text(assistant_rec),
        assistant_thinking=_extract_thinking(assistant_rec),
    )

def _parse_bullets(text: str):
    if "<relevant-memories>" not in text:
        return []
    start = text.index("relevant:\n") + len("relevant:\n")
    end = text.index("\n</relevant-memories>")
    block = text[start:end]
    parts = _BULLET_RE.split(block)
    bullets = []
    for p in parts:
        p = p.strip()
        if not p: continue
        body = p
        bullets.append(InjectedBullet(
            abstract_head=body[:80], text=body, chars=len(body),
        ))
    return bullets

def _extract_assistant_text(rec):
    if not rec: return None
    for c in rec["message"]["content"]:
        if c.get("type") == "text":
            return c.get("text")
    return None

def _extract_thinking(rec):
    if not rec: return None
    for c in rec["message"]["content"]:
        if c.get("type") == "thinking":
            return c.get("thinking")
    return None
```

- [ ] **Step 5: Commit**

---

#### Task B2.2: ov_log_grep — 时间窗 + user 过滤

- [ ] **Step 1: 测试**

用 fixture log,验证只返回时间窗内 + 含 owner_user_id 的行。

- [ ] **Step 2-4: 实现**

```python
import re
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")

@dataclass
class WindowedLine:
    timestamp: datetime
    text: str

def grep_window(ov_log: Path, start_dt: datetime, end_dt: datetime,
                patterns: list[str], user_filter: str | None = None):
    pats = [re.compile(p) for p in patterns]
    with ov_log.open(errors="replace") as f:
        for line in f:
            m = _TS_RE.match(line)
            if not m: continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
            except ValueError:
                continue
            if ts < start_dt: continue
            if ts > end_dt: break
            if user_filter and user_filter not in line: continue
            if any(p.search(line) for p in pats):
                yield WindowedLine(timestamp=ts, text=line.rstrip())
```

- [ ] **Step 5: Commit**

---

### Phase B3: recall + ingest + injection 重建 + 6 章节 report

每个 Task 走完整的 test→fail→implement→pass→commit 五步。下面给数据契约和关键代码骨架,五步同前面 Task 风格,简洁起见这里不重复列。

#### Task B3.1: recall_reconstructor

从 ov-server.log 时间窗 grep 出:
- `[N] URI: ... score: X.XXXX, level: ...` 行 → `vector_topk: list[(rank, uri, score, level)]`
- `Added initial candidate ... (score: ...)` 行 → `rerank_topk: list[(rank, uri, score)]`
- `Telemetry summary ... 'returned': N` → 元信息

如果 `<run>/artifacts/recall_traces/<qid>.jsonl`(Part A5 落地后)存在,优先读 `stage=vector_topk` / `stage=rerank_topk` / `stage=server_summary` 直接得到结构化数据。

数据结构:
```python
@dataclass
class RecallTrace:
    vector_topk: list[CandidateRow]  # CandidateRow = (rank, uri, score, level)
    rerank_topk: list[CandidateRow]
    returned_count: int
    duration_ms: float | None
    source: str  # "trace_artifact" or "log_grep"
```

测试:fixture log 含 5 行 vector + 5 行 rerank,assert 返回的 RecallTrace 字段正确。

Commit: `feat(qa_logs): recall_reconstructor — parse vector_topk + rerank_topk from ov log or trace artifact`

#### Task B3.2: ingest_reconstructor

从 dataset evidence 拿关键短语(每条 evidence turn 的人名 + 前 30 chars 实词),分别:
1. 在 `.ovdata/viking/default/user/<conv>/memories/` 文件树 grep 这些短语,找出**候选 golden 文件** URI 列表
2. 在 ov-server.log 找这些 URI 的 `Enqueued embedding message` 行,确认 level + enqueue 时间 + 是否生成 vector

数据结构:
```python
@dataclass
class GoldenIngestStatus:
    evidence_turn: EvidenceTurn
    candidate_uris: list[str]          # .ovdata grep 命中的 URI
    ingest_records: list[IngestRecord] # ov-server.log Enqueued 行解析
    in_store: bool                     # 至少一个候选 URI 入了 L2 + 有 vector
```

测试:fixture ovdata 含一个含 evidence 短语的 .md,fixture ov-server.log 含其 Enqueued 行,assert `in_store=True`。

Commit: `feat(qa_logs): ingest_reconstructor — verify golden ingestion from ovdata + Enqueued log`

#### Task B3.3: injection_recon

从 session jsonl 的 relevant-memories block 解析出 N 个 bullet 各自的 abstract 头 80 chars,在 `.ovdata` grep 反查到 URI;同时合计字符 + 标出 budget 使用率。若 plugin-stdout.log(Phase B0 落地后)含 `plugin_summary` JSON 行,优先直接读 `picked_uris` / `injected_uris` / `skipped_by_budget`。

数据结构:
```python
@dataclass
class InjectionStatus:
    bullets: list[InjectedBullet]
    injected_uris: list[str]      # 反推得到的 URI
    picked_uris: list[str] | None # 仅当 plugin_summary 可读时
    skipped_by_budget: list[str] | None
    total_chars: int
    char_budget: int              # 从 plugin config 或 4000 默认
    budget_usage_pct: float
```

测试:fixture session jsonl 5 bullets,fixture ovdata 5 文件 abstract 匹配,assert `injected_uris` 含 5 个、`total_chars` ≈ 3992、`budget_usage_pct` ≈ 99.8。

Commit: `feat(qa_logs): injection_recon — map injected bullets back to URIs; surface budget usage`

#### Task B3.4: report renderer(**6 章节**)

按以下章节渲染单题 markdown 报告——**不**做自动根因分类,**不**做横向跨题对比。

```
# qa_logs: locomo_7_qa9

## §1 题目元数据 (dataset)
   - conv / question / golden_answer / category

## §2 Golden 对话原文 (dataset evidence)
   - 列出每条 evidence turn 的 speaker / text / timestamp

## §3 Ingest 阶段 (ovdata + Enqueued)
   - 每条 evidence 是否入库、URI、L 层、enqueue 时间、vector dim

## §4 Archive 阶段 (commit + task)
   - commit_session 时间、task_id、completion 时间、memories_extracted 计数

## §5 评测时 agent 看到什么 (session jsonl)
   - jsonl 文件名 + user message timestamp
   - N 个 bullet 列表 + 各自字符数 + 累计 + budget 使用率
   - 若 plugin_summary 可读,展示 picked_uris vs injected_uris vs skipped_by_budget

## §6 Recall 全过程 + Judge (ov log 时间窗 + eval_results)
   - vector_topk 完整表 + rerank_topk 完整表
   - server returned 24 概要
   - generated_answer + judge correct/wrong (raw,不分类)
```

数据结构:
```python
@dataclass
class Report:
    qa: QAFields
    ingest: list[GoldenIngestStatus]
    archive: ArchiveStatus  # commit/task 信息
    injection: InjectionStatus
    recall: RecallTrace
    eval_result: EvalResult

def render_markdown(report: Report) -> str: ...
```

测试:fixture 全套数据,assert 渲染输出含 6 个 `## §` 标题、含 evidence text、含 vector_topk URI 列表。

Commit: `feat(qa_logs): report renderer — 6-section markdown without auto-classification`

#### Task B3.5: 端到端测试 — qa9 真实数据

```python
import pytest
from pathlib import Path
from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs import run_qa_logs

REAL_RUN = "main-noproxy-c4"
REAL_RESULTS = Path("evaluation/results")

@pytest.mark.skipif(
    not (REAL_RESULTS / f"locomo-openclaw-docker-openviking-session-bundle-noop-{REAL_RUN}").exists(),
    reason="requires real run artifacts; skipped in sandboxed CI",
)
def test_e2e_qa9_report_has_six_sections():
    args = CLIArgs(
        qid="locomo_7_qa9", qid_mode="single", run_name=REAL_RUN,
        system="openclaw-docker-openviking-session-bundle-noop",
        ov_log=None, ovdata=None, dataset=None,
        results_root=REAL_RESULTS, out=None,
    )
    report = run_qa_logs(args)
    md = report.to_markdown()
    for section in ["## §1", "## §2", "## §3", "## §4", "## §5", "## §6"]:
        assert section in md
    # 核心事实断言:agent 实际未注入 golden(从注入文本反推)
    assert "deborah_father_passed" not in {u for u in report.injection.injected_uris}
    # 核心事实断言:ingest 阶段确实把 golden 入了库
    assert any(
        "deborah_father_passed" in u
        for s in report.ingest
        for u in s.candidate_uris
    )
    # judge 是错(原始,不分类)
    assert report.eval_result.correct is False
```

跑成功 → commit:
```bash
git commit -m "feat(qa_logs): e2e test against real locomo_7_qa9 — 6-section report renders correctly"
```

#### Task B3.6: all-errors 分派(默认模式)

`__main__.py` 当 `args.qid_mode == "all_errors"` 时,for 循环跑所有 wrong qid,每题写到 `reports/qa_logs/<run-name>/<qid>/report.md`;跑完打印一行进度("Processed 865/865 wrong qids in 47m")。

```python
# evaluation/tools/qa_logs/__init__.py
def run_qa_logs_all_errors(args: CLIArgs) -> None:
    eval_results_path = args.results_root / f"locomo-{args.system}-{args.run_name}" / "eval_results.json"
    wrong = list_wrong_qids(eval_results_path)
    print(f"[qa_logs] {len(wrong)} wrong qids in run={args.run_name}")
    for i, qid in enumerate(wrong, 1):
        try:
            sub_args = dataclasses.replace(args, qid=qid, qid_mode="single")
            report = run_qa_logs(sub_args)
            out_dir = Path(args.out or f"reports/qa_logs/{args.run_name}") / qid
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.md").write_text(report.to_markdown())
        except Exception as e:
            print(f"[qa_logs] {qid} FAILED: {e}")
        if i % 50 == 0:
            print(f"[qa_logs] {i}/{len(wrong)} done")
```

测试:fixture run 含 3 题 wrong + 1 题 correct;跑 all-errors 后断言 reports 目录有 3 个 qid 子目录。

Commit: `feat(qa_logs): default all-errors mode iterates wrong qids and writes per-qid report.md`

---

## Self-Review (执行前我自己过一遍)

**Spec coverage:**
- [x] OpenViking-fork 新分支 `feat/recall-trace` — Phase A0-A5
- [x] evermemos worktree 新分支 `feat/qa-logs-tools` — Phase B0-B3
- [x] trace 方案落实 — A1 (LEVEL 1) / A3 (LEVEL 2) / A4 (LEVEL 3) / A5 (artifact)
- [x] qa_logs 工具 — B1-B3,默认 latest run + all errors;qid 接受 `locomo_7_qa_9` normalize 形式
- [x] **不做**:自动根因码分类、横向对比、批量汇总——已从 plan 移除
- [x] 深度思考新维度 — 见下面 "Further Investigation"

**Placeholder scan:**
- Phase A2.1 / A2.2 / A2.3 部分 step 引用了 "see step N" 风格但都列了完整代码 — OK
- Phase A4 step 3 的代码引用了 `_computeRankBreakdownForTest`,导出 `_` 前缀强调是 test helper — OK
- Phase B3 的 Task B3.1 / B3.2 / B3.3 给的是数据契约 + 关键代码骨架而非五步 step,因为它们都是直筒型模块(load → parse → return),五步对照前面 Task B1.x / B2.x 同模板套用即可。如果实施时一个 task 卡住,执行者按 test→fail→implement→pass→commit 重复展开。
- Phase B3.4(report renderer) / B3.5(e2e test) / B3.6(all-errors 分派) 给了完整代码,可直接执行。

**Type consistency:**
- TraceLevel enum 名一致(OFF/SUMMARY/CANDIDATE/BREAKDOWN/FULL)
- traceContext: { traceId; questionId; convId } 在 client.ts / auto-recall.ts / index.ts 一致
- CLIArgs 字段(qid/qid_mode/run_name/system/...)在 cli.py / __main__.py / __init__.py 一致
- QAFields / RecallTrace / GoldenIngestStatus / InjectionStatus / Report 跨 dataset_loader / recall_reconstructor / ingest_reconstructor / injection_recon / report.py 一致
- 不再有 rootcause 字段引用(已从 Report 移除)
- qid 在 CLI 接收时 normalize 为 canonical 形式,内部所有模块统一用 canonical(`locomo_7_qa9`),数据集和 `eval_results.json` 也用 canonical

---

## Further Investigation — 第二轮深思,下一个 plan 的种子

执行完上面 Part A+B 后,以下方向还能让"问题定位"更上一层楼,**不在本 plan 范围内**,但值得记录:

### 1. 抽取阶段的不透明 — 现在我们只能看到 `Enqueued embedding`,看不到上游决策

- **LLM extract prompt + raw response 录制**: 在 `openviking/session/memory/memory_updater.py`(或抽取调用点)加 `OV_LOG_EXTRACT_LLM=1` 开关,把发给 LLM 的 prompt 和返回结果落到 `extract_traces/<conv>_<session_idx>.jsonl`。
  
  解决:R1_extract_miss 现在完全靠"事后从 .ovdata 缺啥反推",有了 prompt+response 就能看到"是 LLM 抽漏了 还是 OV upstream 截断了"。

- **抽取覆盖率检查**: 写 `evaluation/tools/ingest_audit.py`,evaluation 结束时遍历每个 conv 的 evidence 文档,验证是否每条 evidence 都至少对应一个 OV 节点。任何 R1 miss 立即报警。

### 2. Rerank cross-encoder 输入审查

`rerank_batch(query, documents)` 的 documents 实际就是 abstract 字符串。问题是:`comfort_activities.md` 的 abstract 里完全没有"父亲"、"死亡"、"日期"信号,为什么 rerank 给它 0.9688? 

- 加 `OV_LOG_RERANK_INPUT=1`,把每次 rerank 的 (query, doc[i], score[i]) tuple 落 `rerank_traces/<trace_id>.jsonl`。
- 离线脚本 `analyze_rerank_decisions.py`: 找"高分但语义不相关"的 (query, doc) pair,作为 cross-encoder 微调数据集。

### 3. Hybrid retrieval 探针

OV server 代码里有 `sparse_query_vector`,说明支持 sparse(BM25 或 BGE-M3 sparse vector)。如果开启:

- "father" 字面会通过 sparse 通道命中 father_passed.md (URI 含 "father")
- "passed away" 在 abstract 字面命中
- 不再依赖 dense embedding 的 "dad"/"father" 语义近似

加 trace 输出 `sparse_score` / `dense_score` / `fusion_score`,A/B 测试 dense-only vs hybrid 对 qa9 这类题的命中。

### 4. Query expansion

`"When did Deborah's father pass away?"` 扩展成 `["When did Deborah's father pass away", "Deborah father death date", "Deborah dad died when"]`,三个变体并行 find,按 RRF 融合 top-N。

- plugin 端开一个 `cfg.queryExpansionMode`,verbose 记录每个变体的 top-N。
- 离线脚本看:对哪些 R2 miss 题,扩展后能命中 golden?

### 5. Budget 维度全量统计

- 收集所有题的 `total_chars / char_budget` 比值分布。
- 看是否大量题卡在 95%+ budget 区间(就如 qa9 卡 99.8%)。
- 决策:把 `recallMaxInjectedChars` 从 4000 提到 8000 / 12000,代价是 prompt 长度增加。需要离线模拟"如果 budget 翻倍,有多少 R2 题被救",权衡 prompt cost vs accuracy gain。

### 6. Entity 杂烩节点单独治理

qa9 的 1956 chars 注入第 5 个 bullet 是 `entities/person/deborah.md`,这种含多个 "Updated as of …" 的杂烩节点是 budget 杀手。两个方向:

- **抽取侧**: 不要把所有 Updated 都塞进单一 entity 文件,按 update 时间窗拆多个 entity snapshots。
- **召回侧**: 给 entity 节点单独的 budget cap(比如 entity 最多占 30% budget)。

写 `entity_size_distribution.py` 看全 dataset entity 节点的字符分布。

### 7. Recall stability 探针

同一文档 `deborah_father_passed.md` 在不同 query 拿到 0.4440 / 0.3526 / 0.3570 / 0.4031 / 0.3719 的 rerank 分。如果对一个 fixed corpus 跑 100 个不同 query,rerank 分波动幅度多大?

- 写 `rerank_stability_probe.py`,固定 corpus,随机 sample queries,统计同一 doc 的 rerank 分 std。波动太大说明 cross-encoder 模型不够稳。

### 8. 错题集语义聚类

把所有 R2 miss 题的 query embedding 聚类(K-means / HDBSCAN),看是否所有 R2 miss 都集中在某些语义簇(比如"询问具体日期" vs "询问人物关系")。

帮助定向制定"针对哪类 query 改进 rerank"。

### 9. 跨 run 的 comparative replay

`diff_qa_logs.py --run-a main-noproxy-c4 --run-b hybrid-retrieval-test --qid locomo_7_qa9`:

输出两个 run 的同一题 6 章节 diff:vector top-N 哪些变了、injected URI 集合 diff、judge 结果变化。

这是 "trace 落地后" 最有价值的工具——能直接验证每次优化对错题的影响。

### 10. Trace store schema

所有 trace JSON 长期归档到 SQLite/DuckDB,开放 SQL 查询:

```sql
-- 找所有"golden 被 picked 但被 budget 跳过"的题
SELECT qid FROM plugin_summary 
WHERE skipped_by_budget @> '["father_passed"]'  
  AND char_budget = 4000;

-- 看 entity 节点占注入比例的分布
SELECT qid, char_share(injected_uris, 'entities/person/')
FROM plugin_summary 
ORDER BY 2 DESC LIMIT 20;
```

### 11. 实时观测(评测过程中)

- 评测 Stage 3 每完成 100 题,POST 进度到 `http://eval-dashboard/progress`(本地 Flask app):当前 (correct/total)、当前根因分布、当前 P95 latency。
- 加 `tail -f` 友好的 progress.jsonl,可视化用 Grafana 或简单 `lnav`。

### 12. Memory hotness 可观察性

`hotness_alpha=0` 默认关闭,但代码里写好了。如果未来开启:

- trace 输出 `semantic_score` / `hotness_score` / `blended_score` 三件套。
- 看是否高 active_count 节点过度 boost 导致"老节点反复出现"。

### 13. Sub-session 时间过滤接通

代码里 `search.py:61-93` 支持 `since/until/time_field`,但 plugin `client.find` 没传。

- 给 plugin 加 `inferTimeFilterFromQuery(query)`:识别 "yesterday" / "last week" / "2023-01" 等时间表达式,设置 since/until。
- trace 输出 `time_filter_applied` 字段。
- R5 时间锚定类(28 题)直接受益。

### 14. plugin "ov_archive_expand" 路径的可观察性

memory_v0 中提到 `agent_local 证据在 artifacts 不在 search_results`,以及 `ov_archive_expand 剥日期`。这条回退路径的可观察性如何?加 trace,确认是否被触发、剥了什么。

### 15. 单 conv 完整 ingestion replay

写 `ingest_replay.py --conv locomo_7 --dry-run`,模拟 ingest 但不真的写 vector,输出"哪些对话被抽取成哪些节点,产出 N 个 events/preferences/entities"——能在改抽取 prompt 后快速看效果,不用全跑 evaluation。

---

## Plan complete

Plan complete and saved to `docs/superpowers/plans/2026-05-28-recall-trace-and-qa-logs.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
