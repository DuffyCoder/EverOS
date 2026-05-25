# OpenViking 改进路线图（生产优先）

本文档由 LoCoMo + OpenClaw + OpenViking（noop / session-bundle ingest）评测中暴露的问题整理而来，**改动目标以 OpenViking 在生产环境更好用为准**（抽取更准确、检索更合理、注入更可读、token 更省），评测只是验证手段，**不做仅适配 benchmark 、会损害真实用户路径的改动**。

相关分析见：[ov_optimization.md](./ov_optimization.md)。

---

## 原则

1. **生产场景优先**：长期会话用户 incremental commit 时，`latest_archive_overview` 与 assemble 的 archive 层是合理设计；评测里「全量 ingest 后再逐题 QA」只是 stress test，不能为此削弱线上逻辑。
2. **修数据管线，不修 benchmark 歪招**：优先改 Extract / prefetch / recall 呈现 / ingest 契约，而不是在 eval 里关闭 assemble、绕过 WM、或写死 eval-only 分支。
3. **pip 与 git 对齐**：若 OpenViking Server 由 **pip 安装** 拉起，本地 `OpenViking/` git 改动需 **`pip install -e` 或发版安装** 并 **重启服务** 后才生效；改 git 不重启 server 等于未改。
4. **改 Extract / ingest 必 re-ingest**：长期记忆是 commit 时 materialize 的；改抽取逻辑后需对受影响 user 空间重新 ingest，不能只重跑 QA。

---

## 部署与生效方式（速查）

| 组件 | 典型部署 | 改动如何生效 | 是否重建 OpenClaw 镜像 |
|------|----------|--------------|------------------------|
| OpenViking Server | 主机 `pip`，如 `:1933` | 换包 / `pip install -e` + **重启 OV** | 否 |
| OpenClaw + OV 插件 | Docker | 更新 `openclaw-eval/plugins/openviking` + **重建镜像或 bind-mount 插件** | 是（或 mount 后重启容器） |
| EverOS eval / ingest | 主机 CLI + `ov_ingest` HTTP | 改代码后 **re-ingest + 重跑 eval** | 否 |

**记忆数据**：凡涉及 Extract 或 ingest 文本形态 → 必须 **re-ingest**（或清空 `viking://user/<id>/` 后重放）。

---

## 评测暴露的根因（与生产的对应关系）

| 现象 | 生产含义 | 应改进的方向 |
|------|----------|--------------|
| `events/` 始终为空 | 带时间的事实应进 events，却坍缩进 entities PATCH | OV Extract：prefetch + schema + WM→Extract 对齐 |
| autorecall 注入 `PATCH: { ... }` 原文 | 用户侧 prompt 难读、费 token、模型难解析 | 插件 recall 优先 abstract / 渲染后正文 |
| overview 有细节、memories 没有 | WM 与 Extract 双管线未 reconciliation | OV Extract 接入当前 archive WM；events 分流 |
| 图片题错误率高 | 纯 text ingest 丢 URL/query；blip 不含 OCR 信息 | ingest 契约 + 可选 resources/vision（中长期） |
| LoCoMo 全量 ingest 后 QA 用不到「最后一档 overview」 | **评测 artifact**；线上 incremental 会话仍依赖 latest overview | **不**为 eval 削弱 assemble；用 **archive 索引 + 结构化记忆** 覆盖历史 |

---

## P0 — 插件与 ingest 契约（低风险、可先验）

### P0-1：Auto-recall 注入可读摘要，而非 PATCH 原文

| 项 | 说明 |
|----|------|
| **侧** | EverOS OpenClaw 插件 `openclaw-eval/plugins/openviking`（或插件配置） |
| **改动** | 默认或推荐 **`recallPreferAbstract: true`**；若 read 全文且内容为 `PATCH:` 前缀，fallback 到 `.abstract.md` 或 apply patch 后再注入（避免 qa14 类 PATCH JSON 进 prompt） |
| **生产收益** | Prompt 更干净、token 更少、回答更稳；与「记忆是给 agent 读的可读知识」一致 |
| **评测** | 通常 **不必 re-ingest**，重跑 QA 即可 |
| **镜像** | 需更新容器内插件（重建或 mount） |

