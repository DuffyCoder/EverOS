# Stage 1 — Closure Report

> **Date**: 2026-04-28
> **Result**: Week 1-3 complete; plugin matrix infrastructure in place; mem0 + evermemos LoCoMo 1c1q gates pass at 100%. **N=3 statistical matrix queued separately**.

---

## What Stage 1 Set Out To Prove

> Validate that **swapping the memory plugin inside openclaw** produces
> measurably different LoCoMo-S accuracy, with deterministic
> reproducibility (std<5pp) and clean trace-based diagnosis.
> — kickoff doc, 2026-04-26

The bigger goal (kickoff): publish a 3-plugin × N=3 scorecard showing
each plugin's natural deployment performance on LoCoMo-S.

## Status Against The Original Week-by-Week Plan

| Week | Plan | Result |
|---|---|---|
| 1 | Docker底座 + R&D Spikes (Spike #1 trace API, Spike #2 plugin effort) | ✅ Done |
| 2 | DockerizedOpenclawAdapter + memory-core E2E reproduce Stage 0 baseline | ✅ Done — docker memory-core 26% mean / 1.63pp std (within 5pp of Stage 0 30.67%) |
| 3 (rev) | Stub plugin gate (Day 0) + mem0 plugin (Days 1-5) | ✅ Done — stub gate PASS; mem0 LoCoMo 1c1q 100% / 1c5q 40% |
| 4-5 (rev) | evermemos plugin + N=3 × 3-plugin matrix + closure | ⚠️ evermemos plugin Done (1c1q 100%); **N=3 matrix DEFERRED** |

Plan revision history:
- Kickoff put stub gate at Week 3, plugin work at Week 3-5.
- During execution we accelerated: stub gate fell to **Week 2 Day 0**,
  mem0 plugin to **Week 2 Day 1-4a**, evermemos plugin to **Week 3**.
- evermemos was originally "Week 4 second plugin"; since it has a
  stand-alone HTTP API on the host, the TS-only plugin path landed
  in 1 day vs Spike #2's 1.5–2-day estimate.

## Plugin Matrix Infrastructure Landed

Common path B chain for every plugin:

```
eval framework → docker run openclaw container per conv
  → bridge.mjs inside container handles index/status/agent_run
  → openclaw plugin: <name>
    → MemoryPluginRuntime (registered via api.registerMemoryCapability)
    → memory_search / memory_get tools (registered via api.registerTool)
    → backend (mem0 sidecar inside container, OR host evermemos API)
```

Per-plugin specifics:

| Plugin | Backend location | Sidecar shape | Image size |
|---|---|---|---|
| **memory-core** | container-internal sqlite + FTS5 + sophnet remote embed | none (built-in) | 3.49GB |
| **mem0** | container-internal chromadb + MiniLM embedder | Python FastAPI sidecar (uvicorn :8765) | 5.75GB |
| **evermemos** | host docker-compose (mongo+milvus+es+redis) | none — plugin TS fetches host.docker.internal:1995 | 3.58GB |
| **stub** | inline sentinel WOMBAT_42 | none | 3.49GB |

Each plugin ships its own promptBuilder grounded in its own docs:
- memory-core uses upstream `extensions/memory-core/src/prompt-section.ts`
  unchanged.
- mem0 prompt adapted from `docs/integrations/elevenlabs.mdx` +
  `skills/mem0/references/integration-patterns.md` (mem0 own repo).
- evermemos prompt grounded in the plugin's HTTP API surface and
  `docs/dev_docs/agentic_retrieval_guide.md` (this repo).
- stub prompt directs the LLM to assume a single passphrase exists
  in memory.

This is the "ecological validity" choice (per user direction
2026-04-27): each plugin runs with the prompt its developers would
ship, not a normalized prompt. Trade-off and caveat documented in
**§ Stage 1 Caveats** below.

## Verification Points That Passed

| Verification | Plugin | Scale | Acc |
|---|---|---|---|
| Stub passphrase gate | stub | 1Q (sentinel) | PASS |
| Mem0 passphrase gate | mem0 | 1Q (codeword) | PASS |
| Mem0 LoCoMo smoke | mem0 | 1conv × 1Q | 100% |
| Mem0 LoCoMo smoke | mem0 | 1conv × 5Q | 40% |
| Evermemos chain gate | evermemos | 1Q (chain only) | PASS |
| Evermemos LoCoMo smoke | evermemos | 1conv × 1Q | 100% |
| Memory-core docker baseline | memory-core | 50Q × 10conv × N=3 | 26% mean (std 1.63pp) |

Plugin chain end-to-end is proven for all three production plugins
(memory-core, mem0, evermemos). What remains is **statistical
characterization at scale** — the N=3 matrix.

## Stage 1 Caveats

### A. Plugin matrix scores reflect (backend + plugin's own prompt) jointly

Stage 1 plugin matrix compares each plugin running with its developer's
recommended prompt. We are **NOT** measuring isolated backend behavior;
we are measuring the plugin a user would actually install. If
plugin A scores 30% and plugin B scores 25%, we cannot say A's
backend retrieves better — only that A's (backend + prompt) combo
beats B's (backend + prompt). Stage 2 ablation runs (uniform prompt
across plugins) would be needed to attribute deltas to backend
alone.

