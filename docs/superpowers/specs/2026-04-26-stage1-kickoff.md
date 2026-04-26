# Stage 1 Kickoff — Docker-as-Adapter + Multi-Plugin Matrix

> **Date**: 2026-04-26
> **Owner**: this worktree (`claude/reverent-tharp-f8f082`)
> **Stage 0 status**: ✅ FULLY CLOSED (see `2026-04-24-memory-eval-framework-d4-d5-findings.md`)
> **Estimated window**: 4-5 weeks

---

## Stage 0 Outputs Carried Forward

✅ Path B baseline accuracy: **30.67% ± 2.49pp** (LoCoMo-S 50 QA, conc=4)
✅ Path A baseline:           30.67% (sanity check, ~0pp delta from B)
✅ Memory sensitivity:        **30.67pp** vs noop (plugin matrix has signal)
✅ Bridge / resolved_config / adapter / metrics — all 194 tests pass
✅ Concurrency configurable, secret hygiene clean, sandbox lookup wired
✅ Adapter robust to provider rate-limit (treats `stop_reason=error` as failure)

---

## Stage 1 Goal

> Validate that **swapping the memory plugin inside openclaw** produces
> measurably different LoCoMo-S accuracy, with deterministic
> reproducibility (std<5pp) and clean trace-based diagnosis.

Concretely: produce a 3-plugin × N=3 scorecard:
- `memory-core` (baseline, openclaw bundled)
- `mem0` (Form B, Python sidecar)
- `evermemos` (Form B, Python sidecar)

Optional Stage 1.5: hermes-holographic if time/effort allows.

---

## Week-by-Week Plan

### Week 1 — Docker底座 + R&D Spikes (1 week)

**Track A: Docker底座**
- [ ] `Dockerfile.eval` — copy from openclaw native Dockerfile, add ARG MEMORY_PLUGIN
- [ ] `container/entrypoint.sh` — render openclaw.json from MEMORY_PLUGIN_ID env, branch on memory_mode
- [ ] `container/openclaw.template.json` — based on D1-validated minimal config
- [ ] `harness/build.py` — batch build images for memory matrix
- [ ] Smoke: build memory-core image, run agent_run via docker exec, verify reply

**Track B: R&D Spike #1 — Openclaw trace capability**
- [ ] Run `openclaw agent --local --verbose on` and parse stderr
- [ ] Check if `--log-level=debug` exists; if so, schema for tool-call events
- [ ] Investigate if hook system (`api.on`) can capture events from outside
- [ ] **Decision output**: can Stage 1+ trace tool-call sequences?

**Track C: R&D Spike #2 — MemoryPluginRuntime real effort**
- [ ] Read full `extensions/memory-core/index.ts` to map runtime impl
- [ ] Read `MemorySearchManager` impl in memory-core (sqlite + sophnet embed)
- [ ] Estimate effort to wrap mem0/evermemos as a plugin runtime
- [ ] **Decision output**: confirm 3-5 days/plugin estimate or revise Stage 1 scope

**Week 1 milestone**: docker memory-core baseline runs end-to-end via `evaluation.cli`. R&D spike #1 + #2 conclusions documented.

### Week 2 — DockerizedOpenclawAdapter + memory-core E2E

- [ ] `evaluation/src/adapters/openclaw_docker_adapter.py`
  - [ ] Inherits BaseAdapter, registered as `openclaw-docker`
  - [ ] `prepare()` — spawn N detached containers (per-conv)
  - [ ] `add()` — write session.md to volume, exec `openclaw memory index`
  - [ ] `search()` — pass-through skipped (agent_local owns retrieval)
  - [ ] `answer()` — exec `openclaw agent --local` inside container
  - [ ] `cleanup()` — stop+rm containers
- [ ] Reuse evermemos `_sandbox_by_conversation_id` pattern
- [ ] `evaluation/config/systems/openclaw-docker.yaml` — image tag + memory_plugin
- [ ] Smoke: LoCoMo-S 1 conv × 5 Q via docker baseline
- [ ] Side-by-side compare: `openclaw-docker` vs Stage 0 `openclaw-agent-local` accuracy
  - Expectation: same numbers ± noise

**Week 2 milestone**: docker baseline reproduces Stage 0 30.67% within 3pp.

### Week 3 — Stub plugin gate (Day 0) + mem0 plugin

**Day 0: Stub plugin gate**
- [ ] `plugins/stub/index.ts` — registers minimal `MemoryPluginRuntime`
- [ ] `MemorySearchManager` returns single sentinel hit `THE_PASSPHRASE_IS_WOMBAT_42`
- [ ] Build `openclaw-eval:7da23c3-stub-*-slim` image
- [ ] Test: agent prompt "Tell me the secret passphrase" → reply MUST contain "WOMBAT_42"
- [ ] Gate: passing means plugin discovery + load + use chain works end-to-end

If stub gate fails → STOP, debug. Don't move on to real plugin.

