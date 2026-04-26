# Memory Evaluation Framework 设计方案

> **版本**：v0.7 · 2026-04-24
> **Target reader**：在这个 repo 工作的工程师（evermemos + openclaw 双栈）
> **预计实施窗口**：5-7 周，分三阶段增量交付
> **D1 实测状态**：5 个 Check 全 PASS

---

## Changelog

### v0.7 (2026-04-24) — 吸收 Codex 第六轮 review（落地完整性闭环）
- **[Blocker Fix]** §4.8 + §A.2 — `_bridge_base_payload` **必须包含 `agent_llm_env_vars`** 字段。原因：`openclaw_adapter.py:373-389` 现有 payload 只 6 个字段，没有 env 白名单。v0.6 模板把 `${LLM_API_KEY}` 落盘到 resolved_config 后，openclaw 启动时若子进程 env 里没 `LLM_API_KEY` 直接抛 `MissingEnvVarError`（`env-substitution.ts:113`）。所有 bridge 调用都会失败
- **[Blocker Fix]** §4.4 + §A.2 — `build_lazy_index` **必须**也持久化 `self._sandbox_by_conversation_id`。原因：`pipeline.py:277-289` 的两条 skip-add 路径都只调 `build_lazy_index`，v0.6 只在 `add()` 里持久化 → resume / lazy-index 路径下 `_sandbox_for(conv_id)` 直接 raise
- **[High Fix]** §4.7 + §A.4 — **只对真 secret 用 `*_env` marker**。`base_url` / `easyllm_id` 不是 credential，保留 `${VAR:default}` 形态让 evermemos 展开后落盘。原因：v0.6 把所有字段都改 `*_env` 抹掉了原 yaml 的 default fallback，env 不存在时 openclaw 反而抛 MissingEnvVarError
- **[High Fix]** §A.1 — `extractJsonObject` 改 **line-based candidate + JSON.parse 验证**；`stripAnsi` regex **必须含 `\x1b` ESC 字节**。原因：v0.6 brace matcher 不识 quoted string，reply 含 `{}` 会破坏匹配；ANSI regex 缺 ESC 会误吃合法 `[xx]` 文本
- **[High Fix]** §A.2 — `_prebootstrap_workspace` 改 **retry + 硬 raise**。原因：v0.6 只 log warning，pre-bootstrap 失败时 race 条件原样保留
- **[Risk]** §10.1 新增 R23-R26

### v0.6 — 吸收 Codex 第五轮 review（evermemos 集成层修正）
### v0.5 — D1 smoke 实证修订
### v0.4 — Codex 第三轮 review
### v0.3 — openclaw 源码核查
### v0.2 — Codex 第一轮 review
### v0.1 — 初稿

---

## 0. 摘要（TL;DR）

把各种 memory 系统接入 **openclaw 真实 agent loop** 测试并优化：

- Pipeline orchestrator：evermemos `Pipeline` 不变
- Adapter as docker：每个 LoCoMo conversation 一个独立 openclaw 容器
- Session 粒度：session-id `<conv_id>__<qid>` 按 QA 独立（D1 实证）
- 非侵入 openclaw：`OPENCLAW_EXTENSIONS` build-arg + `plugins.allow` + `registerMemoryCapability`
- Path B 本质改动：answer 走 `openclaw agent --local`；bridge 从 stderr 解析（line-based + schema validate）取 `payloads[0].text`
- **Secret 不落盘**：仅 `apiKey` 用 `*_env` marker → adapter 重建 `${VAR}` 模板；非 secret 字段保留 `${VAR:default}`（v0.7）
- **Bridge env 白名单**：`_bridge_base_payload` 必须含 `agent_llm_env_vars`（v0.7）
- **Sandbox 持久化**：`add()` 和 `build_lazy_index()` 双路径都填 `_sandbox_by_conversation_id`（v0.7）
- **Bootstrap 硬保护**：retry + raise，不容许软失败（v0.7）

三阶段：**Stage 0**（D1 完成；D2-D5 接续）→ **Stage 1**（4-5 周）→ **Stage 2**（4 周+）。

---

## 1. 背景与问题

### 1.1 最终目标

> 把各种 memory（standalone 或 coupled），接入 openclaw agent loop 测试，然后优化效果。

### 1.2 范式选择（v0.4 不变）

### 1.3 evermemos 现有资产 + Path B 差距（v0.7 完整列表）

1. `OpenClawAdapter.answer()` 改用 `openclaw agent --local`
2. Agent LLM config 进 resolved_config（§4.7）
3. QA 间 session 隔离
4. Retrieval metrics 对 skipped 样本 suppress
5. Stage 0 noop 用 `memorySearch.enabled=false`
6. Adapter 持久化 sandbox （`add()` **和 `build_lazy_index()` 都要**，v0.7）
7. Bridge 从 stderr 反向解析 + schema validate
8. Plugin allow + slots 配置
9. **Secret 不落盘**：仅真 secret 走 `*_env` marker；非 secret 保留 `${VAR:default}`（v0.7）
10. Adapter `get_answer_timeout()` + answer_stage 接受 override
11. add() pre-bootstrap dummy agent run（**retry + 硬 raise**，v0.7）
12. **Bridge `_bridge_base_payload` 含 `agent_llm_env_vars`**（v0.7）
13. **stripAnsi regex 含 ESC 字节**（v0.7）
14. **JSON 解析 line-based + JSON.parse 验证**（v0.7）

