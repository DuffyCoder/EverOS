# Per-QA isolation for context-engine plugins

> Status: implemented in PR4 of `claude/plugin-cli-unify`. CLI flag:
> `--per-qa-isolation snapshot|auto|off` (default: `auto`).

## Why this exists

Memory-plugin runs (mem0, evermemos, memory-core, etc.) get cross-QA
isolation **for free** by construction:

| Layer | Memory-plugin | Context-engine (R2 routing) |
|-------|---------------|-----------------------------|
| L1 plugin store | Read-only after add() | Read-only after add() |
| L2 agent transcript | Per-QA file (session_id=`{conv}__{qid}`) | Per-QA file |
| L3 ContextEngine state | n/a — plugin has no afterTurn hook | **Conv-keyed** — afterTurn writes here |

The L3 write is the leak source. Under R2 routing the eval framework
passes `sessionId = conv_id` to the CE plugin, so every QA's
`afterTurn(sessionId, …)` mutates the same conv-level row. QA n+1's
`assemble(conv_id)` then sees QA n's question and answer.

The damage isn't symmetric across plugins: an aggressive afterTurn writer
(hypercompositor, etc.) gets more "free signal" from the leak than a
lightweight one (legacy CE), which silently inflates its score relative
to less-aggressive engines. Internal CE-vs-CE comparison becomes a
benchmark of "how much each engine writes in afterTurn", not retrieval
quality.

## What this fixes

After `add()` finalizes, freeze `/workspace/state/` to a baseline.
**Implementation note**: the freeze fires lazily on the first
`_generate_answer_via_agent` call per conversation, not strictly at the
end of `add()`. This makes partial-pipeline runs robust (e.g.
`--stages search answer evaluate` skipping `add`). For each QA, restore
a fresh copy of the baseline to a per-QA directory and point the bridge
at it. Discard the per-QA directory after the answer.

```
first answer call ───► freeze_state()  (once per conv, idempotent)
                       │
                       ▼
                 /workspace/.qa_state_baseline/   (preserved across
                       │                            re-runs unless
                       │                            workspace is wiped)
                       │
per QA:                ▼
  /workspace/.qa_state_baseline/  ──cp -r──►  /workspace/.qa_states/<qid>/
                                                    │
                                                    │  bridge state_dir = /workspace/.qa_states/<qid>
                                                    ▼
                                              agent_run writes here
                                                    │
                                                    │  discard (try/finally)
                                                    ▼
                                              rm -rf .qa_states/<qid>
```

The plugin sees the same `sessionId` (still `conv_id` under R2) and
writes the same things via `afterTurn`. The writes land in a doomed
copy. Next QA starts from the same baseline.

## CLI

```bash
uv run python -m evaluation.cli --dataset locomo --system openclaw-docker \
    --memory-plugin none \
    --context-engine hypercompositor@0.9.6 \
    --per-qa-isolation snapshot
```

Values:

| Value | Behavior |
|-------|----------|
| `auto` (default) | snapshot iff yaml's `openclaw.context_engine_mode` is non-empty (or `--context-engine <id>` was passed on the CLI) |
| `snapshot` | always snapshot, even on memory-only runs |
| `off` | never snapshot — current R2 behavior, leaks documented |

Yaml equivalent (CLI overrides):

```yaml
openclaw:
  context_engine_mode: hypercompositor    # the field auto mode reads
openclaw_docker:
  per_qa_isolation: auto                  # auto | snapshot | off
```

To start fresh after pollution from a prior run, pass
`--clean-groups` (clears the conversation-scoped database state before
the add stage; does NOT touch the workspace directory itself — wipe
`<output_dir>/artifacts/<conv>/workspace/.qa_state_baseline` if the
baseline itself is stale).

## What this does NOT fix

Cross-paradigm comparison (memory-plugin vs context-engine) is still
not directly comparable even with snapshot enabled — see the structural
asymmetries documented in `evaluation/docs/openclaw_adapter.md`:

1. **add-phase work**: memory plugins one-shot ingest into a markdown
   store; CE plugins replay every conv message through `agent_run`
   (see `_replay_conv_via_agent_run` in `openclaw_docker_adapter.py`).
   Different cost basis, different prompt-shaping.

2. **retrieval mechanism**: memory plugin = LLM-driven explicit tool
   calls. CE = transparent context injection via `assemble()`. The LLM
   sees structured tool results vs rewritten messages; different
   utilization characteristics.

3. **legacy CE** in memory-only runs is essentially a passthrough.
   Comparing legacy-side scores to a real CE plugin's scores is mixing
   "memory plugin's number" with "CE plugin's number".

Scope of this fix: **CE-vs-CE comparability**. That's the main
benefit. Don't read cross-paradigm scorecards as comparable just because
the leak is closed.

## Implementation notes

v1 uses `shutil.copytree` (cp -r). LoCoMo state size is typically tens
of MB per conversation, so per-QA cp adds seconds of I/O — small
fraction of a 60–90s `agent_run`. If profiling shows snapshot dominates,
a v2 backed by overlayfs would let `_restore_state_for_qa` mount an
upper-layer rather than copy. The public API would not change.

`symlinks=False` (default) is used during `copytree`. If a plugin ever
writes a symlink in `/workspace/state` pointing outside the workspace
(e.g. `state/cache → /tmp/shared`), the symlink is dereferenced — every
QA gets its own copy of the target instead of all sharing the real
file. Preserving the link would silently break isolation.

Lazy-freeze fires on the first `_generate_answer_via_agent` call per
conversation. Re-runs (e.g. `--stages search answer evaluate` after a
prior full eval) preserve the previous baseline; the lazy-freeze logic
detects an existing `.qa_state_baseline/` and skips re-freezing — the
baseline must reflect post-add() state, not post-answer pollution.
Operators wanting a fresh baseline should pass `--clean-groups` or wipe
the workspace.

## Concurrency

`answer.max_concurrent` (yaml) is a **global** cap on concurrent
answer-stage calls — it is not enforced per-conversation. Two QAs from
the same conv can therefore enter `_generate_answer_via_agent`
concurrently. The freeze step is guarded by a per-sandbox
`asyncio.Lock` so only the first coroutine actually runs
`freeze_state`; subsequent waiters see `_pr4_state_frozen=True` after
the lock releases and return.

The per-QA restore (`/workspace/.qa_states/<qid>/`) does **not** need
its own lock because each `qid` is unique per call — the answer stage
never re-asks the same `qid` in the same run. If a future code path
ever does, the second call hits `FileExistsError` on `copytree` —
catch that as a programmer error rather than papering over it.

## Known follow-ups (deferred from PR1–5)

These are not blockers for the PR1–5 closure but should be tracked
separately:

1. **overlayfs v2** for `freeze_state` / `restore_state_for_qa`. The
   v1 `cp -r` implementation is fine for LoCoMo-scale state (tens of
   MB), but bigger workloads or expensive plugin state could amortize
   per-QA cost via overlay mounts. The public API stays the same.

2. **Strict validation of unknown `per_qa_isolation` values.** Today
   the resolver coerces typos (e.g. `"snapshots"`) to `auto` silently.
   Tighten at config-load / CLI argparse so typos error out early.

3. **CLI matrix runner.** `--memory-plugin X --context-engine Y`
   combinations (4–6 typical) currently require N separate invocations
   with different `--run-name`s. A `--matrix memory=A,B
   context-engine=none,C` runner would be a small ergonomic win for
   sweep workflows.