**Day 1-5: mem0 plugin**
- [ ] `plugins/mem0/sidecar/server.py` — FastAPI wrapping mem0 Python SDK
  - Endpoints: /index /search /read_file /status /probe_embedding /probe_vector /healthz /sync /close
- [ ] `plugins/mem0/index.ts` — TS plugin, registerMemoryCapability with runtime that fetches sidecar
- [ ] `plugins/mem0/Dockerfile.sidecar` (or single image dual-process)
- [ ] Build `openclaw-eval:7da23c3-mem0-*-slim`
- [ ] Smoke: 1 conv × 5 Q with mem0
- [ ] Compare to memory-core baseline accuracy

**Week 3 milestone**: mem0 plugin produces non-zero (and non-baseline) accuracy.

### Week 4-5 — Second plugin + N=3 matrix + robustness

- [ ] evermemos plugin (similar pattern to mem0)
- [ ] OR zep plugin (alternative if evermemos integration is heavy)
- [ ] Run full matrix: 3 plugin × LoCoMo-S × N=3 = 9 runs
- [ ] Apply concurrency=4 from Stage 0
- [ ] Compute scorecard with mean ± std per plugin
- [ ] Verify: each plugin's accuracy distinct from memory-core baseline

**Container robustness checks**:
- [ ] mem_limit=2g per container — verify no OOM at concurrent=4
- [ ] timeout per docker exec — match `agent_timeout_seconds + 30`
- [ ] Cleanup ALWAYS runs even on harness exception

**Week 4-5 milestone**: 3-plugin × N=3 scorecard published.

---

## Acceptance Criteria (Stage 1 DoD)

- [ ] Docker images build deterministically with reproducibility key tag
- [ ] Stub plugin gate passes (passphrase in reply)
- [ ] At least 2 non-baseline plugins (mem0 + evermemos) produce different
      accuracy from memory-core (delta > 5pp in either direction)
- [ ] N=3 std across all conditions < 5pp
- [ ] No regression in 194 existing tests
- [ ] Adapter handles all openclaw subprocess failure modes (rate limit,
      timeout, OOM, plugin load fail) without polluting accuracy
- [ ] R&D spike #1 (trace) outcome: either tool-call trace landed in
      `agent_run_complete` event, or explicit "not feasible in v1" doc
- [ ] R&D spike #2 (plugin effort) outcome: confirmed/revised effort doc

---

## Risk Mitigations Inherited from Stage 0

| Risk | Stage 0 Status | Stage 1 Application |
|---|---|---|
| Provider rate limit | ✅ conc=4 + stop_reason guard | Apply same to docker yaml |
| Empty reply judge bug | ⚠️ noted, not fixed | Watch for in 9-run matrix; add `is_empty_pre_judge` guard if seen |
| Workspace bootstrap race | ✅ prebootstrap dummy run | Run inside docker too |
| Sandbox persistence | ✅ add() + build_lazy_index() | New docker adapter follows pattern |
| Schema drift | ✅ ModelDefinition / plugins.allow / SecretInput | Reused in docker entrypoint |

---

## Stage 1 Kickoff Checklist (today)

- [x] Stage 0 D4-D5 findings doc written
- [x] All adapter changes landed + tested (194 passing)
- [x] Concurrency configurable + sophnet-friendly default
- [ ] Pick first plugin (recommend **mem0** — most mature SDK)
- [ ] Allocate dev environment (docker engine, image registry)
- [ ] Schedule R&D spikes
- [ ] Open PR for Stage 0 closure (this worktree branch claude/reverent-tharp-f8f082)

---

## Open Questions (for Week 1 R&D)

1. **Plugin SDK import path** — does `plugin-sdk/memory-host-files` actually export `MemorySearchManager`? Confirmed by reading source but not by compile.
2. **`MemoryPluginRuntime` work effort** — 3-5 days/plugin estimate from v0.7 §4.3. Real number after Week 1 spike.
3. **openclaw trace API** — `--verbose on` schema unknown. Required for tool-call observability in Stage 2 capability dimensions.
4. **Image size** — full openclaw + dist-runtime is huge; opt-in via `OPENCLAW_EXTENSIONS` should help.
5. **Sidecar vs single-container** — pure container with `supervisord` or docker-compose? D2 design says single-container; revisit if conflicts emerge.

These get answered in Week 1 spikes. If any answer is "blocker", revise Stage 1 scope before starting Week 2.

---

## Files Created (this kickoff)

```
docs/superpowers/specs/
├── 2026-04-24-memory-eval-framework-design.md         (v0.7 unchanged)
├── 2026-04-24-memory-eval-framework-d1-findings.md    (Stage 0 D1)
├── 2026-04-24-memory-eval-framework-d4-d5-findings.md (Stage 0 closure ← updated)
└── 2026-04-26-stage1-kickoff.md                       (this doc)
```

Full Codex review trail (B.1 - B.6) and 7 design iterations (v0.1 - v0.7) preserved in the framework design doc.