### 1.4 / 1.5（v0.4 不变）

---

## 2. 设计原则（v0.4 不变）

---

## 3. 整体架构（v0.5 不变）

---

## 4. 组件详细设计

### 4.1 Docker Image Family（v0.4 不变）

### 4.2 Openclaw 最小配置（v0.5 不变）

### 4.3 Memory Plugin 集成层（v0.4 不变）

### 4.4 DockerizedOpenclawAdapter（v0.7 加 build_lazy_index sandbox 持久化）

**v0.7 新增**：双路径 sandbox 持久化。

```python
class OpenClawAdapter(BaseAdapter):
    def __init__(self, config: dict, output_dir=None):
        super().__init__(config)
        # ...
        self._sandbox_by_conversation_id: dict[str, dict] = {}

    async def add(self, conversations, ...):
        # ... 现有逻辑 ...
        for conv in conversations:
            sandbox = self._prepare_conversation_sandbox(...)
            # ingest + flush + (v0.6) prebootstrap
            self._sandbox_by_conversation_id[conv.conversation_id] = sandbox

    def build_lazy_index(self, conversations, output_dir):
        """v0.7: 与 add() 平行,从磁盘 handle.json 重建 sandbox map.
        Pipeline 走 skip-add 或 resume 路径时由它填充。"""
        root_dir = self._locate_existing_run_root(Path(output_dir))
        handles: dict[str, dict] = {}
        for conv in conversations:
            handle_path = root_dir / "conversations" / conv.conversation_id / "handle.json"
            if not handle_path.exists():
                continue
            handle = json.loads(handle_path.read_text())
            if handle.get("run_status") != "ready":
                continue
            mode = handle.get("visibility_mode")
            state = handle.get("visibility_state")
            if mode == "settled" and state != "settled":
                continue
            if mode != "settled" and state not in ("indexed", "settled"):
                continue
            handles[conv.conversation_id] = handle
            # v0.7 关键: 持久化到 sandbox map
            self._sandbox_by_conversation_id[conv.conversation_id] = handle
        return {
            "type": "openclaw_sandboxes",
            "run_id": root_dir.name,
            "root_dir": str(root_dir),
            "conversations": handles,
        }

    def _sandbox_for(self, conversation_id: str) -> dict:
        sandbox = self._sandbox_by_conversation_id.get(conversation_id)
        if sandbox is None:
            raise RuntimeError(
                f"No sandbox for conversation_id={conversation_id}. "
                f"add() or build_lazy_index() must run before answer()."
            )
        return sandbox

    # v0.6 / v0.7 加强: retry + 硬 raise
    async def _prebootstrap_workspace(self, sandbox: dict) -> None:
        last_error = None
        for attempt in range(3):
            payload = {
                **self._bridge_base_payload(sandbox),
                "command": "agent_run",
                "session_id": f"{sandbox['conversation_id']}__bootstrap",
                "message": "Reply with: BOOTSTRAP_OK",
                "timeout_seconds": 60,
            }
            try:
                resp = await arun_bridge(self._bridge_script_path(), payload)
                if resp.get("ok"):
                    break
                last_error = resp.get("error", "")
            except Exception as err:
                last_error = str(err)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s
        else:
            raise RuntimeError(
                f"prebootstrap failed for {sandbox['conversation_id']} "
                f"after 3 attempts: {last_error}"
            )

        # v0.7: 硬验证 workspace 文件
        ws = Path(sandbox["workspace_dir"])
        expected = ["AGENTS.md", "SOUL.md", "TOOLS.md"]
        missing = [n for n in expected if not (ws / n).exists()]
        if missing:
            raise RuntimeError(
                f"workspace bootstrap files missing for {sandbox['conversation_id']}: {missing}. "
                f"agent --local did not write expected files."
            )

    def get_answer_timeout(self) -> float:
        agent_t = float(self._openclaw_cfg.get("agent_timeout_seconds", 180))
        return agent_t + 30.0
```

`add()` 中 `_prebootstrap_workspace` 抛出时该 conv 的 `run_status` 标 `failed`，pipeline 后续 stage 跳过此 conv（已有逻辑）。其他 conv 不受影响。

### 4.5 / 4.6（v0.5 不变）

### 4.7 Agent LLM Config Surface（v0.7：仅真 secret 用 marker）

#### Evermemos 系统 yaml（v0.7 修订）

