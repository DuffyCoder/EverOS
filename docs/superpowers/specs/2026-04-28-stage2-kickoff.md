# Stage 2 — Kickoff

> **Date**: 2026-04-28
> **Stage 1 status**: ✅ Functional close (commit be19e50). Plugin matrix
> N=1 partial: memory-core 23.78% / evermemos 34.67% / mem0 (5Q sample)
> 40%. Three open risks (R-S1-1, R-S1-2, R-S1-3) carried forward.

## Goal

Close the open risks from Stage 1 closure into a **defensible
Stage 1.5 scorecard** that an external reviewer can read without
having to add caveats.

Specifically:
1. Statistical completeness — every plugin has N≥2 runs at full 50Q.
2. Prompt confound resolved — backend contribution separable from
   prompt contribution.
3. Host-vs-docker memory-core 4.67pp gap root-cause identified or
   closed.

## Tracks

### Track A — Statistical Completeness (close R-S1-2)

**Goal**: every plugin has N=2+ at full 50Q (LoCoMo-S category-5).

| Step | What | Wall-clock |
|---|---|---|
| A.1 | Rebuild `openclaw-eval:7da23c3-mem0-*-slim` image (was purged) | ~50 min |
| A.2 | mem0 50Q run #1 | ~30 min |
| A.3 | mem0 50Q run #2 | ~30 min |
| A.4 | evermemos 50Q run #2 | ~4 h |
| A.5 | (optional) memory-core 50Q run #4 — for trace anchor | ~10 min |
| A.6 | Update closure doc with N=2 numbers + std | ~15 min |

Acceptance: each plugin has at least 2 runs at full 50Q; per-plugin
std reported.

### Track B — Prompt Ablation (close R-S1-3)

**Goal**: separate backend contribution from prompt contribution.

Approach: **swap promptBuilders so each plugin runs once with another
plugin's prompt**. Specifically:

| Plugin | Native prompt | Ablation prompt |
|---|---|---|
| memory-core | upstream extension prompt | mem0-style prompt |
| mem0 | mem0 docs prompt | memory-core upstream prompt |
| evermemos | evermemos docs prompt | memory-core upstream prompt |

Each ablation run: 50Q × N=1.

| Step | What | Wall-clock |
|---|---|---|
| B.1 | Add config knob to each plugin: `promptStyle: "native" | "memory-core" | "mem0"` | ~1 h coding |
| B.2 | Build 3 ablation images (or pass via env) | ~10 min if env-driven |
| B.3 | Run 3 plugins × 1 ablation prompt × 50Q | ~5 h total (evermemos dominates) |
| B.4 | Side-by-side: native vs ablation per plugin | ~30 min analysis |
| B.5 | Update closure with ablation table | ~15 min |

Acceptance: report can answer "if all plugins used the same prompt,
plugin X scores Y" — even if rough. Distinguishes backend retrieval
quality from prompt tuning.

### Track C — Host-vs-Docker Bias Root Cause (close R-S1-1)

**Goal**: Stage 1 Week 1 closure observed 4.67pp gap between host
memory-core (30.67%) and docker memory-core (~26%). Open risk says
"Stage 2 trace R&D may close it".

| Step | What | Wall-clock |
|---|---|---|
| C.1 | Capture host vs docker bootstrap diff: `meta.systemPromptReport.systemPrompt.chars` (21618 host vs 23596 container) | ~30 min |
| C.2 | Capture cache-state diff: vector cache, FTS cache, embedding HTTP buffers across runs | ~1 h |
| C.3 | If C.1/C.2 explain it: doc + close R-S1-1 | ~30 min |
| C.4 | If not: enumerate remaining hypotheses, defer to Stage 3 | ~30 min |

Acceptance: either R-S1-1 closed with a documented cause, OR a
narrowed-down list of remaining hypotheses.

## Tracks **NOT** in Stage 2

- Adding more plugins (zep, memos, memu) — Stage 3 backlog
- Trace API instrumentation beyond what's needed for C.1/C.2 —
  Stage 3
- Per-question retrieval-quality metric (precision@K, recall@K) —
  Stage 3 (current scorecard is end-to-end LLM-judge accuracy
  only; finer-grained metrics need separate eval harness work)

## Execution Order

A first (closes the largest open risk and produces N=2 numbers
quickly), then B (ablation needs A's stable image set), then C
(diagnostic, doesn't block scorecard).

## Stage 2 Budget

Total wall-clock estimate: **~13 hours active** (a/b serially) +
some scaffolding/analysis. Realistic over 1-2 calendar days if
runs are batched.

## Commit / Push Cadence

Each Track step that lands new data or code lands its own commit
during execution. Closure doc updated incrementally so reviewers
can see partial progress.
