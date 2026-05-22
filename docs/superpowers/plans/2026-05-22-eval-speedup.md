# 评测加速计划 — N 个 API key → N× 加速

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans

**Goal:** 在不影响 ingest-complete-then-stage3 barrier、不影响 acc 的前提下，挖掘评测各 stage 的加速空间，让 N 个 API key 真正带来接近 N× 加速。

**Architecture:** 按 stage 分析 wall-time bottleneck → 找到 key-saturated 还是 throttled-by-us → 改 default / 加 yaml 配置 / 改并发模型。

---

## Wall-Time Breakdown (full10 idle180 baseline)

| Stage | Wall time | 占比 | 主要操作 | bottleneck |
|---|---|---|---|---|
| 1 Add (ingest) | 31 min | 6% | OV ingest + fact-extract | OV server LLM throughput |
| 2 Search | **0.4s** | 0% | OV plugin retrieve | n/a (无需优化) |
| 3 Answer | 2hr 16min | 27% | OpenClaw agent_local | per-conv lock + key pool |
| 4 Evaluate | **5hr 47min** | **67%** | LLMJudge × num_runs=3 | client semaphore cap |
| **Total** | **8hr 35min** | | | |

**结论**：Stage 4 占 67%，是最大优化点。Stage 3 次之。Stage 1 第三。Stage 2 可忽略。

---

## 当前的并发能力 (实测)

```
.env: LLM_API_KEY + LLM_API_KEY_2 + LLM_API_KEY_3 + LLM_API_KEY_4  (N=4 keys)

Stage 1: max_concurrent_containers=4 (yaml hard-coded)
Stage 2: search.num_workers=5 (yaml hard-coded)
Stage 3: answer.max_concurrent=auto → 4 (= pool size)
Stage 4: judge_concurrency = _BASE_CONCURRENCY_PER_KEY(4) × N_keys(4) = 16
```

Stage 4 理论 throughput = 16 inflight / avg-latency = 16 / 10s = 1.6 call/s
Stage 4 实际 throughput = 4620 / 20520s = **0.225 call/s** (7× under-saturate)

→ **判断**：要么 single judge call latency 比 10s 长很多 (~70s)，要么 sophnet rate-limited 我们的 in-flight 更少。需要量化。

---

## 加速方向（按 ROI 排序）

### Tier 1 — 最高 ROI (Stage 4)

**1.1 量化 single judge call latency + 429 rate**
- 加 timing log 到 `_invoke_judge` 一轮
- 跑 small subset (e.g. smoke3 stage4) 收集 latency p50/p95/max + 429 frequency
- 决定 `_BASE_CONCURRENCY_PER_KEY` 该提到多少 (8/12/16/32)

**1.2 提高 `_BASE_CONCURRENCY_PER_KEY` 或加 yaml `judge_concurrency_per_key`**

当前 `llm_judge.py:37`:
```python
_BASE_CONCURRENCY_PER_KEY = 4
```
改成读 yaml `judge_concurrency_per_key`（default 仍 4 保守），让 user 按 sophnet limit 调到 8/16。

理论加速：BASE 4→16 = 4× → Stage 4 5.7hr → 1.4hr (saved 4.3hr)。

**1.3 增加 num_runs 的并行度** (低优先级)
- 当前 num_runs=3 是每 question 串行 3 次
- 改成 3 次 concurrent — 每 question 时间 / 3
- 但 num_runs 是 fault-tolerance 设计 (majority vote)，并行不增减总 call 数 → 收益不明显

### Tier 2 — Stage 3 加速

**2.1 Per-conv sharding (拆分大 conv 跨多 container)**

Stage 3 per-conv lock 是 openclaw sessionStore 限制。如果一个 conv 有 200 QA，per-conv 串行 wall = 200 × 50s = 10000s = 2.8hr。

破解：
- 改 docker_adapter 允许同 conv 多 container（如 2 个 container 各跑 100 QA）
- 每 container 独立 sessionStore → 不互相 race
- N container/conv → N× 加速 per-conv answer

代价：
- N 倍 RAM（每 container ~1.5GB）
- 每 container 重复 OV plugin 初始化（启动慢 ~30s）
- 适合 long-conv (200+ QA), 不适合 short-conv

**2.2 调 `max_concurrent` 上限**

当前 auto = N_keys = 4。10 conv 但只 4 inflight → 6 conv 在等。
- 改 auto 计算逻辑：`max(N_keys, N_convs)`
- 或者 hardcoded 提到 N_convs
- 但 per-conv lock 仍是 cap，wall time 不会变