```yaml
adapter: "openclaw"

llm:                                   # 答题/judge LLM (Path A 兼容)
  provider: "openai"
  model: "gpt-4o-mini"
  api_key: "${LLM_API_KEY}"            # 仅 evermemos 自用,可保留模板
  base_url: "${LLM_BASE_URL}"

openclaw:
  repo_path: "${OPENCLAW_REPO_PATH}"
  visibility_mode: "settled"
  backend_mode: "hybrid"
  flush_mode: "shared_llm"
  answer_mode: "agent_local"
  memory_mode: "memory-core"
  agent_timeout_seconds: 180
  agent_llm:
    provider_id: "sophnet"
    base_url: "${LLM_BASE_URL}"        # ✅ 非 secret,展开后落盘 OK
    api: "openai-completions"
    model:
      id: "gpt-4.1-mini"
      name: "GPT 4.1 Mini (sophnet)"
      reasoning: false
      input: ["text"]
      cost: {input:0, output:0, cacheRead:0, cacheWrite:0}
      context_window: 128000
      max_tokens: 4096
    api_key_env: "LLM_API_KEY"         # ⚠️ secret marker (v0.6 / v0.7)
    env_vars:                          # bridge 白名单
      - "LLM_API_KEY"
      - "LLM_BASE_URL"                 # 让 openclaw 子进程 env 也有(虽然非 secret)
      - "LLM_MODEL"
      - "SOPH_API_KEY"
      - "SOPH_EMBED_URL"
      - "SOPH_EMBED_EASYLLM_ID"
  embedding:
    provider: "${OPENCLAW_EMBED_PROVIDER:sophnet}"      # 非 secret,模板+default
    model: "${OPENCLAW_EMBED_MODEL:text-embeddings}"    # 同上
    api_key_env: "SOPH_API_KEY"        # ⚠️ secret marker (v0.6 / v0.7)
    base_url: "${SOPH_EMBED_URL:https://www.sophnet.com/api/open-apis/projects/6RT1wCDeN59dVd1L10UMRz/easyllms/embeddings}"  # ✅ v0.7 保留 default
    easyllm_id: "${SOPH_EMBED_EASYLLM_ID:1aKhWpzR3QKay9sZZ1TqWi}"  # ✅ v0.7 保留 default
    output_dimensionality: 1024
```

**v0.7 规则**：
- 仅 `api_key` 类字段用 `*_env` marker → adapter 重建 `${VAR}` 落盘 → openclaw 解析 env
- 其他字段（`base_url`, `easyllm_id`, `provider`, `model`）保留 `${VAR:default}` 模板形态 → evermemos 展开 → 落盘明文（不是 secret，OK）
- yaml 因此**保留所有 default fallback** 行为

#### Resolved config 生成（v0.7 修订）

```python
def build_openclaw_resolved_config(
    *, workspace_dir, native_store_dir, backend_mode, flush_mode,
    memory_mode: str = "memory-core",
    agent_llm: Optional[dict] = None,
    embedding: Optional[dict] = None,
) -> dict:
    sqlite_path = str(Path(native_store_dir) / "memory" / "default.sqlite")

    # === memorySearch ===
    memory_search: dict[str, Any] = {
        "enabled": memory_mode != "noop",
        "store": {"path": sqlite_path,
                  "vector": {"enabled": backend_mode != "fts_only"}},
    }
    if embedding and backend_mode != "fts_only":
        memory_search["provider"] = embedding.get("provider", "sophnet")
        memory_search["model"] = embedding.get("model", "text-embeddings")
        memory_search["outputDimensionality"] = int(embedding.get("output_dimensionality", 1024))

        # v0.7: 仅 apiKey 走 marker→template;其他用 evermemos 展开值
        remote: dict[str, Any] = {}
        if "api_key_env" in embedding:
            remote["apiKey"] = "${" + embedding["api_key_env"] + "}"
        elif "api_key" in embedding:
            # 向下兼容老 yaml(不推荐):secret 落盘明文
            logger.warning(
                "embedding.api_key found in yaml; use api_key_env for security"
            )
            remote["apiKey"] = embedding["api_key"]
        # 非 secret 用展开值
        remote["baseUrl"] = embedding.get("base_url", "")
        remote["easyllmId"] = embedding.get("easyllm_id", "")
        memory_search["remote"] = remote
    elif backend_mode == "fts_only":
        memory_search["provider"] = "auto"

    resolved = {
        "memory": {"backend": "builtin"},
        "agents": {"defaults": {
            "workspace": workspace_dir,
            "userTimezone": "UTC",
            "memorySearch": memory_search,
            "compaction": {"memoryFlush": {"enabled": False}},
        }},
    }

    # === agent LLM provider ===
    if agent_llm:
        pid = agent_llm["provider_id"]
        md = agent_llm["model"]
        models_cfg = resolved.setdefault("models", {})
        models_cfg.setdefault("mode", "replace")
        providers = models_cfg.setdefault("providers", {})
        providers[pid] = {
            "baseUrl": agent_llm["base_url"],   # 非 secret,展开值
            # v0.7: 仅 apiKey 走模板
            "apiKey": "${" + agent_llm["api_key_env"] + "}",
            "api": agent_llm.get("api", "openai-completions"),
            "models": [{
                "id": md["id"], "name": md["name"],
                "reasoning": md.get("reasoning", False),
                "input": md.get("input", ["text"]),
                "cost": md.get("cost", {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}),
                "contextWindow": md["context_window"],
                "maxTokens": md["max_tokens"],
            }],
        }
        resolved["agents"]["defaults"]["model"] = f"{pid}/{md['id']}"

    # === plugins ===
    plugin_id = (
        memory_mode if memory_mode not in ("memory-core", "noop") else "memory-core"
    )
    if plugin_id == "memory-core":
        plugin_allow = ["memory-core"]
        memory_slot = "memory-core"
        plugin_entries = {"memory-core": {"enabled": True}}
    else:
        plugin_allow = ["memory-core", plugin_id]
        memory_slot = plugin_id
        plugin_entries = {
            "memory-core": {"enabled": False},
            plugin_id: {"enabled": True},
        }
    resolved["plugins"] = {
        "allow": plugin_allow,
        "slots": {"memory": memory_slot},
        "entries": plugin_entries,
    }

    return resolved
```

