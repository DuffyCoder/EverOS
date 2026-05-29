# Stage 3 Operator Runbook

**Audience:** anyone running, debugging, or scaling the context-engine
plugin pipeline (Stage 3 wiring) on real openclaw images.

**Date:** 2026-05-05
**Backing closure:** [`docs/superpowers/specs/2026-05-05-stage3-closure.md`](../specs/2026-05-05-stage3-closure.md)
**Plugin audit:** [`docs/superpowers/specs/2026-05-02-stage3-phase5-plugin-audit.md`](../specs/2026-05-02-stage3-phase5-plugin-audit.md)

---

## What works today (validated 2026-05-05)

| Surface | Status |
|---|---|
| `kind: "context-engine"` slot wiring through Phase 0-2 | ✅ Validated by 80 unit tests + real-machine smoke |
| `dispatchEngineImport` precedence dispatcher | ✅ Unit tested (afterTurn ▶ ingestBatch ▶ ingest) |
| Native `engine_import_history` RPC | ⚠️ Stub-mode only; native blocked on upstream openclaw stable export |
| stub-engine smoke gate (Phase 4) | ✅ `WOMBAT_42` round-trip green |
| hypercompositor wiring (Phase 5 #1) | ✅ Multi-turn ingest+recall via `agent_run` |
| memclaw-context-engine wiring (Phase 5 #2) | ✅ Plugin loads, agent_run ok=true 19s |
| 1c1q LoCoMo (hypercompositor wiring baseline) | ✅ 4 stages complete, 0% accuracy as designed |
| 50Q LoCoMo (hypercompositor wiring baseline) | ✅ 94.5min, 0/50, no crashes |
| In-pipeline LLM judge resilience | ✅ R-S2-3 fix (concurrency 4 + retry on transient) |
| `forced_terminate` ratio in diagnostics | ✅ R-S3-4 emit |

---

## Quick start

### Prereqs

- Docker engine running (verify with `docker info`).
- `.env` at repo root with sophnet credentials:
  ```
  LLM_API_KEY=...
  LLM_BASE_URL=https://www.sophnet.com/api/open-apis/v1
  LLM_MODEL=gpt-4.1-mini
  SOPH_API_KEY=...
  SOPH_EMBED_URL=...
  SOPH_EMBED_EASYLLM_ID=...
  OPENCLAW_REPO_PATH=/Data3/shutong.shan/openclaw/repo
  ```
- Disk with **~10GB free** for image builds (use `make clean-docker` if low).
- `uv sync` once (creates `.venv/`).

### Build images

```bash
# Stub-engine (Phase 4 — bundled plugin source in openclaw-eval/plugins/)
.venv/bin/python openclaw-eval/harness/build.py \
  --memory-plugin stub-engine --variant slim

# Hypercompositor (Phase 5 #1 — install-mode via npm pack)
.venv/bin/python openclaw-eval/harness/build.py \
  --memory-plugin memory-core \
  --install-spec '@psiclawops/hypercompositor@0.9.6' \
  --install-plugin-id hypercompositor \
  --variant slim

# Memclaw (Phase 5 #2 — generality validator)
.venv/bin/python openclaw-eval/harness/build.py \
  --memory-plugin memory-core \
  --install-spec '@memclaw/memclaw-context-engine@0.9.61' \
  --install-plugin-id memclaw-context-engine \
  --variant slim
```

First build of base image takes ~30 minutes on slow npm; subsequent eval-layer
rebuilds are ~5 minutes.

### Run smoke gates

```bash
make smoke-stage3   # runs both stub-engine + hypercompositor gates
```

Pass criteria: stub-engine reply contains `WOMBAT_42`; hypercompositor T2
reply mentions "teal" (T1 planted that fact). Exit 0 = green.

### Run an eval scorecard

```bash
# 1c1q smoke (single conv + 1 question — fast wiring check ~5min)
.venv/bin/python -m evaluation.cli \
  --dataset locomo --system openclaw-docker-hypercompositor \
  --smoke --smoke-messages 0 --smoke-questions 1 \
  --from-conv 0 --to-conv 1 \
  --run-name s3-1c1q-$(date +%Y%m%d) \
  --retry-policy retry_once

# 50Q wiring baseline (10 convs × 5 q — ~95 min)
.venv/bin/python -m evaluation.cli \
  --dataset locomo --system openclaw-docker-hypercompositor \
  --smoke --smoke-messages 0 --smoke-questions 5 \
  --run-name s3-50q-$(date +%Y%m%d) \
  --retry-policy retry_once
```

⚠️ **0% accuracy expected** for context-engine plugins until upstream
`engine_import_history` lands or operator opts into yaml's
`context_engine_ingest_mode: all` (8.5h/conv at sophnet's response rate
— operator-driven only).

### After-the-fact rejudge

If the in-pipeline LLM judge ever fails (rare since R-S2-3 hardening
but possible), re-judge from saved `answer_results.json`:

```bash
.venv/bin/python evaluation/scripts/rejudge.py \
  --run-dir /tmp/<run-name> \
  --dataset-config evaluation/config/datasets/locomo.yaml \
  --concurrency 4
```

---

## Cleanup

```bash
make clean-docker            # safe: dangling images + cache > 24h, no eval images dropped
make clean-docker-aggressive # also drops older eval images, keeps newest of each plugin family
./openclaw-eval/scripts/clean_docker.sh --nuke   # docker system prune -af --volumes (your funeral)
```

The `--nuke` mode requires re-pulling the openclaw base image (~211MB, slow on
sophnet's typical 30 KiB/s npm tarball download).

---

## Troubleshooting

### "openclaw status reported not settled"
**Fixed in commit `7734fe6` (A1).** Settle wait is now skipped when
`context_engine_mode` is set + `memory_mode in (memory-core, noop)`.
If you still see it, verify your image has the patch:
```bash
docker run --rm --entrypoint grep <image> -c "settle_skipped_context_engine" \
  /eval/../app/evaluation/src/adapters/openclaw_adapter.py
```

### "docker prebootstrap agent_run failed for ... after 3 attempts"
hypercompositor's first-run vector store init pushes past 60s under
container concurrency. Bumped to 120s/180s for context-engine mode in
commit `6eead35`. If still hitting it: drop yaml's
`max_concurrent_containers` to 1.

### "plugin not found: ... (stale config entry ignored)"
**Fixed in commit `dfc8741`.** The plugin install path in the image
must be `/opt/openclaw/extensions/<id>/` owned by `root:root`. Use
`make clean-docker-aggressive` to drop old images and rebuild fresh.

### Bridge agent_run hangs forever (>2 min)
**Fixed in commit `dfc8741`.** Bridge spawns with `detached:true` +
process-group SIGKILL on hard timeout. If still observed, check that
the image has the patched bridge:
```bash
docker run --rm --entrypoint grep <image> -c "hardTimeoutMs\|forced_terminate" \
  /eval/openclaw_eval_bridge.mjs
# expect 6+
```

### "ENOSPC: no space left on device"
The disk-full incident from 2026-05-05 happened during an unattended
build loop. Recovery:
```bash
df -h /
make clean-docker-aggressive
docker builder prune -af
```
If still tight, check `/Data3/shutong.shan/.npm/_cacache/` — npm cache
isn't pruned by docker.

### LLM judge silently scores 0%
**Fixed in commit `84477cb` (R-S2-3).** Live LLMJudge now retries
transient errors with exponential backoff and logs loudly when
exhausted. The old silent-False-on-exception bug is gone. Verify
your judge config:
```bash
grep -E "judge_concurrency|judge_max_retries" evaluation/config/datasets/locomo.yaml
# defaults are concurrency=4, max_retries=4 (in code, no yaml needed)
```

### Latency stats look inflated
Check `diagnostics.json["forced_terminate"]`. Each true-counted call has
~3s kill-grace baked in. Subtract `kill_overhead_ms_estimate` from
total latency for a backing-engine-only view.

---

## File map

```
openclaw-eval/
├── harness/
│   ├── build.py                  # image build orchestrator (--install-spec etc.)
│   ├── stub_engine_passphrase_gate.sh   # Phase 4 smoke gate
│   ├── stub_passphrase_gate.sh   # Stage 1 memory-stub gate
│   ├── mem0_passphrase_gate.sh
│   └── evermemos_passphrase_gate.sh
├── scripts/
│   ├── smoke_stage3.sh           # this runbook's smoke entry
│   └── clean_docker.sh
├── container/
│   ├── entrypoint.sh             # renders /workspace/openclaw.docker.json
│   ├── openclaw_eval_bridge.mjs  # baked into images (synced from evaluation/scripts/)
│   ├── openclaw_eval_bridge_lib.mjs
│   └── openclaw.template.json
└── plugins/
    ├── stub-engine/              # Phase 4 stub plugin (kind: context-engine)
    └── stub/                     # legacy memory stub
```

```
evaluation/
├── cli.py                        # main entry: python -m evaluation.cli
├── config/
│   ├── datasets/locomo.yaml      # judge llm config + run shape
│   └── systems/
│       ├── openclaw-docker-hypercompositor.yaml
│       ├── openclaw-docker-memclaw.yaml
│       ├── openclaw-docker-stub.yaml
│       └── ... (Stage 1/2 memory plugins)
├── scripts/
│   ├── openclaw_eval_bridge.mjs   # canonical bridge (Stage 3+); container/ is mirror
│   ├── openclaw_eval_bridge_lib.mjs
│   └── rejudge.py
└── src/
    ├── adapters/openclaw_adapter.py
    ├── adapters/openclaw_docker_adapter.py
    ├── evaluators/llm_judge.py
    └── metrics/forced_terminate_metrics.py   # R-S3-4 aggregator
```

---

## Known constraints (Stage 4 entry)

| # | Constraint | Mitigation |
|---|---|---|
| 1 | Native `engine_import_history` blocked on upstream openclaw stable export | CLI replay (yaml `context_engine_ingest_mode: all`) for low-message-count datasets; otherwise wiring/prototype scorecard only |
| 2 | Hypercompositor / memclaw scorecards NOT comparable with Stage 1/2 memory-plugin matrix | Closure doc + plot legend must mark these runs explicitly |
| 3 | LoCoMo replay rate ~80s/msg makes "all" mode untenable for 380-msg conversations | Use shorter datasets or upgrade to bulk batch via `dispatchEngineImport` once #1 closes |
| 4 | Memclaw needs `~/.local/share/memclaw/config.toml` for full scoring | Auto-scaffolded on first boot; for production scoring, customize before run |
| 5 | Hypercompositor flagged "dangerous code patterns" at install (env access + network send) | Documented; production-grade evaluation should run network-isolated |

For the broader Stage 4 plan, see closure doc "Next steps" section.
