# D1 Smoke Findings (2026-04-24)

> **Context**：v0.4 §10.3 Stage 0 Day 1 dependency check 实跑结果
> **Working dir**: `/tmp/openclaw-d1-smoke/`
> **Openclaw**: commit `7da23c36`, repo `/Data3/shutong.shan/openclaw/repo`

---

## 5 个 Check 结果

### ✅ Check 1: `openclaw agent --local --json` 真实 stdout schema

**关键修正**：JSON 输出走 **stderr，不是 stdout**。stdout 始终为空（0 bytes）。

```
exit=0, stdout bytes=0, stderr bytes=7831
```

bridge.mjs 必须从 stderr 读 JSON，不是 stdout。

**Reply schema（v0.4 §A.1 错误，必须改）**：

```json
{
  "payloads": [
    { "text": "ECHO", "mediaUrl": null }
  ],
  "meta": {
    "durationMs": 2796,
    "agentMeta": {
      "sessionId": "check1_run",
      "provider": "sophnet",
      "model": "gpt-4.1-mini",
      "lastCallUsage": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0 }
    },
    "aborted": false,
    "stopReason": "stop",
    "systemPromptReport": {
      "source": "run",
      "sessionId": "check1_run",
      "sessionKey": "agent:main:explicit:check1_run",
      "provider": "sophnet",
      "model": "gpt-4.1-mini",
      "workspaceDir": "...",
      "tools": {
        "listChars": ...,
        "schemaChars": ...,
        "entries": [
          { "name": "memory_search", "summaryChars": 385, "schemaChars": 260, "propertiesCount": 4 },
          ...
        ]
      },
      ...
    }
  }
}
```

**Reply 提取路径**：`parsed.payloads?.[0]?.text` —— **不是** v0.4 写的 `parsed.reply ?? parsed.text ?? parsed.content`。

**Tool list 提取路径**：`parsed.meta.systemPromptReport.tools.entries[].name`。

**Token usage 备注**：sophnet 不返回 usage，全 0。openclaw 自己也不计算 input token，所以 token 维度的指标在 sophnet 后端**不可用**。换成 OpenAI/Anthropic 后端会有数据。

### ✅ Check 2: minimal openclaw.json schema

**v0.4 模板有 6 处非法 key**，全部必须删除：

```
- approvals: Unrecognized key: "mode"
- gateway: Unrecognized key: "enabled"
- <root>: Unrecognized keys: "schemaVersion", "webhooks", "flows", "dashboard"
```

**真实可工作的最小 config**：

```json
{
  "models": {
    "mode": "replace",
    "providers": {
      "sophnet": {
        "baseUrl": "${LLM_BASE_URL}",
        "apiKey": "${LLM_API_KEY}",
        "api": "openai-completions",
        "models": [{
          "id": "gpt-4.1-mini",
          "name": "GPT 4.1 Mini (sophnet)",
          "reasoning": false,
          "input": ["text"],
          "cost": {"input":0,"output":0,"cacheRead":0,"cacheWrite":0},
          "contextWindow": 128000,
          "maxTokens": 4096
        }]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": "sophnet/gpt-4.1-mini",
      "workspace": "${WORKSPACE_DIR}",
      "userTimezone": "UTC",
      "memorySearch": {
        "enabled": true,
        "store": {"path": "${SQLITE_PATH}", "vector": {"enabled": false}}
      },
      "compaction": {"memoryFlush": {"enabled": false}}
    }
  },
  "plugins": {
    "allow": ["memory-core"],
    "slots": {"memory": "memory-core"},
    "entries": {"memory-core": {"enabled": true}}
  }
}
```

**v0.4 没发现的关键字段**：
- `plugins.allow: string[]` —— 必须用，否则 dist-runtime/ 下 108 个 extension 全被加载，acpx 这种 broken 的会报错
- `plugins.slots.memory: string` —— 显式指定 memory slot 的 owner
- 6 个非法 key 删除（schemaVersion/webhooks/flows/dashboard/approvals/gateway）

**ModelApi 验证**：`"openai-completions"` 配 sophnet 工作（reply 正常生成）。`"openai-responses"` 未单独测，但 completions 已通过即可。

**ModelDefinitionConfig 必填字段确认**：`{id, name, reasoning, input, cost{input,output,cacheRead,cacheWrite}, contextWindow, maxTokens}` 7 个字段，与 v0.4 列出一致。`api` 字段在 model 级是可选的（provider 级 `api` 已经传播到 model）。

### ✅ Check 3: per-QA session-id 隔离

```
Session A (conv_X__q1):
  message: "My name is Alice and I love pizza. Just acknowledge."
  reply:   "Noted, Alice. You love pizza."

Session B (conv_X__q2):
  message: "What name did I tell you in the previous message?"
  reply:   "You haven't told me your name in any previous message in this session."
```

**结论**：不同 session-id 的 chat history 完全互不可见。v0.4 §4.4 的 `session_id = f"{conv_id}__{qid}"` 设计正确。

### ✅ Check 4: noop 模式 agent 真无 memory tool

| 模式 | tool 总数 | memory 相关 tool |
|---|---|---|
| `memorySearch.enabled=true` | 27 | `memory_search`, `memory_get` |
| `memorySearch.enabled=false` | 25 | **无** |