**单元测试要求（v0.7 加强）**：

```python
def test_resolved_config_does_not_leak_secrets():
    os.environ["LLM_API_KEY"] = "sk-shouldnotleak-12345"
    os.environ["SOPH_API_KEY"] = "sk-noleak-soph-67890"
    os.environ["SOPH_EMBED_URL"] = "https://expanded-url.example/embed"

    cfg = build_openclaw_resolved_config(
        workspace_dir="/tmp/x", native_store_dir="/tmp/y",
        backend_mode="hybrid", flush_mode="shared_llm",
        agent_llm={
            "provider_id": "sophnet",
            "base_url": "https://expanded-url.example",
            "api": "openai-completions",
            "api_key_env": "LLM_API_KEY",
            "model": {"id": "x", "name": "X", "context_window": 1, "max_tokens": 1},
        },
        embedding={
            "provider": "sophnet",
            "model": "x",
            "api_key_env": "SOPH_API_KEY",
            "base_url": "https://expanded-url.example/embed",  # v0.7: 已展开值
            "easyllm_id": "easy-id-123",
            "output_dimensionality": 1024,
        },
    )
    s = json.dumps(cfg)
    assert "sk-shouldnotleak-12345" not in s
    assert "sk-noleak-soph-67890" not in s
    assert "${LLM_API_KEY}" in s
    assert "${SOPH_API_KEY}" in s
    # v0.7: 非 secret 应保持展开值
    assert "https://expanded-url.example/embed" in s
    assert "easy-id-123" in s
```

#### Bridge env 白名单（v0.7：必须落到 _bridge_base_payload）

见 §4.8。

### 4.8 Adapter → bridge payload 映射（v0.7 修订：必须含 env_vars）

**当前 `openclaw_adapter.py:373-389`**：

```python
def _bridge_base_payload(self, sandbox: dict) -> dict:
    return {
        "repo_path": self._openclaw_repo_path,
        "config_path": sandbox.get("resolved_config_path", ""),
        "workspace_dir": sandbox.get("workspace_dir", ""),
        "state_dir": sandbox.get("native_store_dir", ""),
        "home_dir": sandbox.get("home_dir", ""),
        "cwd_dir": sandbox.get("cwd_dir", ""),
    }
```

**v0.7 修订（必须）**：

```python
def _bridge_base_payload(self, sandbox: dict) -> dict:
    """Fields every BridgeCommand needs.

    v0.7: agent_llm_env_vars MUST be present so bridge.envForSandbox can
    pass through real secret env to openclaw subprocess. Without this
    OpenClaw throws MissingEnvVarError on `${LLM_API_KEY}` / `${SOPH_API_KEY}`
    templates in resolved_config.
    """
    agent_llm = self._openclaw_cfg.get("agent_llm") or {}
    env_vars = agent_llm.get("env_vars") or []

    return {
        "repo_path": self._openclaw_repo_path,
        "config_path": sandbox.get("resolved_config_path", ""),
        "workspace_dir": sandbox.get("workspace_dir", ""),
        "state_dir": sandbox.get("native_store_dir", ""),
        "home_dir": sandbox.get("home_dir", ""),
        "cwd_dir": sandbox.get("cwd_dir", ""),
        "agent_llm_env_vars": list(env_vars),
    }
```

**单元测试（v0.7 必加）**：

```python
def test_bridge_payload_includes_env_whitelist():
    cfg = {
        "openclaw": {
            "repo_path": "/foo",
            "agent_llm": {
                "env_vars": ["LLM_API_KEY", "SOPH_API_KEY"]
            }
        }
    }
    adapter = OpenClawAdapter(config=cfg)
    sandbox = {"resolved_config_path": "/x.json", "workspace_dir": "/ws"}
    payload = adapter._bridge_base_payload(sandbox)
    assert payload["agent_llm_env_vars"] == ["LLM_API_KEY", "SOPH_API_KEY"]

def test_bridge_payload_env_vars_missing_yaml():
    """没配 env_vars 时不能 raise,降级为空列表"""
    adapter = OpenClawAdapter(config={"openclaw": {"repo_path": "/foo"}})
    payload = adapter._bridge_base_payload({})
    assert payload["agent_llm_env_vars"] == []
```

`bridge.mjs::envForSandbox` 已有 v0.4 逻辑读 `input.agent_llm_env_vars` 列表透传，但因为之前 payload 不含此字段，列表始终为空。v0.7 修复后链路打通。

---

## 5. 任务建模（v0.4 不变）

---

## 6. 评测指标（v0.5 不变）

---

## 7. 三阶段路线图

### Stage 0：D1 完成 / D2-D5 接续（v0.7 修订）

**D1 状态：✅ 已完成**

**D2** — Bridge + resolved_config 改造（v0.7 关键加项 ⭐）

- [ ] bridge.mjs 加 `handleAgentRun`：
  - [ ] 从 stderr 抓取
  - [ ] ⭐ stripAnsi 用 **`/\x1b\[[0-9;]*m/g`** (v0.7,含 ESC)
  - [ ] ⭐ extractJsonObject **line-based + JSON.parse 验证** (v0.7)
  - [ ] reply: `parsed.payloads?.[0]?.text`
