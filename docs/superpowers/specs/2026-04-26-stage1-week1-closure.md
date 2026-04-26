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

## Host-vs-Docker 4.67pp Bias — Open Risk

The reproducible 4.67pp gap between docker memory-core (26%) and host memory-core (30.67%) is a **localized phenomenon**: it appears only when memory is enabled. With memory disabled (noop), docker and host both score 0%.

The bias originates in the **memory access path** under docker:
- Embedding cache state (host vs container sqlite + sophnet caches)
- FTS5 query path differences
- sophnet HTTP client buffer behavior
- Workspace bootstrap content (system_prompt_chars 21618 host vs 23596 container suggests ~2KB difference in AGENTS.md/SOUL.md/etc)

**Reply distribution check**: Q-by-Q diff between host and docker (same QA, same model) shows 43/50 replies differ in wording, mostly semantically similar but with date interpretation flips ("before Jan 20" vs "on Jan 20") that change judge verdicts.

### Important caveat (Codex r7 F2): bias does NOT cancel across plugins

Earlier framing claimed the 4.67pp bias "applies uniformly to all plugins and cancels in cross-plugin comparison". **That claim is unsupported**. Different memory plugins (mem0, evermemos, zep) replace the memory access path with different implementations. There is no reason to assume the docker overhead has the same magnitude across them.

Concrete risk: plugin A (mem0) might have a docker-vs-host gap of -2pp; plugin B (evermemos) might be -8pp. Comparing docker mem0 (24%) to docker evermemos (22%) would imply mem0 wins by 2pp, when in reality their host accuracies might be 26% vs 30% (evermemos winning by 4pp). **Plugin matrix interpretation must be docker-vs-docker only**, never extrapolate to "what would this plugin score on host".

### Mitigation strategy

1. **Stage 1 conclusions are docker-internal**: every plugin claim must say "in docker mode" explicitly
2. **No host-vs-docker cross-comparison** in Stage 1 reports
3. **Stage 2 trace R&D**: investigate the 4.67pp source via `meta.systemPromptReport.systemPrompt.chars` diff between host and docker baselines; if root cause is purely workspace bootstrap content, Stage 2 may close the gap by aligning bootstrap behavior

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

**Goal**: pass the stub plugin gate, then implement first non-baseline plugin (mem0).

### Week 2 Schedule (Codex r7 F3 fix: stub gate is Week 2 Day 0, not Week 3)

```
Day 0  Stub plugin gate (HARD GATE — mem0 cannot start until passing)
       - plugins/stub/ scaffolding (~0.5 day)
       - Build openclaw-eval:7da23c3-stub-<rev>-slim image
       - Run: agent prompt "Tell me the secret passphrase from memory"
       - PASS: reply contains "WOMBAT_42"
       - FAIL: plugin discovery/registration chain broken → STOP, debug

Day 1-4  mem0 plugin (3.5-4 days per Spike #2)
       plugins/mem0/
       ├── openclaw.plugin.json
       ├── package.json
       ├── tsconfig.json
       ├── index.ts                    # registerMemoryCapability
       ├── src/
       │   ├── runtime.ts              # MemoryPluginRuntime impl
       │   ├── search-manager.ts       # MemorySearchManager (7 methods)
       │   ├── sidecar-client.ts       # HTTP client to Python sidecar
       │   └── backend-config.ts       # resolveMemoryBackendConfig
       ├── sidecar/
       │   ├── server.py               # FastAPI wrapping mem0 SDK
       │   ├── requirements.txt
       │   └── Dockerfile.sidecar      # optional separate image
       └── tests/

Day 5  mem0 N=3 LoCoMo-S smoke + scorecard delta vs docker baseline (26%)
       Acceptance: |delta| > 2*std (≈ 3.3pp) → real plugin signal
```

**Parallel side-track**: `plugins/_template/` scaffolding doc (0.5 day, can be authored
during Day 0 or alongside Day 1 boilerplate work).

## Decision

**GO Week 2** with Day 0 stub gate as hard prerequisite. mem0 work blocked
until passphrase test passes (≤ 1 day). evermemos plugin (1.5-2 days per Spike
#2) deferred to Week 3.
