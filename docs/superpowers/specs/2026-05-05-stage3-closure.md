# Stage 3 Closure — Context-Engine Plugin Support

**Date:** 2026-05-05
**Branch:** `claude/reverent-tharp-f8f082`
**Spec input:** [`2026-04-30-plugin-kinds-design-note.md`](2026-04-30-plugin-kinds-design-note.md) (revision 4, post 4 rounds Codex review)
**Plan:** [`docs/superpowers/plans/2026-04-30-stage3-context-engine-support.md`](../plans/2026-04-30-stage3-context-engine-support.md)

---

## Outcome at a glance

| Phase | Plan estimate | Actual | Status |
|---|---|---|---|
| 0 — upstream openclaw patch | 0.5 day | empirically 0d (downgraded) | ✅ no-op |
| 1 — entrypoint slot render | 0.5 day | 0.5 day | ✅ |
| 2 — resolved-config + docker env | 1 day | 1 day | ✅ |
| 3 — bridge `engine_import_history` RPC | 3-3.5 days | 0.5 day (dispatcher only) | ⚠️ partial |
| 4 — stub-engine + smoke gate | 0.5 day | 0.5 day + real-machine smoke PASS | ✅ |
| 5 — first real plugin onboarding | 1-2 days | ~1.5 days incl. real-machine bring-up | ✅ wiring; ⚠️ scoring |
| **Total** | 6.5-8 days | **~4.5 days** + 13 commits | |

**Real-machine validations:**
- ✅ stub-engine smoke gate (Phase 4): three criteria green, sentinel WOMBAT_42 flowed end-to-end through `slots.contextEngine`.
- ✅ hypercompositor multi-turn smoke (Phase 5): T1 ingest → T2 recall ("teal" → "Your favorite color is teal") via direct bridge agent_run.
- ✅ hypercompositor 1c1q LoCoMo (wiring baseline): all 4 stages complete in 329s, accuracy 0% (expected for empty engine state without ingest replay).
- ✅ hypercompositor 50Q LoCoMo (wiring baseline): all 4 stages complete in 5670s (~94.5 min), accuracy 0/50 (0%) — pipeline stable across 10 conversations × 5 questions each; all 50 answers consistently report "no information in memory" (mean answer length 190 chars, no empty answers, no crashes).

---

## Commit timeline (Stage 3 Phase 0 → 5 + A1 fix)

| # | Commit | Phase | Summary |
|---|---|---|---|
| 1 | `c7c9c7f` | 0 | Phase 0 re-audit — upstream normalize patch downgraded to nice-to-have |
| 2 | `a2ea01d` | 1 | entrypoint.sh renders `slots.contextEngine` from `CONTEXT_ENGINE_PLUGIN_ID` |
| 3 | `91ace70` | 2 | `build_openclaw_resolved_config(context_engine_mode=...)` + docker adapter env emit |
| 4 | `af4e85d` | 3 | `dispatchEngineImport` precedence dispatcher (afterTurn ▶ ingestBatch ▶ ingest) + stub bridge handler |
| 5 | `11efe15` | 4 | stub-engine plugin scaffold + `stub_engine_passphrase_gate.sh` |
| 6 | `953ddaa` | 5 | hypercompositor audit + yaml scaffold |
| 7 | `7f8313d` | 4 | fix smoke gate config path (`/workspace/openclaw.docker.json`) — real-machine smoke PASS |
| 8 | `b2227e3` | 5 | build.py `--extra-install-spec` + Dockerfile dual install + entrypoint multi-load.paths |
| 9 | `dfc8741` | 5 | bridge process-group kill + npm pack install + chown root for ownership guard |
| 10 | `124a7ea` | 5 | hypercompositor docker yaml + audit-aligned config |
| 11 | `7734fe6` | A1 | skip settle wait when context-engine intercepts ingest |
| 12 | `6eead35` | A1 | replay scaffold (gated by `context_engine_ingest_mode`) + container bridge sync |

13 source files modified, 80 unit tests passing across 9 test files.

---

## What works end-to-end today

### Wiring chain (verified by both unit tests AND real-machine smoke)

