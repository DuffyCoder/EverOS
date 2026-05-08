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

After `add()` finalizes, freeze `/workspace/state/` to a baseline. For
each QA, restore a fresh copy of the baseline to a per-QA directory and
point the bridge at it. Discard the per-QA directory after the answer.

```
end of add() ───► freeze_state()
                       │
                       ▼
                 /workspace/.qa_state_baseline/   (immutable until --clean-groups)

per QA:
  /workspace/.qa_state_baseline/  ──cp -r──►  /workspace/.qa_states/<qid>/
                                                    │
                                                    │  bridge state_dir = /workspace/.qa_states/<qid>
                                                    ▼
                                              agent_run writes here
                                                    │
                                                    │  discard
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
| `auto` (default) | snapshot iff `openclaw.context_engine_mode` is non-empty |
| `snapshot` | always snapshot, even on memory-only runs |
| `off` | never snapshot — current R2 behavior, leaks documented |

Yaml equivalent (overrideable by CLI):

```yaml
openclaw_docker:
  per_qa_isolation: auto    # auto | snapshot | off
```

## What this does NOT fix

Cross-paradigm comparison (memory-plugin vs context-engine) is still
not directly comparable even with snapshot enabled — see the structural
asymmetries documented in `evaluation/docs/openclaw_adapter.md`:

1. **add-phase work**: memory plugins one-shot ingest into a markdown
   store; CE plugins replay every conv message through `agent_run`
   (see `_replay_conv_for_context_engine` in `openclaw_docker_adapter.py`).
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

## Concurrency caveat

The current implementation assumes per-conversation serialization of
QAs (yaml `max_inflight_queries_per_conversation: 1`). Concurrent QAs
within the same conv would race on `restore_state_for_qa` for the same
`qid` (collision on `.qa_states/<qid>`) — see the docstring on the
`# per-QA isolation v1` block in `openclaw_docker_adapter.py`. Raising
that cap requires adding an `asyncio.Lock` keyed by `(conv_id, qid)`
inside the adapter.