- [ ] bridge.mjs `envForSandbox` 加 `agent_llm_env_vars` 白名单透传
- [ ] ⭐ `_bridge_base_payload` 加 `agent_llm_env_vars` 字段 (v0.7)
- [ ] `build_openclaw_resolved_config` 接受 `memory_mode + agent_llm + embedding`
  - [ ] ⭐ 仅 `api_key` 字段从 `*_env` 重建 `${VAR}` 模板 (v0.7)
  - [ ] 非 secret 字段(base_url/easyllm_id) 用展开值落盘 (v0.7)
  - [ ] sophnet embedding `remote` 段
  - [ ] `plugins.allow + slots + entries`
  - [ ] setdefault 合并
- [ ] 单元测试：
  - [ ] ⭐ `test_resolved_config_does_not_leak_secrets` (v0.6 + v0.7 加强)
  - [ ] ⭐ `test_bridge_payload_includes_env_whitelist` (v0.7)
  - [ ] ⭐ `test_extract_json_with_curly_in_reply` (v0.7) — 验证 reply 含 `{}` 不破坏
  - [ ] ⭐ `test_extract_json_with_prefix_jsonlike` (v0.6 / v0.7)
  - [ ] ⭐ `test_strip_ansi_does_not_eat_brackets` (v0.7)
  - [ ] env 透传 + setdefault 合并
- [ ] 集成验证：写一个 mock openclaw fixture，用真实 ${} 模板的 resolved config 通过 bridge 调用，确认 env 白名单生效

**D3** — Adapter 改造 + bootstrap + timeout + metrics

- [ ] `OpenClawAdapter`:
  - [ ] `answer_mode` + `_generate_answer_via_agent`
  - [ ] session_id = `<conv_id>__<qid>`
  - [ ] `_sandbox_by_conversation_id` 持久化:
    - [ ] add() 阶段
    - [ ] ⭐ `build_lazy_index()` 阶段 (v0.7)
  - [ ] `get_answer_timeout()` override
  - [ ] ⭐ `_prebootstrap_workspace` retry + 硬 raise (v0.7)
- [ ] `BaseAdapter.get_answer_timeout()` default
- [ ] `evermemos/answer_stage.py:235` 改 `timeout_seconds = adapter.get_answer_timeout() ...`
- [ ] `search()` agent_local 返回 skipped SearchResult
- [ ] `retrieval_metrics.py` + `content_overlap.py` skipped suppress
- [ ] 单元测试:
  - [ ] ⭐ `test_sandbox_persisted_via_build_lazy_index` (v0.7)
  - [ ] ⭐ `test_prebootstrap_raises_on_failure` (v0.7) — 验证 dummy run 失败时 add() 抛
  - [ ] `test_per_qa_session_id_isolation`
- [ ] Smoke: LoCoMo-S 1 conv × 5 Q 两档,验证 50 并发不 race

**D4 / D5**（v0.5 不变）

### Stage 1 / Stage 2（v0.4 不变）

---

## 8. 目录结构（v0.5 不变）

---

## 9. 版本固定与复现（v0.5 不变）

---

## 10. 风险与开放决策

### 10.1 风险清单（v0.7 新增 R23-R26）

| # | 风险 | 状态 | 缓解 |
|---|---|---|---|
| R1-R18 | （v0.5/v0.6 已 PASS 或缓解） | | |
| R19 | evermemos `_replace_env_vars` 立即展开 yaml `${VAR}` 导致 secret 落盘 | ⚠️ Open → D2 | yaml 改 `*_env` marker（仅 secret） |
| R20 | answer_stage 硬编码 timeout=120s | ⚠️ Open → D3 | `BaseAdapter.get_answer_timeout()` 协商 |
| R21 | 50 并发 first-run race workspace bootstrap | ⚠️ Open → D3 | `_prebootstrap_workspace` (retry + raise) |
| R22 | extractJsonObject 范围过宽 | ⚠️ Open → D2 | line-based + JSON.parse 验证 |
| **R23 (v0.7)** | **`_bridge_base_payload` 缺 `agent_llm_env_vars`,openclaw 抛 MissingEnvVarError** | ⚠️ Open → D2 | 必加字段 + 单元测试守 |
| **R24 (v0.7)** | **`build_lazy_index` 不持久化 sandbox,resume 路径炸** | ⚠️ Open → D3 | 与 add() 双路径同步填 sandbox map |
| **R25 (v0.7)** | **v0.6 把 base_url/easyllm_id 改 `*_env` 抹掉 yaml default fallback** | ⚠️ Open → D2 | 仅 secret 用 marker;非 secret 保留 `${VAR:default}` |
| **R26 (v0.7)** | **stripAnsi regex 缺 ESC + brace matcher 不识 quoted string** | ⚠️ Open → D2 | regex 加 `\x1b`;改 line-based + JSON.parse 验证 |

### 10.2 / 10.3（v0.5 不变）

---

## 11. 第一周执行清单（v0.7 修订）

**D1** ✅ 已完成

**D2** — Bridge + resolved_config（v0.7 关键加项 ⭐）
- [ ] bridge.mjs `handleAgentRun`
  - [ ] stderr 抓取 + ⭐ stripAnsi `/\x1b\[[0-9;]*m/g` (v0.7)
  - [ ] ⭐ `extractJsonObject` line-based + JSON.parse 验证 (v0.7)
  - [ ] reply: `parsed.payloads[0].text`
  - [ ] 透出 trace 字段