```
yaml openclaw.context_engine_mode=hypercompositor
  ↓
build_openclaw_resolved_config(context_engine_mode=...)
  ↓
plugins.allow=[..., "hypercompositor"], slots.contextEngine="hypercompositor", entries[hypercompositor].enabled=true
  ↓
DockerizedOpenclawAdapter._docker_env_for_container() emits CONTEXT_ENGINE_PLUGIN_ID
  ↓
entrypoint.sh jq splices into rendered openclaw.docker.json
  ↓
plugins.load.paths includes /opt/openclaw/extensions/hypercompositor (from --install-spec)
  ↓
openclaw plugin loader discovers hypercompositor's openclaw.plugin.json
  ↓
plugin's register() calls api.registerContextEngine("hypercompositor", factory)
  ↓
agent --local triggers resolveContextEngine(config) → factory() → engine.assemble({sessionId, messages})
  ↓
systemPromptAddition flows into agent's system prompt → model reply reflects engine's context
```

All 16 stub-engine plugin manifest tests + 9 hypercompositor yaml integration tests + 13 dispatcher tests verify each link in this chain.

### Smoke gate command (operator-runnable today)

```bash
# stub-engine (Phase 4):
IMAGE=openclaw-eval:7da23c3-stub-engine-005ac5c-slim \
  ./openclaw-eval/harness/stub_engine_passphrase_gate.sh
# Expected: PASS — all three criteria green
```

### First real plugin (Phase 5 hypercompositor)

```bash
# Build:
.venv/bin/python openclaw-eval/harness/build.py \
  --memory-plugin memory-core \
  --install-spec '@psiclawops/hypercompositor@0.9.6' \
  --install-plugin-id hypercompositor \
  --variant slim
# → tag: openclaw-eval:7da23c3-install-hypercompositor-5bace1f-slim

# Direct multi-turn smoke (verified 2026-05-05):
# T1: "My favorite color is teal..." → "Got it, teal is your favorite color."  (7.7s)
# T2: "What is my favorite color?"   → "Your favorite color is teal."         (3.3s)
# T2 recalls T1's fact via hypercompositor's afterTurn ingest.

# 1c1q LoCoMo wiring baseline (verified 2026-05-05, 329s):
.venv/bin/python -m evaluation.cli \
  --dataset locomo --system openclaw-docker-hypercompositor \
  --smoke --smoke-messages 0 --smoke-questions 1 \
  --from-conv 0 --to-conv 1 \
  --run-name s3-hyper-1c1q-baseline
# Result: pipeline completes, accuracy 0% (expected for empty engine state).
```

---

## Empirical findings (deltas vs spec)

### F1 — Phase 0 upstream normalize patch is unnecessary (spec said required)

`loadConfig()` at `src/config/io.ts` reads raw JSON without going through `normalizePluginsConfigWithResolver`. `slots.contextEngine` survives the load → `resolveContextEngine(config)` reads `config?.plugins?.slots?.contextEngine` directly (`registry.ts:412`). The Codex r1 finding is technically accurate but does not block our slot wiring. Phase 0 patch deferred indefinitely; 0.5 day saved.

### F2 — Native `engine_import_history` requires upstream stable export (Phase 3 partial)

Survey of openclaw `dist/`:
- 255 `package.json#exports` paths, all `./plugin-sdk/*` subpaths.
- `resolveContextEngine` / `resolveRuntimePluginRegistry` are bundle-internal symbols (e.g. `dist/registry-D4L8wbCo.js` exports them under munged `r`/`t`/`n` names).
- No stable path reaches `context-engine/registry`.

Three implementations considered:
1. **Upstream patch** (right answer) — adds `./context-engine` or `./eval-harness/import-history` plugin-sdk subpath. Tracked as Stage 4 work.
2. **Hash-discovery import** — `glob dist/registry-*.js`, import munged `r`. Brittle across openclaw rebuilds.
3. **CLI replay via `agent_run`** — feed each message as a turn; engine's `afterTurn` fires natively. Used for Phase 5 prototype but **prohibitively slow** (~80s/msg × ~380 msg/conv × 10 convs = ~8.5h/conv sequential).

Stage 3 dispatcher (`dispatchEngineImport` in `openclaw_eval_bridge_lib.mjs`) is fully unit-tested for the production precedence (`afterTurn ▶ ingestBatch ▶ ingest`) and ready to wire as soon as the upstream export lands.