**2.3 Streaming pipeline: search + answer 在一个 worker 内**

官方 eval.py qa: `process_single_question` 包含 search + answer，ThreadPoolExecutor 并发。
我们 stage 分离，answer 等 search 全部完成。

Stage 2 才 0.4s，stage barrier 的浪费很小 → **收益微乎其微**，不做。

### Tier 3 — Stage 1 加速

**3.1 提高 `max_concurrent_containers`**

当前 4。10 conv 串行 batch=4 ingest。
- 提到 10 (全并发) → 但 OV server 内部 fact-extract LLM 仍是 bottleneck
- OV server 的 LLM key 池由 OV 主仓控制 — 我们不能改

实测：smoke3 3 conv 同时 ingest，OV server 处理 70 task 用了 64-93 min。
若 10 conv 同时 = 230 task → OV server LLM time 同样的 throughput → 230 × 55s / OV_concurrency
- 若 OV 单 LLM key (concurrency=1): 230 × 55s = 211min = 3.5hr
- 若 OV 多 key (concurrency=4): 211 / 4 = 53min

→ 加 `max_concurrent_containers` 到 10 **可能** 帮助但取决于 OV server 内部 throughput。

**3.2 OV server 多 LLM key 配置**

OV server config 看是否支持 multiple sophnet keys。如果支持，配多 key → ingest 加速。
**用户约束：不动 OV 主仓。** → 但可以改 OV server config (yaml 配置)，这不是改代码。

需要 check OV server config 格式。

### Tier 4 — 微优化

**4.1 Search stage num_workers auto + conv 并行**
Stage 2 只 0.4s，**no benefit**。不做。

**4.2 max_concurrent_containers 跟 N_keys 绑定**

container 数 ≠ key 数。container 数受 RAM 限制，key 数受 sophnet quota。
但 container 内 OpenClaw agent 用 LLM_API_KEY (从 env_vars 传) → container 数应 ≤ key 数避免一个 key 跨 container 共享 RPM 限制。

→ 这一点已经在 answer_stage 用 `max_concurrent=auto`(=N_keys) 实现了。
→ Ingest 阶段 container 数应同样跟 N_keys 关联?但 ingest 不调 sophnet (调 OV server)。

→ ingest 阶段 max_concurrent_containers 跟 OV server throughput 关，跟 client N_keys 无关。

---

## 推荐实施顺序

### Phase A: Stage 4 加速 (最大 ROI)

1. 加 yaml `judge_concurrency_per_key` (default 4 保持向后兼容)
2. 加 detailed timing log 到 LLMJudge (单 call latency, 429 count)
3. 跑 smoke3 stage 4 only (用现成 answer_results.json) measure baseline
4. 调到 8/12/16，分别跑 stage 4，找到 429 epsilon 的 sweet spot
5. 默认值定下，commit

### Phase B: Stage 1 加速

6. 看 OV server config 是否支持 multi-key + 改 yaml (非主仓改动)
7. `max_concurrent_containers` 4→10 测试
8. 如果 OV 单 key 拖累，stage 1 wall time 没改善 → revert

### Phase C: Stage 3 加速 (复杂, 最后做)

9. Per-conv sharding 设计 + 实现
10. 测试 long-conv (locomo_2 等大 conv) wall time 改善

---

## 验证指标

每个 Phase 改完跑：
1. **Acc 不变**: 跟 smoke3-serial-raise 的 baseline acc 对比 (Δ acc < 1%)
2. **Wall time 减少**: 跟 baseline stage time 对比，记录 speedup factor
3. **Key utilization**: 跑期间监控 sophnet 后台 RPM 使用率

acc 不变是硬约束 (用户 Goal 2 强调)。如果某改动 acc 跌 → revert。

---

## 不做的事

- ❌ 改 OV 主仓代码 (用户约束)
- ❌ Stage 2 优化 (wall time < 1s)
- ❌ Search/Answer streaming pipeline (Stage 2 太短)
- ❌ 重写 LLM SDK 层加 rate limiter (sophnet 自己限速)

---

## Open Questions

- [ ] sophnet 每 key 真实 RPM/TPM 是多少？(需要 doc 或者实测)
- [ ] OV server 当前用几个 LLM key？是否可配 sophnet keys list?
- [ ] full10 stage 4 5.7hr 是 sophnet 真挂了重试还是稳定 throughput? (查 baseline 那次 sophnet 异常事件)