- [ ] bridge.mjs `envForSandbox` 加 `agent_llm_env_vars` 白名单
- [ ] ⭐ `_bridge_base_payload` 加 `agent_llm_env_vars` 字段 (v0.7)
- [ ] `build_openclaw_resolved_config`
  - [ ] ⭐ 仅 secret 字段从 `*_env` 重建模板 (v0.7)
  - [ ] sophnet embedding `remote` 段
  - [ ] `plugins.allow + slots + entries`
  - [ ] setdefault 合并
- [ ] 单元测试 (v0.7 加强):
  - [ ] `test_resolved_config_does_not_leak_secrets`
  - [ ] ⭐ `test_bridge_payload_includes_env_whitelist`
  - [ ] ⭐ `test_strip_ansi_does_not_eat_brackets`
  - [ ] ⭐ `test_extract_json_with_curly_in_reply`
  - [ ] `test_extract_json_with_prefix_jsonlike`

**D3** — Adapter + bootstrap + timeout + metrics (v0.7 加强)
- [ ] `OpenClawAdapter`:
  - [ ] `answer_mode` + `_generate_answer_via_agent`
  - [ ] session_id = `<conv_id>__<qid>`
  - [ ] `_sandbox_by_conversation_id` 持久化（add + build_lazy_index）⭐ v0.7
  - [ ] `get_answer_timeout()` override
  - [ ] `_prebootstrap_workspace()` retry + 硬 raise ⭐ v0.7
- [ ] `BaseAdapter.get_answer_timeout()` default
- [ ] `evermemos/answer_stage.py:235` 改 timeout 协商
- [ ] `search()` agent_local 返回 skipped SearchResult
- [ ] metrics skipped suppress
- [ ] 单元测试 (v0.7 加强):
  - [ ] ⭐ `test_sandbox_persisted_via_build_lazy_index`
  - [ ] ⭐ `test_prebootstrap_raises_on_failure`
  - [ ] `test_per_qa_session_id_isolation`
- [ ] Smoke: LoCoMo-S 1 conv × 5 Q 两档

**D4 / D5**（v0.5 不变）

---

## 12. 签收标准（v0.7 修订）

### Stage 0 DoD
- [x] D1 5 Checks 全 PASS
- [ ] D2 bridge `handleAgentRun` stderr line-based + schema validate + ANSI ESC 单元测试
- [ ] D2 `test_resolved_config_does_not_leak_secrets`（仅 secret marker）
- [ ] ⭐ D2 `test_bridge_payload_includes_env_whitelist`（v0.7）
- [ ] ⭐ D2 `test_strip_ansi_does_not_eat_brackets`（v0.7）
- [ ] ⭐ D2 `test_extract_json_with_curly_in_reply`（v0.7）
- [ ] D2 sophnet embedding + plugins.allow/slots 单元测试
- [ ] D3 adapter sandbox 持久化双路径单元测试 ⭐ v0.7
- [ ] ⭐ D3 `_prebootstrap_workspace` retry+raise 单元测试（v0.7）
- [ ] D3 `get_answer_timeout` 协商使 180s 真生效
- [ ] D3 metrics skipped suppress 单元测试
- [ ] D4 三 condition × 全量 LoCoMo-S 跑通
- [ ] D5 四维 gate + go/no-go

### Stage 1 / 2（v0.4 不变）

---

## 附录 A：关键代码骨架（v0.7 修正）

### A.1 bridge.mjs handleAgentRun（v0.7：line-based + ANSI ESC + schema validate）

```javascript
// ANSI escape regex including the actual ESC byte (0x1b)
const ANSI_ESCAPE_RE = /\x1b\[[0-9;]*m/g;

function stripAnsi(s) {
  return s.replace(ANSI_ESCAPE_RE, "");
}

// v0.7: line-based candidate scan + JSON.parse validation
// 不依赖手写 brace matcher,免受 reply 文本里 {/} 干扰
function extractJsonObject(text) {
  const lines = text.split("\n");

  // 收集所有"独立成行的 {"作为候选起点
  const startCandidates = [];
  for (let i = 0; i < lines.length; i++) {
    if (lines[i] === "{") startCandidates.push(i);
  }

  // 反向尝试:从最近的 startLine 开始,从 lines 末尾找匹配的 }
  for (let s = startCandidates.length - 1; s >= 0; s--) {
    const startLine = startCandidates[s];
    for (let endLine = lines.length - 1; endLine >= startLine; endLine--) {
      if (lines[endLine] !== "}") continue;
      const block = lines.slice(startLine, endLine + 1).join("\n");
      try {
        const obj = JSON.parse(block);
        if (obj
            && Object.prototype.hasOwnProperty.call(obj, "payloads")
            && Object.prototype.hasOwnProperty.call(obj, "meta")) {
          return obj;
        }
      } catch {
        // JSON 解析失败:此 [start, end] 不是有效 JSON,继续找
      }
    }
  }
  return null;
}

async function handleAgentRun(input, launcher) {
  if (!launcher) return { ok: true, command: "agent_run", reply: "[stub]", raw: {} };

  const env = envForSandbox(input);
  const cwd = cwdForSandbox(input);
  const args = [
    "agent", "--local",
    "--session-id", String(input.session_id),
    "--message", String(input.message ?? ""),
    "--json",
    "--timeout", String(input.timeout_seconds ?? 180),
  ];

  const { code, stdout, stderr } = await runLauncher(launcher, args, env, cwd);

  // v0.5: openclaw --json 输出走 stderr
  const merged = stripAnsi(stderr || "") || stripAnsi(stdout || "");

  if (code !== 0) {
    return {
      ok: false, command: "agent_run",
      error: extractErrorTail(merged) || `exit ${code}`,
    };
  }

  const parsed = extractJsonObject(merged);
  if (!parsed) {
    return { ok: false, command: "agent_run",
             error: "no valid JSON object (with payloads+meta) found in stderr" };
  }

  const reply = parsed.payloads?.[0]?.text ?? "";
  const meta = parsed.meta || {};
  return {
    ok: true, command: "agent_run",
    reply, raw: parsed,
    duration_ms: meta.durationMs ?? null,
    aborted: meta.aborted ?? false,
    stop_reason: meta.stopReason ?? null,
    tool_names: (meta.systemPromptReport?.tools?.entries || []).map(t => t.name),
    system_prompt_chars: meta.systemPromptReport?.systemPrompt?.chars ?? null,
    last_call_usage: meta.agentMeta?.lastCallUsage ?? null,
  };
}

function extractErrorTail(text) {
  return text.split("\n").filter(l => l.trim()).slice(-10).join("\n");
}
```