### P0-2：Ingest 文本保留图片元数据（url / blip / query） ✅ 已实现

| 项 | 说明 |
|----|------|
| **侧** | EverOS `evaluation/src/core/loaders.py`（`format_locomo_message_content_for_ingest`）、`ov_ingest.py` 文档 |
| **改动** | 纯 text ingest 形态：`{text}\n{url}: {blip}\n[image search query: {query}]`（对齐 openclaw-openviking-eval，并保留 LoCoMo `query`） |
| **生产收益** | 与真实场景一致：用户发图常伴随 caption/链接；Extract 与向量索引有更多信号，**不依赖** eval 专用逻辑 |
| **评测** | **必须 re-ingest** 后重跑 QA |
| **镜像** | 否 |

**明确不做（原 P0-3 类）**：不为 LoCoMo「全量 ingest 后再 QA」去 **关闭 assemble**、**忽略 `latest_archive_overview`** 或写 eval-only 短路——那会破坏 incremental 会话里「最近归档摘要进 context」的线上行为。

---

## P1 — OpenViking Server（Extract / prefetch / schema）

> 需在 **pip 运行环境** 安装含改动的 OpenViking 包并重启服务；改本地 git 不会自动生效。

### P1-1：Events 在 prefetch 中可见（修正 `add_only` 误伤）

| 项 | 说明 |
|----|------|
| **侧** | OpenViking `session_extract_context_provider.py`（及/或 `events.yaml`） |
| **改动** | `add_only` 仍表示「不写 PATCH 旧 event」，但 **prefetch 应对 `events/` 做 search 或读 index/overview**，让 Extract LLM 知道 events 通道与已有节点 |
| **生产收益** | 带时间事实进 `events/YYYY/MM/DD/`，符合 schema 设计；entities 少被 PATCH 覆盖混淆 |
| **评测** | re-ingest；**cat2 temporal** 预期提升最明显 |
| **镜像** | 否 |

### P1-2：Extract 接入当前 archive 的 WM（非「全局 latest」误用）

| 项 | 说明 |
|----|------|
| **侧** | OpenViking `SessionExtractContextProvider` + commit 管线 |
| **改动** | 每次 commit 抽记忆时，把 **本 archive 刚生成的 `.overview.md`（或 WM New Information）** 拼进 Extract prompt；`latest_archive_overview` 参数应表示 **上一档** 或 **当前档**，并在文档中定义语义 |
| **生产收益** | WM 高召回与结构化 memories 对齐，减少「overview 有、user memories 无」；incremental 会话每档 commit 都能把 narrative 落盘 |
| **评测** | re-ingest |
| **镜像** | 否 |

### P1-3：Prefetch search 使用会话相关 query

| 项 | 说明 |
|----|------|
| **侧** | OpenViking `session_extract_context_provider.prefetch` |
| **改动** | 用当前 batch 对话摘要/首条用户句 embedding 替代固定 `"[Keywords]"` |
| **生产收益** | 预取 URI 与 **本场对话** 相关，减少盲目 PATCH `caroline.md`；Extract LLM read 次数可下降（更高效） |
| **评测** | re-ingest |
| **镜像** | 否 |

### P1-4：Schema 模板强化（`custom_templates_dir`）

| 项 | 说明 |
|----|------|
| **侧** | OpenViking 配置 `memory.custom_templates_dir` + yaml 模板 |
| **改动** | entities 要求链到 events；events 强调绝对日期；hobbies/people 粒度示例——**默认模板也可 upstream 改进**，不仅 eval 私有目录 |
| **生产收益** | 可配置、可版本化的记忆分类策略，租户可定制 |
| **评测** | re-ingest |
| **镜像** | 否 |

### P1-5（中期）：Recall / find 返回渲染后正文