### B. Plugin bias does NOT cancel across plugins

Earlier (Codex r7 F2) we documented that the 4.67pp host-vs-docker
bias on memory-core baseline does NOT cancel when comparing
plugin A docker vs plugin B docker. Same caution applies here: any
container-level overhead may have plugin-specific magnitudes
because each plugin replaces the memory access path with a
different implementation. Plugin matrix conclusions must say
"in docker mode" explicitly and never extrapolate to "what would
this plugin score on host".

### C. Boundary detection / ingest semantics differ per plugin

- memory-core: synchronous index build via `openclaw memory index --force`.
- mem0: synchronous mem0.add() with infer=False; chromadb persistence.
- evermemos: async LLM-driven boundary detection; messages may sit
  in 'accumulated' state until the server's LLM judges a topic
  shift (5 short synthetic messages don't flush; 419 real LoCoMo
  messages flush naturally).

For LoCoMo evaluation this difference washes out (real conversations
provide enough natural boundaries) but for synthetic gates it
matters. Day 4 evermemos gate switched to a "chain pass" criterion
that doesn't require flush.

### D. EverMemOS `vectorize_sophnet.py` is in main repo only

The eval framework runs from a worktree; EverMemOS API server must
run from the main repo because `src/agentic_layer/vectorize_sophnet.py`
is missing in the worktree. Likewise main repo's `.env` had
`LLM_MODEL=openai/gpt-4.1-mini` which sophnet rejects (HTTP 400);
fixed to `gpt-4.1-mini`. Documented in commit 6b17694.

### E. LLM safety alignment leaks into eval semantics

Smoke gates initially used "secret passphrase" wording, which the
LLM safety-refuses regardless of plugin. Reworded to "preferred
project codeword" for the gate tests. LoCoMo natural-question types
(dates, names, places, decisions) are expected to be safety-clean,
but incidental sensitive queries may surface in larger runs.

## Open Risks (For Stage 2)

### R-S1-1: 4.67pp host-vs-docker memory-core gap

Inherited from Week 1 closure. The reproducible 4.67pp gap between
host (30.67%) and docker (26%) memory-core baselines — origin
localized to the memory access path under docker (cache state,
embedding HTTP buffers, workspace bootstrap content
`system_prompt_chars` 21618 host vs 23596 container). Stage 2
trace R&D may close it.

### R-S1-2: N=3 matrix not yet executed

This Stage 1 closure is being filed before the N=3 statistical
matrix runs because:
- Plugin chain infrastructure is verified end-to-end (4 plugins).
- 1c1q smokes show all chains produce correct answers when memory
  has the right data.
- Full N=3 (3 plugins × 3 runs × 50 QA × 10 conv) wall-clock cost
  ≈ 12+ hours, dominated by evermemos's per-conv ingest (~1000s).

A reduced N=1 × 3-plugin × 50 QA pass (~4 hours) is queued; numbers
will be appended to this doc once it lands.