**v0.7 单元测试 fixture**：

```javascript
describe("stripAnsi", () => {
  it("strips real ANSI sequences", () => {
    expect(stripAnsi("\x1b[31mERROR\x1b[0m")).toBe("ERROR");
  });
  it("preserves [literal brackets] in plain text", () => {
    // 没 ESC 前缀的 [...m 不应被吞
    expect(stripAnsi("text with [33m] in it"))
      .toBe("text with [33m] in it");
  });
});

describe("extractJsonObject", () => {
  it("parses real D1 stderr (warning + final JSON)", () => {
    const txt = readFileSync("/tmp/openclaw-d1-smoke/check1.stderr.clean", "utf8");
    const r = extractJsonObject(txt);
    expect(r.payloads[0].text).toBe("ECHO");
  });

  it("ignores prefix JSON-like blocks before final result", () => {
    const txt = `
[plugins] some warning
{"level":"error","code":42}
{
  "payloads": [{"text":"FINAL", "mediaUrl":null}],
  "meta": {"durationMs":100,"aborted":false,"stopReason":"stop","agentMeta":{}}
}`;
    const r = extractJsonObject(txt);
    expect(r.payloads[0].text).toBe("FINAL");
  });

  it("handles reply containing curly braces", () => {
    const txt = `
{
  "payloads": [{"text":"use {x} as {y}", "mediaUrl":null}],
  "meta": {"durationMs":100,"aborted":false,"stopReason":"stop","agentMeta":{}}
}`;
    const r = extractJsonObject(txt);
    expect(r.payloads[0].text).toBe("use {x} as {y}");
  });

  it("returns null for stderr without payloads+meta", () => {
    const txt = `{"some":"other","object":true}`;
    expect(extractJsonObject(txt)).toBeNull();
  });
});
```

### A.2 OpenClawAdapter（v0.7：双路径 sandbox 持久化 + retry-raise prebootstrap）