| 项 | 说明 |
|----|------|
| **侧** | OpenViking storage / read API 或插件 read 路径 |
| **改动** | 对仍含未 apply 的 `PATCH:` 存储格式，**对外 read/find 返回 merged 正文**（或稳定 abstract） |
| **生产收益** | 所有消费者（插件、SDK、agent）一致可读；从根上避免 PATCH 泄漏到 prompt |
| **评测** | 视实现：可能只需重跑 QA |
| **镜像** | 插件若仍 read 全文则配合 P0-1 |

---

## P2 — 多 archive 历史召回（生产 + 长跑会话）

适用于：**同一 session 多档 archive** 的真实用户（与 LoCoMo 全历史 QA 同构，但是正当产品需求）。

### P2-1：Auto-recall 补充 session / archive 层

| 项 | 说明 |
|----|------|
| **侧** | EverOS OpenClaw 插件 |
| **改动** | `before_prompt_build` 在 `find(user/memories)` 之外，利用 **`pre_archive_abstracts` / archive index** 或受限的 `find(session/history)`，按 **问题 query** 注入相关 archive 摘要（非仅 `latest_archive_overview` 一条） |
| **生产收益** | 长跑会话问早期事实时可召回；assemble 仍保留「最近一档详述 + 历史索引」分工 |
| **评测** | 多数情况 **不必 re-ingest**（依赖 session 侧 abstract 已有） |
| **镜像** | 需更新插件 |

### P2-2：Archive 原文工具链可发现性

| 项 | 说明 |
|----|------|
| **侧** | 插件 prompt + `ov_archive_expand` 工具策略 |
| **改动** | 当 memories recall 分数低或问题含时间/「什么时候」时，引导或策略性 expand（非纯靠模型「自觉」） |
| **生产收益** | 用户问细节时可落到 `messages.jsonl` 原文，与 WM 设计一致 |
| **评测** | 插件 + 可选配置 |
| **镜像** | 需更新插件 |

---

## 暂不纳入最小集（成本高 / 需单独立项）

| 项 | 原因 |
|----|------|
| 全局废除 PATCH merge | 影响面大，需迁移与兼容 |
| Eval-only 关闭 assemble / 忽略 latest overview | **损害 incremental 线上逻辑** |
| 全量多模态 resources + vision ingest | 正确但工程量大；P0-2 为 text 路径先行 |
| 仅改 LoCoMo 判分或 QA prompt | 不改善 OV 产品 |

---

## 推荐实施顺序

```text
1. P0-1  插件 recall 可读化     → 重启 OpenClaw 容器 → 重跑 QA（快）
2. P0-2  ingest 契约            → re-ingest → QA
3. P1-1 + P1-2  OV pip 升级    → 重启 OV → 清空 user → re-ingest → QA
4. P1-3、P1-4  继续迭代 OV
5. 若长跑 / 早期 session QA 仍弱 → P2-1、P2-2
6. P1-5  与 P0-1 合并考虑，统一 read 语义
```

---

## 改动 × 生效条件矩阵

| 改动 ID | OpenClaw 镜像 | 重启 OV (pip) | re-ingest | 仅重跑 QA |
|---------|---------------|---------------|-----------|-----------|
| P0-1 | 更新插件 | 否 | 否 | 是 |
| P0-2 | 否 | 否 | 是 | — |
| P1-* | 否 | 是 | 是 | — |
| P2-* | 更新插件 | 否 | 通常否 | 是 |

---

## 与 pip / git OpenViking 的工作流建议

1. 在 **跑 server 的 venv** 内：`pip install -e /path/to/OpenViking` 或安装指定版本 wheel。
2. 改 P1 前记录当前 `pip show openviking` 版本，便于回滚。
3. `custom_templates_dir` 指向 **版本管理下的模板目录**，与 server 配置一并部署，避免仅本机有效。
4. 每次 OV 升级后：对关键 conv 跑一轮 ingest + spot-check `memory_diff.json`（events 是否出现、PATCH 是否减少）。

---

## 文档维护

- 问题分析与案例：**[ov_optimization.md](./ov_optimization.md)**
- 本路线图：**ov-improvement-roadmap.md**（实施优先级与生产约束）
- OpenClaw 适配说明：**[openclaw_adapter.md](./openclaw_adapter.md)**