**结论**：`memorySearch.enabled=false` 干净禁用 memory 路径，agent 的 prompt 中 tool 清单里**不再出现** memory_search 和 memory_get。Codex review 第三轮 New 3 关注的"noop 不够干净"问题**实测不存在** —— openclaw 的实现确实在 enabled=false 时摘除 tools。

但 v0.4 §10.3 Check 4 的强化判据仍然有意义：保留作为 noop 配置变更的回归 sanity。

### ✅ Check 5: `${LLM_API_KEY}` SecretInput 模板解析

通过反向证明：所有 3 次 agent --local 调用都成功打通 sophnet endpoint。如果模板未解析，HTTP auth 401。**模板生效**。

---

## Bonus 发现（D1 触发的非 Check 项）

### B.1 dist-runtime/ 自动 plugin 发现

openclaw 启动时扫描 `dist-runtime/extensions/*`，所有 108 个 extension 都尝试加载。**`plugins.allow` 是必需的过滤器**，否则：

```
[plugins] acpx failed to load from /Data3/shutong.shan/openclaw/repo/dist-runtime/extensions/acpx/index.js: Error: Cannot find module 'acpx/runtime'
[openclaw] Failed to start CLI: PluginLoadFailureError: plugin load failed: acpx
```

直接退出。`plugins.allow` 后非 allowed 的 plugin 加载错误降级为 warning（exit 0 不受影响）。

### B.2 Workspace bootstrap 自动写文件

第一次 `agent --local` 在 `workspace/` 下创建：
- `AGENTS.md` (7809 chars)
- `SOUL.md` (1738 chars)
- `TOOLS.md` (850 chars)
- `IDENTITY.md` (633 chars)
- `USER.md` (...)

这是 openclaw 的 agent 身份/工具 prompt 文件。eval 上下文可能需要在 docker 内 disable 这个 bootstrap 或预先填充。Stage 0 D2-D4 不阻塞。

### B.3 系统 prompt 体积

`systemPrompt.chars = 21618`。一次 agent 调用 base prompt 已经占 21K chars。如果后面要做 prompt budget 控制需要注意。

### B.4 Memory index 需要 sophnet embedding 配置

```
Memory index failed (main): openai embeddings failed: 403
{"error":{"code":"unsupported_country_region_territory",...}}
```

默认 embedding provider 走 OpenAI 直连，地理封锁。**需要在 `agents.defaults.memorySearch` 加 `provider: "sophnet"` 和 `remote: {baseUrl, easyllmId, apiKey}`**。evermemos 现有 `build_openclaw_resolved_config` 已经做这个 —— v0.5 集成时直接复用。

### B.5 LLM token usage 不可用

sophnet 不返回 usage，openclaw 不计算 input token：
```
"lastCallUsage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
```

→ Stage 0 metrics 表里"token cost"维度在 sophnet 后端**不可用**。如果要算 token 需要 evermemos 自己 tokenize 或换 OpenAI 直连。

---

## v0.4 错误清单（v0.5 必修）

按优先级排：

| 严重度 | 章节 | v0.4 内容 | v0.5 修法 |
|---|---|---|---|
| **Blocker** | §A.1 bridge | `extractJsonTail(stdout)` | 改读 stderr |
| **Blocker** | §A.1 bridge | `reply: parsed.reply ?? parsed.text ?? parsed.content` | 改成 `parsed.payloads?.[0]?.text` |
| **Blocker** | §4.2 模板 | 包含 schemaVersion/webhooks/flows/dashboard/approvals/gateway 6 个非法 key | 全删 |
| **Blocker** | §4.2 模板 | 缺 `plugins.allow` + `plugins.slots.memory` | 补上 |
| **High** | §4.2 模板 | 缺 sophnet embedding 配置 | 补 `memorySearch.provider/remote`(复用 evermemos `embedding` 字段) |
| **Medium** | §6.1 / §10.2 | 把 token 列入 metrics | sophnet 后端标 N/A,只在 OpenAI 后端可信 |
| **Medium** | §10.3 Check 1 | "确认 stdout schema" | 改成 "stderr schema",并指定 stderr→JSON 提取 |
| **Low** | 风险 R12 | "openai-completions vs openai-responses" | 已确认 completions 工作,关闭 |

---

## D1 决策

**全部 5 Checks PASS**。Stage 0 可以继续推进 D2-D5。

**v0.5 修订必要性**：高。v0.4 的 §A.1 bridge 代码骨架若实现就跑不通（stdout 空、reply 字段名错）。建议在写 D2 bridge 改造代码前先出 v0.5。

**v0.5 范围**：
- §A.1 bridge `handleAgentRun` 改 stderr 读取 + payloads[0].text
- §4.2 模板按 D1 实证重写
- §10.3 Check 列表精简（5 项中部分已永久通过）
- §A.4 resolved_config 加 sophnet embedding 段（复用 evermemos 既有逻辑）

---

## 附：测试 artifact 留档

```
/tmp/openclaw-d1-smoke/
├── openclaw.v04.json           # baseline (memorySearch.enabled=true)
├── openclaw.noop.json          # noop (memorySearch.enabled=false)
├── check1.parsed.json          # full reply schema reference
├── check3a.combined            # session A response
├── check3b.combined            # session B response
├── check4.clean                # noop run (no memory tools)
├── checkY.combined             # agent + memory_search 调用 (空索引)
└── workspace/
    ├── AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md  # openclaw bootstrap
    └── memory/session-S0-test.md
```