```python
class OpenClawAdapter(BaseAdapter):
    def __init__(self, config, output_dir=None):
        super().__init__(config)
        # ...
        self._sandbox_by_conversation_id: dict[str, dict] = {}

    def get_answer_timeout(self) -> float:
        return float(self._openclaw_cfg.get("agent_timeout_seconds", 180)) + 30.0

    def _bridge_base_payload(self, sandbox: dict) -> dict:
        # v0.7: 必须含 agent_llm_env_vars (R23 修)
        agent_llm = self._openclaw_cfg.get("agent_llm") or {}
        env_vars = agent_llm.get("env_vars") or []
        return {
            "repo_path": self._openclaw_repo_path,
            "config_path": sandbox.get("resolved_config_path", ""),
            "workspace_dir": sandbox.get("workspace_dir", ""),
            "state_dir": sandbox.get("native_store_dir", ""),
            "home_dir": sandbox.get("home_dir", ""),
            "cwd_dir": sandbox.get("cwd_dir", ""),
            "agent_llm_env_vars": list(env_vars),
        }

    async def add(self, conversations, output_dir=None, **kw):
        # ...
        for conv in conversations:
            sandbox = self._prepare_conversation_sandbox(...)
            self._sandbox_by_conversation_id[conv.conversation_id] = sandbox

            try:
                await self._ingest_conversation(sandbox, conv)
                await self._flush_and_settle_if_needed(sandbox)
                self._assert_visibility_contract(sandbox)
                # v0.6: pre-bootstrap (v0.7: retry + raise)
                if self._openclaw_cfg.get("answer_mode") == "agent_local":
                    await self._prebootstrap_workspace(sandbox)
            except Exception:
                # ... mark run_status=failed,record handle ...
                raise
            # ...

    def build_lazy_index(self, conversations, output_dir):
        """v0.7: 与 add() 平行,从 disk handle 重建 sandbox map (R24 修)"""
        root_dir = self._locate_existing_run_root(Path(output_dir))
        handles: dict[str, dict] = {}
        for conv in conversations:
            handle_path = root_dir / "conversations" / conv.conversation_id / "handle.json"
            if not handle_path.exists():
                continue
            handle = json.loads(handle_path.read_text())
            if handle.get("run_status") != "ready":
                continue
            mode = handle.get("visibility_mode")
            state = handle.get("visibility_state")
            if mode == "settled" and state != "settled":
                continue
            if mode != "settled" and state not in ("indexed", "settled"):
                continue
            handles[conv.conversation_id] = handle
            self._sandbox_by_conversation_id[conv.conversation_id] = handle  # v0.7
        return {
            "type": "openclaw_sandboxes",
            "run_id": root_dir.name,
            "root_dir": str(root_dir),
            "conversations": handles,
        }

    def _sandbox_for(self, conversation_id: str) -> dict:
        sandbox = self._sandbox_by_conversation_id.get(conversation_id)
        if sandbox is None:
            raise RuntimeError(
                f"No sandbox for conversation_id={conversation_id}. "
                f"add() or build_lazy_index() must run before answer()."
            )
        return sandbox

    async def _prebootstrap_workspace(self, sandbox: dict) -> None:
        """v0.7: retry + 硬 raise (R21 加强)"""
        last_error = None
        for attempt in range(3):
            payload = {
                **self._bridge_base_payload(sandbox),
                "command": "agent_run",
                "session_id": f"{sandbox['conversation_id']}__bootstrap",
                "message": "Reply with: BOOTSTRAP_OK",
                "timeout_seconds": 60,
            }
            try:
                resp = await arun_bridge(self._bridge_script_path(), payload)
                if resp.get("ok"):
                    last_error = None
                    break
                last_error = resp.get("error", "")
            except Exception as err:
                last_error = str(err)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

        if last_error:
            raise RuntimeError(
                f"prebootstrap failed for {sandbox['conversation_id']} "
                f"after 3 attempts: {last_error}"
            )

        ws = Path(sandbox["workspace_dir"])
        expected = ["AGENTS.md", "SOUL.md", "TOOLS.md"]
        missing = [n for n in expected if not (ws / n).exists()]
        if missing:
            raise RuntimeError(
                f"workspace bootstrap files missing for "
                f"{sandbox['conversation_id']}: {missing}"
            )

    async def answer(self, query, context, **kwargs):
        # ... v0.6 不变 ...
        pass

    async def _generate_answer_via_agent(self, query, conv_id, qid):
        sandbox = self._sandbox_for(conv_id)
        payload = {
            **self._bridge_base_payload(sandbox),
            "command": "agent_run",
            "session_id": f"{conv_id}__{qid}",
            "message": query,
            "timeout_seconds": int(
                self._openclaw_cfg.get("agent_timeout_seconds", 180)
            ),
        }
        resp = await arun_bridge(self._bridge_script_path(), payload)
        # ... v0.5 不变 ...
```

### A.3 BaseAdapter.get_answer_timeout（v0.6 不变）

### A.4 evermemos answer_stage 改造（v0.6 不变）

### A.5 retrieval_metrics skipped suppress（v0.3 不变）

### A.6 Stub plugin（v0.4 不变）

---

## 附录 B：Codex review trail（七轮）

- **B.1 第一轮 (v0.1 → v0.2)**：session contamination / agent LLM config / search skipped / stub gate / trace overpromise / single-delta gate
- **B.2 第二轮 (v0.2 → v0.3)**：plugin API 错（`{id,indexDocuments,search}`）/ provider config 错（`agents.defaults.providers`）/ merge 错 / noop 用配置开关 / metrics suppress 落地 / D4 N=3
- **B.3 第三轮 (v0.3 → v0.4)**：stub 6 处契约错 / ModelApi 不是 "openai" / ModelDefinition 必填 / env_vars 命名冲突 / adapter sandbox 查找 / stub import / noop 不够干净
- **B.4 D1 实测 (v0.4 → v0.5)**：JSON 走 stderr / payloads[0].text / 6 个非法 root key / plugins.allow + slots / sophnet embedding 必需 / sophnet token 不可用 / workspace bootstrap
- **B.5 第五轮 (v0.5 → v0.6)**：env/secret leak / answer_stage 硬编码 timeout / workspace race / extractJsonObject 范围过宽
- **B.6 第六轮 (v0.6 → v0.7)**：
  - bridge_base_payload 缺 agent_llm_env_vars（**Blocker**——所有 bridge 调用炸）
  - build_lazy_index 不持久化 sandbox（**Blocker**——resume 路径炸）
  - base_url_env 抹去原 yaml default（修法：只对真 secret 用 marker）
  - stripAnsi 缺 ESC + brace matcher 不识 quoted string
  - prebootstrap 软 warning 不阻挡 race
- **B.7 v0.7 遗留**：所有 review 发现的问题已设计层闭合。剩余假设 (plugin-sdk import / MemoryPluginRuntime 工作量 / openclaw trace 能力) 仅 Stage 1 编译期/运行期能验证

---

**v0.7 结束**。相对 v0.6：
- bridge_base_payload 加 env_vars 字段（R23）
- build_lazy_index 双路径填 sandbox map（R24）
- 仅 secret 用 marker（R25）
- stripAnsi 加 ESC + JSON 解析改 line-based（R26）
- prebootstrap retry + raise（R21 加强）
- 单元测试矩阵扩展到覆盖以上每条

下一步：D2 bridge 改造 + 单元测试。所有 acceptance criteria 都在 §11 D2/D3 清单 + §12 DoD 里。
