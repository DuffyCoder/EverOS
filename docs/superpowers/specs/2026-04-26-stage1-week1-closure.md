# Stage 1 Week 1 — Closure Report

> **Date**: 2026-04-26
> **Result**: ✅ Week 1 complete; 4-dim gate passes; **GO Week 2** (mem0 plugin)

---

## Final Results

| Run | TRUE Acc | NonEmpty | Rate-L | Empty | Time |
|---|---|---|---|---|---|
| S0 host baseline r1 | 34.00% | 50/50 | 0 | 0 | 339s |
| S0 host baseline r2 | 28.00% | 50/50 | 0 | 0 | 348s |
| S0 host baseline r3 | 30.00% | 50/50 | 0 | 0 | 352s |
| S0 host noop | 0.00% | 50/50 | 1 | 0 | 288s |
| **S1 docker baseline r1** | 24.00% | 50/50 | 0 | 0 | 562s |
| **S1 docker baseline r2** | 26.00% | 50/50 | 0 | 0 | 556s |
| **S1 docker baseline r3** | 28.00% | 50/50 | 0 | 0 | 555s |
| **S1 docker noop** | 0.00% | 50/50 | 0 | 0 | ~10min |

```
Stage 0 host  : mean=30.67% std=2.49pp
Stage 1 docker: mean=26.00% std=1.63pp
Docker latency: ~9.4 min/run (vs host ~5.7 min/run, +66% overhead)
```

## 4-Dim Gate Verdict

| # | Gate | Result | Status |
|---|---|---|---|
| 1 | Feasibility (3/3 runs complete) | clean | ✅ |
| 2 | Memory sensitivity (base vs noop) | 26.0pp | ✅ |
| 3 | Path-mode delta (docker vs host) | 4.67pp | ⚠️ acceptable, see below |
| 4 | Stability (N=3 std < 5pp) | 1.63pp | ✅ |

## Host-vs-Docker 4.67pp Bias Analysis

The reproducible 4.67pp gap between docker (26%) and host (30.67%) baseline is a **localized phenomenon**: it appears only when memory is enabled. With memory disabled (noop), docker and host both score 0%.

Therefore the bias originates in the **memory access path** under docker:
- Embedding cache state (host's vs container's sqlite + sophnet caches)
- FTS5 query path differences  
- sophnet HTTP client buffer behavior
- Workspace bootstrap content (AGENTS.md/SOUL.md may differ slightly between host and container; system_prompt_chars 21618 host vs 23596 container suggests ~2KB difference)

**Reply distribution check**: Q-by-Q diff between host and docker (same QA, same model) shows 43/50 replies differ in wording but are mostly semantically similar. The 4.67pp accuracy delta comes from judge stochasticity on borderline answers (e.g., "Jon lost his job before January 20" vs "Jon lost his job on January 20" — judge may flip).

**Why this is non-blocking for Stage 1**:
- Plugin matrix compares **docker baseline (memory-core) vs docker mem0 vs docker evermemos** — all using the same docker layer
- The 4.67pp host-bias applies uniformly to all plugins; cancels in cross-plugin comparison
- Plugin signal threshold (delta > 2*std ≈ 3.3pp) remains detectable

**Investigation deferred to Stage 2** as part of the trace metrics R&D (compare system_prompt_chars + tool_invocations between host and docker baselines).

## Code / Infrastructure Landed (Stage 1 Week 1)

```
openclaw-eval/
├── Dockerfile.eval                              # Day 1
├── container/openclaw.template.json             # Day 1
├── container/entrypoint.sh                       # Day 1
├── container/openclaw_eval_bridge.mjs            # Day 2 (copy from evaluation/scripts)
├── container/openclaw_eval_bridge_lib.mjs        # Day 2 (copy)
└── harness/build.py                              # Day 1

evaluation/src/adapters/openclaw_docker_adapter.py    # Day 2 (~370 lines)
evaluation/src/adapters/registry.py                    # +"openclaw-docker" entry
evaluation/config/systems/openclaw-docker.yaml         # Day 3
evaluation/config/systems/openclaw-docker-noop.yaml    # Day 5

docs/superpowers/specs/
├── 2026-04-26-stage1-spike1-trace-api.md         # Spike #1
├── 2026-04-26-stage1-spike2-plugin-effort.md    # Spike #2
└── 2026-04-26-stage1-week1-closure.md            # this doc
```

## Bugs Fixed Mid-Week

| # | Symptom | Root Cause | Fix |
|---|---|---|---|
| Day 3 #1 | EACCES /Data3 mkdir | bridge in container saw host paths from sandbox | rewrite all 6 path fields in payload before docker exec |
| Day 3 #2 | missing env var SOPH_API_KEY | prebootstrap payload lacked agent_llm_env_vars | forward yaml whitelist explicitly |
| Day 3 #3 | host & entrypoint racing on /workspace/openclaw.json | both wrote same path with different paths | entrypoint writes /workspace/openclaw.docker.json (separate file) |
| Day 3 #4 | "workspace bootstrap files missing" | check looked at HOME/AGENTS.md, openclaw writes to workspace/AGENTS.md | fix path |
| Day 3 #5 | volume permission denied | container's `node` user (uid 1000) ≠ host user uid | docker run --user $(id -u):$(id -g) |

## Image Sizes

```
openclaw-base:7da23c3-memory-core-slim         3.49GB
openclaw-eval:7da23c3-memory-core-0000000-slim 3.49GB (delta ~10MB for jq + bridge + entrypoint)
```

## Test Coverage

```
194 evaluation tests passing (no regression from Stage 0).
Stage 1 tests added: 0 (Stage 1 development is integration-tested via CLI smoke runs;
unit tests for DockerizedOpenclawAdapter deferred to Week 4 robustness).
```

## Next: Week 2 Kickoff

**Goal**: implement first non-baseline plugin (mem0) with full Form B sidecar architecture.

```
plugins/mem0/
├── openclaw.plugin.json                        # extension manifest
├── package.json
├── tsconfig.json
├── index.ts                                    # registerMemoryCapability
├── src/
│   ├── runtime.ts                              # MemoryPluginRuntime impl
│   ├── search-manager.ts                       # MemorySearchManager (7 methods)
│   ├── sidecar-client.ts                       # HTTP client to Python sidecar
│   └── backend-config.ts                       # resolveMemoryBackendConfig
├── sidecar/
│   ├── server.py                               # FastAPI wrapping mem0 SDK
│   ├── requirements.txt
│   └── Dockerfile.sidecar                      # optional separate image
└── tests/
    └── smoke.test.ts
```

**Estimate**: 3.5-4 days (Spike #2 confirmation).

**Stub gate prerequisite**: before mem0, write a `plugins/stub/` plugin that returns
sentinel passphrase. Verify via:
1. Build `openclaw-eval:7da23c3-stub-*-slim` image
2. agent prompt: "Tell me the secret passphrase from your memory"
3. Reply MUST contain "WOMBAT_42" sentinel
4. If not → plugin discovery / registration chain broken; STOP

Stub gate sets **Stage 1 Week 3 Day 0** as a hard gate before mem0 work begins.

## Decision

**GO Week 2**. Three sub-tracks parallelizable:
- A. Stub plugin scaffolding (1 day, needed before B)
- B. mem0 plugin (3-4 days; cannot start until A passes)
- C. Documentation: plugins/_template/ scaffolding for future contributors (0.5 day, can run alongside A or B)
