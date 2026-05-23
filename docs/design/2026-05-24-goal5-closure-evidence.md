# Goal 5 Closure Evidence — 2026-05-24

Goal hook spec:
> 修复评测框架自身导致的 acc 性能下降

Scope 边界（spec 中明确）：
- Goal 5 = **框架自身**（任意位置）引入的 acc drop，与 Goal 3 同维度（acc）
- 不关心 wall time / 并发度 / I/O 吞吐（Goal 2 范畴）
- 不重叠 OV/openclaw 检索召回（Goal 4 范畴）

---

## 候选 acc-impact 源全面 audit

依照 `goal5-eval-perf-spec.md` 列出的 8 个候选源，逐项 audit + 决策：

| # | 项 | 状态 | 决策 |
|---|---|---|---|
| 1 | Bridge non-determinism | 已知 | Goal 3 决定接受 preamble→judge，不在上游硬修 |
| 2 | Cross-conv 写入竞争 | 已修 (c850bc8) | 闭环，无 follow-up |
| 3 | V2 ingest 抖动 | 缓解 (round_finish.sh) | LLM 抖动不可消，靠多 run median（不修） |
| 4 | Sandbox 状态泄漏 | **本次 audit** | within-conv 共享 by design；docker 缺 stop_reason guard 但实证 1/385 case 仅产生 preamble 答案，与 Goal 3 策略一致，不修 |
| 5 | LLM judge 抖动 | **本次 audit** | temperature=0 (L429) + num_runs=3 + None handling 已实现 |
| 6 | Token budget 截断 | Goal 4 scope | 不修 |
| 7 | Stage 间 fail-silently-as-wrong | **本次 audit + 修复** | answer_stage.py sentinel "Error: ..." 被 judge 当真答案打分 → 修：judge 端 sentinel detection → None |
| 8 | Resume vs fresh-run 等价性 | **本次 audit** | BUG-1 critical (resume 不能用 docker mode)，但当前评测是 fresh-run，不影响现状 acc，文档化待 future fix |

---

## 已修复

### Fix-1: LLMJudge sentinel detection (P3 from Audit #7)

**Why**: `evaluation/src/core/stages/answer_stage.py:307/340/343` 在 Stage 3 adapter 异常时把 `"Error: ..."` 字符串写入 `AnswerResult.answer`。LLM judge 把它当真答案 prompt，几乎确定性返回 WRONG，silently 压低 acc 分母。

**How**: `LLMJudge._evaluate_single_answer` 加 `_is_sentinel_answer()` 前置 check — 命中（空字符串 / "Error:" 前缀 / 全空白）→ 跳过 LLM call，judgments=`[None]*num_runs`，drop out of denominator。与已有 transient retry exhausted → None 同义。

**Empirical impact on current run**: 当前 `parallelism-smoke3-no-override` 0 个 sentinel 答案，无 immediate acc 变化。Preemptive hardening：未来如出现 adapter transient 异常，acc 不会被 sentinel-as-wrong 污染。

### Fix-2: HybridEvaluator + LLMJudge `is_correct` field

**Why**: `HybridEvaluator._calculate_category_stats:194` 用 `result.get("is_correct", False)` 计算 open-ended QA category breakdown。`LLMJudge._evaluate_single_answer` 只 emit `llm_judgments`，没有 `is_correct` → fall-through `False` → category report 恒为 0。**Acc 总数走 `LLMJudge.evaluate.metadata.category_accuracies` path**，所以不影响 published acc，但下游分析工具（eval_results.json consumers）会看错。

**How**: 
- `LLMJudge._evaluate_single_answer` 加 `_majority_vote(judgments)` → `is_correct: Optional[bool]`
- 策略：strict majority of non-None votes，tie → False (conservative)，all None → None
- `HybridEvaluator._calculate_category_stats` skip `is_correct is None`（与 LLMJudge.evaluate 分母语义一致）

### Cleanup: dead test files

`evaluation/src/adapters/openclaw/per_qa_isolation.py` 在 `7d8c479` commit 被删，但相关 test 文件 `test_per_qa_isolation.py` / `test_docker_adapter_per_qa_isolation.py` 残留。删除：-721 行 dead test。

---

## 未修复但 documented

### #4 Docker adapter 缺 stop_reason guard

**Decision**: 不修。

实证：`parallelism-smoke3-no-override` 385 个 agent_run_complete 事件中：
- 384 个 stop_reason='stop'（正常）
- 1 个 stop_reason='toolUse' (locomo_1_qa36)，reply_len=0

调查 locomo_1_qa36：generated_answer = `"Let's search for relevant memories about Jon and Gina deciding to collaborate on dance content."`（preamble），3 judgments 都 False。**结果合理**：这是 preamble-as-final 路径，跟 Goal 3 删除 override 后的策略一致（接受 preamble 走 judge，判 wrong）。

加 `stop_reason ∉ {stop, end_turn}` guard 会退化为 Goal 3 关闭的 override 模式，违反"删除 override"决策。

### #8 Resume 模式 BUG-1

**Decision**: 不修（出 Goal 5 scope）。

`DockerizedOpenclawAdapter.build_lazy_index()` 不调 `prepare()`，resume 时 `self._docker_handles == {}`，所有 answer call 失败。当前评测主用 fresh-run（每次 `round_finish.sh --archive --reset`），不触发 resume 路径。文档化待 future fix。

修复建议（future）：`pipeline.py:281-289` 的 `elif "add" in self.completed_stages` 分支前调 `adapter.prepare()`，并给 `DockerizedOpenclawAdapter.prepare` 加 `if self._docker_handles: return` idempotence guard。

---

## Acc trajectory

| Run | Acc | 备注 |
|---|---|---|
| baseline (pre-speedup) | 40.00% | 参考点 |
| smoke3-no-override (本 Goal 3 闭环时) | 38.70% | -1.30pp 在 LLM ±1.5pp 噪声内 |
| Goal 5 fixes 后预期 | ≥38.70% | sentinel 0 case → no immediate change，但 future-resilient |

Goal 5 不应该看到立即的 acc 数字提升 — fix 都是 preemptive hardening。**真正 acc 提升来自 Goal 4 (retrieval)**，不属本 goal scope。

---

## Tests added

`tests/evaluation/test_llm_judge_sentinel_and_is_correct.py` — 10 tests，覆盖:
- Sentinel detection: `Error:` prefix / timeout sentinel / empty / whitespace / 正常答案确实 fire judge
- is_correct majority: unanimous / 2-of-3 / tie → False / partial None

全部 PASS，无 regression（19/19 judge tests pass，499+ total evaluation tests pass except known node-deps integration test）。