### F3 — `openclaw plugins install` writes to `$HOME/.openclaw`, not `OPENCLAW_HOME`

Setting `OPENCLAW_HOME=/opt/openclaw` at build time was insufficient — the install command resolves to `~/.openclaw/extensions/<id>`. With `HOME=/workspace/home` (Dockerfile.eval ENV), the install lands inside the runtime `/workspace` mount and gets shadowed by host's volume on `docker run`.

**Fix:** bypass `openclaw plugins install` for build-time installation. Use `npm pack` + `tar -x` directly into `/opt/openclaw/extensions/<id>/`, then `npm install` to resolve transitive deps. This survives the workspace mount.

### F4 — openclaw plugin loader rejects non-root, non-process-uid ownership

After `npm pack` extracts as `node` (uid 1000), the runtime container runs `--user $HOST_UID` (typically 1005). openclaw's "suspicious ownership" guard rejects the plugin dir.

**Fix:** `chown -R root:root /opt/openclaw/extensions` at the end of plugin install. openclaw treats root as trusted regardless of runtime uid.

### F5 — Bridge `proc.on("close")` never fires for context-engine plugins with background timers

hypermem (hypercompositor's storage dep) starts a 5-minute `setInterval` indexer task. After `agent --local` prints `stopReason: stop`, the parent openclaw process exits but the `openclaw-agent` grandchild inherits the bridge's stderr fd and stays alive. `proc.on("close")` waits for stdio close, hanging the bridge indefinitely.

**Fix (in `runLauncher`):** spawn with `detached: true` + `proc.unref()` to put the spawned tree in its own process group. On hard timeout, `process.kill(-pgid, SIGTERM)` then `SIGKILL` to terminate the entire group, freeing the stderr pipe.

Multi-turn smoke confirms `forced_terminate: false` after the fix — agent group exits cleanly.

### F6 — Eval pipeline's `_flush_and_settle_if_needed` invariant fails for context-engine mode

When `context_engine_mode` + `memory_mode in (memory-core, noop)`, hypercompositor's `afterTurn` intercepts ingest before memory-core's indexer fires. Memory backend reports `settled=false, files=0, chunks=0` forever. The pipeline crashes at the add() boundary.

**Fix (commit `7734fe6`):** short-circuit the settle check when context-engine intercepts. visibility_state is set to "settled" without an RPC. Memory-plugin-only paths and dual-mode paths (real memory plugin + context-engine) preserve the existing settle invariant.

### F7 — R2 replay is too slow for LoCoMo at sequential rate

LoCoMo conv has 19 sessions × ~20 messages = ~380 messages. Each agent_run replay (per the R2 strategy) takes ~80s under the Phase 5 build (LLM call + hypercompositor fact extraction + vector store update). Sequential replay: ~8.5h per conv. Even 50% parallelism still pushes 50Q to many hours.

**Fix (commit `6eead35`):** gate replay behind yaml `context_engine_ingest_mode` (default `"none"`). Default mode produces a wiring/prototype scorecard (engine sees empty session, accuracy ~0%); `"all"` mode is operator-driven for true scoring runs.

For comparable scorecards, the path is F2 + dispatcher (commit `af4e85d`): once upstream exports `resolveContextEngine`, `dispatchEngineImport` ingests the entire conv in one batch via `afterTurn` (a single RPC, not per-turn agent_run), which should run in seconds not hours.

---

## Test inventory (80 passing across 9 files)

| File | Count | Coverage |
|---|---|---|
| `test_openclaw_bridge_lib.py` | 11 | extractJsonObject, stripAnsi |
| `test_openclaw_bridge_payload.py` | 6 | bridge stdin/stdout shape |
| `test_openclaw_bridge_engine_import.py` | 13 | dispatchEngineImport precedence + stub mode |
| `test_resolved_config_context_engine.py` | 9 | build_openclaw_resolved_config(context_engine_mode=...) |
| `test_docker_adapter_context_engine_env.py` | 4 | CONTEXT_ENGINE_PLUGIN_ID env emit |
| `test_entrypoint_context_engine_render.py` | 6 | jq slot splice, install_load_paths |
| `test_stub_engine_plugin_manifest.py` | 16 | stub-engine plugin static structure |
| `test_hypercompositor_yaml.py` | 9 | hypercompositor yaml → resolved config |
| `test_flush_settle_context_engine.py` | 6 | A1 settle skip + memory-plugin invariant preserved |

Run all 80:
```bash
.venv/bin/python -m pytest tests/evaluation/ -q
```

---

## Scorecard

| Run | n | Acc | Latency | Notes |
|---|---|---|---|---|
| stub-engine smoke gate | 1 | PASS | <30s | three criteria green; WOMBAT_42 echo via assemble().systemPromptAddition |
| hypercompositor multi-turn (direct bridge) | 2 | PASS | T1 7.7s, T2 3.3s | T2 recalls T1's "teal" fact |
| hypercompositor 1c1q LoCoMo (wiring baseline) | 1 | 0% | 329s | engine sees empty session as designed |
| hypercompositor 50Q LoCoMo (wiring baseline) | 50 | 0% | 5670s | 10 convs × 5q; 49min add + 45min answer; all 50 answers "no info in memory", no crashes |

**Comparability caveat (Codex r3):** these scorecards are NOT comparable with Stage 1/2 memory-plugin matrix (mem0 50.67%, evermemos 17.33%, memory-core 23.78%). The hypercompositor numbers measure pipeline integration only — without ingest replay, the engine cannot produce informed answers. True-scoring would need either upstream `engine_import_history` export OR an operator-driven 8.5h/conv full replay run.

---

## Risks closed

| # | Risk | Outcome |
|---|---|---|
| R-S2-4 (Stage 2) | context-engine kind plugins blocked from onboarding | ✅ closed — wiring complete, real plugin onboarded |
| Plan R1 | upstream openclaw refuses Phase 0 patch | ✅ avoided — patch shown unnecessary (F1) |
| Plan R2 | first plugin uses bootstrap() not afterTurn | ✅ avoided — hypercompositor uses afterTurn |
| Plan R3 | hidden config requirements | partial — hypercompositor itself works; install required several workarounds (F3, F4, F5) |
| Plan R5 | sophnet quota exhaustion | ✅ no quota issues during Phase 4/5 bring-up |

## Risks open (Stage 4 entry)

| # | Risk | Severity | Action |
|---|---|---|---|
| R-S3-1 | Native `engine_import_history` blocked behind upstream stable export (F2) | High | Submit upstream PR adding `./context-engine` plugin-sdk subpath; meanwhile use CLI replay for low-message-count datasets |
| R-S3-2 | Hypercompositor scorecard NOT comparable with Stage 1/2 memory plugins | High | Closure doc + future plots must mark these runs explicitly; no joint comparisons until R-S3-1 closes |
| R-S3-3 | Replay rate (~80s/msg) makes "all" mode untenable for LoCoMo-scale convs | Medium | Use bulk batch via dispatchEngineImport once R-S3-1 closes; do not advertise "all" mode as production |
| R-S3-4 | Bridge `forced_terminate: true` on hypercompositor counts hypermem indexer overhead in latency metrics | Medium | Add `latency_excluding_forced_terminate_ms` invariant to BenchmarkContext; track in Stage 4 |
| R-S3-5 | hypercompositor dangerous-code warning at install ("env access + network send") | Low | Documented; production-grade evaluation should run in network-isolated container |

---

## Next steps (Stage 4 candidates)

1. **R-S3-1 close:** PR upstream openclaw with stable `./context-engine` export; wire `dispatchEngineImport` into `engine_import_history` bridge handler (commit `af4e85d` already has the dispatcher).
2. **More memory plugins** (Stage 2 deferred): zep, memos, memu — extend the matrix.
3. **Stage 2 R-S2-1:** evermemos N=2 same-prompt missing data point.
4. **Stage 2 R-S2-3:** in-pipeline LLMJudge hardening (concurrency cap + retry from `rejudge.py` migrated to live pipeline).
5. **Second context-engine plugin** for generality validation (`@sonzai-labs/openclaw-context` or `@memclaw/memclaw-context-engine`).
6. **Operator runbook page** consolidating the build/run/clean commands; includes `make smoke-stage3` covering both stub-engine + hypercompositor real-machine gates.