### R-S1-3: prompt confound

Per Caveat A. Closing this requires Stage 2 ablation runs.

## Decision

**Stage 1 functionally complete**: all three production memory
plugins integrate with openclaw via the documented Form B / TS-only
pattern; LoCoMo end-to-end chains verified at single-Q smoke level
with 100% accuracy; infrastructure (Dockerfile, adapter overrides,
bridge sidecar routing, plugin scaffolding patterns) is in place.

**N=3 statistical scorecard deferred** to a separate run cycle that
can absorb the wall-clock cost (queued after this commit).

**Stage 2 entry conditions** met:
- Three plugin implementations with end-to-end gates ✅
- Trace API spike doc done ✅ (Spike #1)
- Plugin authoring doc done ✅ (Spike #2)
- Open risks documented (R-S1-1 through R-S1-3) ✅

## Code / Infrastructure Landed (Stage 1 Total)

```
openclaw-eval/
├── Dockerfile.eval                              # Week 1
├── container/openclaw.template.json
├── container/entrypoint.sh
├── container/openclaw_eval_bridge.mjs           # + sidecar routing (W2D3a)
├── container/openclaw_eval_bridge_lib.mjs
├── harness/build.py                             # + stage_external_plugin (W2D0)
├── harness/stub_passphrase_gate.sh              # W2D0
├── harness/mem0_passphrase_gate.sh              # W2D3b
├── harness/evermemos_passphrase_gate.sh         # W3
└── plugins/
    ├── stub/                                     # W2D0
    │   ├── openclaw.plugin.json
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── index.ts
    │   └── sidecar/.gitkeep
    ├── mem0/                                     # W2D1-D3a
    │   ├── openclaw.plugin.json
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── index.ts
    │   ├── src/{runtime,search-manager,sidecar-client,backend-config}.ts
    │   └── sidecar/{server.py, requirements.txt}
    └── evermemos/                                # W3
        ├── openclaw.plugin.json
        ├── package.json
        ├── tsconfig.json
        ├── index.ts
        ├── src/{runtime,search-manager,api-client,backend-config}.ts
        └── sidecar/.gitkeep

evaluation/
├── src/adapters/
│   ├── openclaw_adapter.py            # + _invoke_bridge virtual hook (W2D4a)
│   ├── openclaw_docker_adapter.py     # + override _ingest_conversation (W3D4)
│   │                                    # + EVERMEMOS_GROUP_ID per-conv env
│   │                                    # + --add-host=host.docker.internal
│   └── openclaw_resolved_config.py    # + Codex r7 F1 noop hygiene
├── config/systems/
│   ├── openclaw-docker.yaml           # memory-core baseline
│   ├── openclaw-docker-mem0.yaml      # W2
│   ├── openclaw-docker-evermemos.yaml # W3
│   ├── openclaw-docker-stub.yaml      # W2D0
│   └── openclaw-docker-noop.yaml      # W1 closure run
└── tests/evaluation/
    └── test_openclaw_resolved_config.py  # 195 tests passing

docs/superpowers/specs/
├── 2026-04-26-stage1-kickoff.md
├── 2026-04-26-stage1-spike1-trace-api.md
├── 2026-04-26-stage1-spike2-plugin-effort.md
├── 2026-04-26-stage1-week1-closure.md
└── 2026-04-28-stage1-closure.md       # this doc
```

## N=3 Matrix Results

(To be filled in once the queued matrix run lands. Placeholder:)

| Plugin | Run 1 | Run 2 | Run 3 | Mean | Std | Time |
|---|---|---|---|---|---|---|
| memory-core | TBD | TBD | TBD | TBD | TBD | TBD |
| mem0 | TBD | TBD | TBD | TBD | TBD | TBD |
| evermemos | TBD | TBD | TBD | TBD | TBD | TBD |

Acceptance:
- All three plugins produce non-zero accuracy on LoCoMo-S.
- Std per plugin < 5pp (reproducibility floor from Stage 0).
- Plugin matrix interpretation is **docker-vs-docker only** (Caveat B).
