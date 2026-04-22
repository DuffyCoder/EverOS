# Hermes Memory Adapter for EverMemOS Evaluation — Design

**Date:** 2026-04-22
**Status:** Draft, awaiting review
**Context:** Integrate `hermes-agent` memory into the EverMemOS evaluation pipeline so we can benchmark hermes memory alongside openclaw/mem0/zep on LoCoMo.

---

## 1. Problem & Goal

`hermes-agent` (Nous Research's openclaw fork) exposes memory as a **plugin provider** interface, separate from its agent chat loop. We want to evaluate hermes memory on LoCoMo **without running the hermes agent**, using the same add → search → answer → evaluate pipeline the other adapters share.

Success criteria:
- `evaluation` CLI can run `--system hermes` on LoCoMo and produce scores comparable to `openclaw` / `mem0` / `zep`.
- Each hermes memory plugin (holographic, honcho, hindsight, ...) is selectable via yaml; results are fair (each plugin's own `prefetch`/`sync_turn` drives retrieval/ingest).
- No changes to hermes source are required — hermes is imported as a path-mounted library.

Non-goals (this spec):
- Running the hermes agent chat loop / tool-calling.
- Simulating the file-based `MEMORY.md` / `USER.md` builtin memory tool.
- Evaluating multiple plugins in a single run.

---

## 2. Scope Boundary — which layer we evaluate

Hermes "memory" is two layers:

1. **Builtin memory tool** (`tools/memory_tool.py`) — read/write `MEMORY.md` + `USER.md`. Always available, independent of `MemoryManager`.
2. **External plugin provider** — `plugins/memory/<name>/` implementing the `MemoryProvider` ABC (`initialize`, `prefetch`, `sync_turn`, `on_memory_write`, `on_session_end`, `shutdown`). Loaded by `plugins.memory.load_memory_provider(name)`.

**Option picked: B — evaluate plugin provider in isolation.** Rationale (documented in brainstorming):
- Matches `openclaw_adapter` pattern: adapter drives the memory subsystem directly, bypassing agent decisions. Keeps the comparison fair (all adapters skip agent-loop tool-calling).
- Builtin MEMORY.md is a full-dump file store without retrieval; on LoCoMo it either blows context or adds noise — not useful as a standalone baseline for this benchmark.
- Plugin providers have the retrieval behavior worth measuring (holographic = structured facts + HRR, honcho = dialectic modeling, hindsight = vector-backed, etc.).

**Deferred** (possible follow-up specs):
- Option C (builtin + plugin combined): mirror `MEMORY.md` into plugin via `on_memory_write`, include file content in context.
- Full agent-loop variant: run hermes run_agent with LoCoMo as conversation history and let the LLM decide when to call memory tools.

---

## 3. Architecture

### 3.1 File layout (new/modified)

```
evaluation/
├── src/adapters/
│   ├── hermes_adapter.py            # NEW — @register_adapter("hermes")
│   ├── hermes_runtime.py            # NEW — sys.path mount, per-conversation HERMES_HOME sandbox, plugin load helpers
│   ├── hermes_ingestion.py          # NEW — LoCoMo Conversation → hermes turn-pair stream
│   └── registry.py                  # MODIFIED — add "hermes" to _ADAPTER_MODULES
├── config/systems/
│   ├── hermes.yaml                  # NEW — default variant (holographic, local)
│   ├── hermes-holographic.yaml      # NEW — explicit holographic variant
│   ├── hermes-honcho.yaml           # NEW — cloud plugin variant (requires HONCHO_API_KEY)
│   └── hermes-hindsight.yaml        # NEW — cloud plugin variant (requires HINDSIGHT_API_KEY)
```

No changes under `evaluation/src/core/` or `evaluation/src/adapters/base.py`. No changes to hermes source.

### 3.2 Runtime model

For each LoCoMo conversation:

1. Create a **per-conversation sandbox** directory under `<output_dir>/artifacts/hermes/<run_id>/conversations/<conversation_id>/`.
2. Set `HERMES_HOME=<sandbox>` in an isolated env so plugin storage paths (e.g. holographic's `$HERMES_HOME/memory_store.db`) land inside the sandbox. This guarantees **conversation-level isolation** — required for LoCoMo since every question targets a single conversation's memories.
3. Instantiate a fresh plugin provider via `load_memory_provider(name)`. Call `provider.initialize(session_id=<conversation_id>, hermes_home=<sandbox>, platform="cli", agent_context="primary")`.
4. **Ingest**: iterate the conversation's messages in order, pairing consecutive `(speaker_A, speaker_B)` turns — drive `provider.sync_turn(user_content=A, assistant_content=B, session_id=<session_id>)` per pair. LoCoMo is typically 2-speaker; we pair whoever spoke first as "user", the respondent as "assistant". For odd-count tails, the unpaired turn is passed as `sync_turn(user_content=X, assistant_content="")` so no content is dropped. Multi-speaker conversations (≥3 speakers) fall back to round-robin pairing — logged as a warning; hermes plugin providers treat the strings opaquely so this is safe. (See §3.4 for per-plugin ingest strategy differences.)
5. **Build index** (lazy): call `provider.on_session_end(messages)` once (if configured) so plugins that extract at session boundaries get their chance. Write a `handle.json` recording the sandbox path + plugin name + ingest stats.
6. **Search**: for each question, call `provider.prefetch(query, session_id=<conversation_id>)` → formatted context string. Wrap in `SearchResult.results=[{content, score, metadata}]`.
7. **Answer**: reuse the shared mem0-compatible answer prompt (`config/prompts.yaml::online_api.default.answer_prompt_mem0`), same as `openclaw_adapter` uses.
8. **Shutdown**: `provider.shutdown()` at adapter teardown.

Providers are **synchronous**; all calls are wrapped in `asyncio.to_thread(...)` to keep the pipeline's async event loop free.

### 3.3 Hermes source mounting

- yaml `hermes.repo_path` is primary. Env var `HERMES_REPO_PATH` is a fallback.
- At adapter construction, `hermes_runtime.ensure_hermes_importable(repo_path)` prepends the repo to `sys.path`. Imports used by the adapter: `agent.memory_provider.MemoryProvider` (type hints), `plugins.memory.load_memory_provider`. `hermes_constants.get_hermes_home` is *not* imported by the adapter — it's called indirectly by plugins (e.g. holographic's `_load_plugin_config()`), and `HERMES_HOME` is overridden per conversation via `os.environ`, which those plugins pick up.
- We do **not** use `MemoryManager`; the adapter drives a single `MemoryProvider` instance directly, bypassing manager-level multiplexing (matches Option B scope).
- Import is **lazy** — failures with clear messages, not at module load time.

### 3.4 Per-plugin ingest strategy

Providers differ in *when* they persist content. The adapter supports three strategies selectable per plugin via yaml `hermes.ingest_strategy`:

| Strategy          | Behavior                                                                                  | Default for                   |
|-------------------|-------------------------------------------------------------------------------------------|-------------------------------|
| `sync_per_turn`   | Call `sync_turn(u, a)` per user/assistant pair.                                           | `honcho`, `hindsight`, `mem0` |
| `session_end`    | Accumulate messages in memory; call `on_session_end(messages)` once per conversation.     | `holographic` (uses `auto_extract=true` for fact extraction) |
| `both`            | Both of the above.                                                                        | opt-in per yaml               |

Rationale: `holographic.sync_turn` is a no-op — it only populates facts through explicit tool calls OR `on_session_end` auto-extraction. Without `session_end`, it would retrieve nothing. Other plugins (honcho, hindsight) persist in `sync_turn`, so we default them there.

When a plugin needs plugin-specific config (e.g. holographic's `auto_extract: true`, `hrr_dim: 1024`), the adapter writes it to `<sandbox>/config.yaml` under `plugins.hermes-memory-store:` before `initialize()` (mirroring holographic's own `_load_plugin_config()` path).

### 3.5 Adapter contract mapping

| `BaseAdapter` method  | Hermes adapter                                                         |
|-----------------------|------------------------------------------------------------------------|
| `prepare()`           | Idempotent: resolves run_root, marks `_prepared`.                     |
| `add(conversations)`  | Per conversation: build sandbox, instantiate provider, ingest, write `handle.json`. Returns `{"type": "hermes_sandboxes", "run_id", "root_dir", "conversations": {...}}`. |
| `build_lazy_index()`  | Re-open sandboxes from disk, load `handle.json`, re-instantiate provider (no re-ingest). Same shape as `add()`'s return. |
| `search(q, conv_id)`  | Concurrency-capped (`max_inflight_queries_per_conversation`) call to `provider.prefetch`; build `SearchResult` with retrieval metadata incl. `retrieval_latency_ms`, `plugin`, `strategy`. |
| `answer(q, ctx)`      | Shared mem0 answer prompt via `LLMProvider` (same wiring as `openclaw_adapter._generate_answer`). |

### 3.6 Error handling & observability

- Plugin `is_available()` returning False or `initialize()` raising → adapter fails that conversation fast, writes `handle.run_status="failed"` + error string, and the pipeline records it (same pattern as `openclaw_adapter`).
- Per-conversation `handle.json` records: plugin name, strategy, ingest_turns, ingest_latency_ms, `HERMES_HOME` path, hermes commit (best-effort from `git -C <repo_path> rev-parse HEAD`).
- All bridge-equivalent calls emit debug logs. No Node bridge here — pure Python imports.

---

## 4. Configuration

### 4.1 `config/systems/hermes.yaml` (default → holographic, local-only)

```yaml
adapter: "hermes"

llm:
  provider: "openai"
  model: "gpt-4o-mini"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
  temperature: 0.0
  max_tokens: 1024

search:
  top_k: 6
  response_top_k: 5
  num_workers: 5
  max_inflight_queries_per_conversation: 1

answer:
  max_retries: 3

hermes:
  repo_path: "${HERMES_REPO_PATH}"
  plugin: "holographic"              # which plugin to load
  ingest_strategy: "session_end"     # see §3.4
  plugin_config:                     # written to <sandbox>/config.yaml under plugins.hermes-memory-store
    auto_extract: true
    default_trust: 0.5
    hrr_dim: 1024
  prompts:
    answer_mode: "shared"            # reuse mem0 prompt from prompts.yaml
```

### 4.2 Other variants

- `hermes-honcho.yaml` — `plugin: "honcho"`, `ingest_strategy: "sync_per_turn"`, requires `HONCHO_API_KEY`.
- `hermes-hindsight.yaml` — `plugin: "hindsight"`, `ingest_strategy: "sync_per_turn"`, requires `HINDSIGHT_API_KEY` + `HINDSIGHT_API_URL` (or local mode).
- `hermes-holographic.yaml` — explicit pin of §4.1.

Further variants (openviking, byterover, supermemory, retaindb, mem0-via-hermes) are trivially copy-paste of the same skeleton; not in this spec's scope (YAGNI until needed).

---

## 5. Testing

Unit tests (pytest, under `tests/evaluation/adapters/hermes/` — new subdir; existing `tests/evaluation/` holds the broader adapter tests):

1. `test_hermes_runtime.py`
   - `ensure_hermes_importable` prepends repo to `sys.path` and idempotent on repeat calls.
   - Missing `repo_path` → clear `ValueError`.

2. `test_hermes_ingestion.py`
   - LoCoMo Conversation → turn-pair stream: alternating speakers pair correctly; trailing unpaired turn is dropped with warning.

3. `test_hermes_adapter.py` (fake provider, no hermes repo required)
   - Inject a stub `MemoryProvider` via monkeypatch on `load_memory_provider`.
   - `add()` writes `handle.json` with `run_status="ready"`.
   - `search()` returns a `SearchResult` whose `results[0]["content"]` equals what the stub's `prefetch()` returned.
   - `ingest_strategy` dispatch: `sync_per_turn` calls `sync_turn` N times; `session_end` calls `on_session_end` once; `both` calls both.
   - Failure mode: stub's `initialize` raises → `handle.run_status="failed"` + no crash of pipeline.

4. `test_hermes_integration_smoke.py` (marked `@pytest.mark.integration`, skipped without `HERMES_REPO_PATH`)
   - Run `add() → search() → answer()` on a 2-turn synthetic conversation using `holographic` plugin.
   - Assertions: non-empty prefetch context; answer returns non-empty string.

Coverage target: 80%+ on new adapter code (`hermes_adapter.py`, `hermes_runtime.py`, `hermes_ingestion.py`).

Not in scope: cross-plugin integration tests (each plugin has its own out-of-band dependencies).

---

## 6. Risks & open questions

1. **Plugin quality varies.** Honcho requires live API. Hindsight requires API URL. Holographic needs `numpy` (optional dep). We ship variants but don't guarantee all are runnable out of the box — `is_available()` gate handles the "skip this run" case.
2. **`sync_turn` is noisy per-turn for some plugins.** Honcho may rate-limit on bulk ingest. Adapter logs but doesn't retry — reruns handle it; if it becomes a real problem we'll add backoff in a follow-up.
3. **`HERMES_HOME` isolation.** We override via `os.environ` at the start of per-conversation work, inside a context manager that restores the prior value. Concurrent conversations (`num_workers: 5`) each need their own sub-process OR serialized access — we start serialized (`max_inflight_queries_per_conversation: 1`), add parallel via worker process pool later if needed. **Action: confirm conversation-level add is serialized in the current pipeline.**
4. **Numpy / sqlite availability.** Holographic imports `numpy` only when HRR is used; missing `numpy` → degrades gracefully. If the evaluation env doesn't have numpy, we skip that plugin in `is_available()`.
5. **Plugin-side LLM calls.** Some plugins (honcho auto-summarization) make their own LLM calls inside `initialize`/`sync_turn`. That LLM key is configured in the plugin's own env vars, not in the adapter's `llm:` block. The yaml must make this distinction clear — plugin LLM config stays outside the adapter's `llm:` section.
6. **Session boundary semantics.** LoCoMo conversations contain multiple "sessions" (dates); for plugins that care about session boundaries, we map each LoCoMo session to a distinct `session_id`. For `session_end` strategy, we call `on_session_end` once per LoCoMo **session** (not per conversation). **Action: confirm this matches each plugin's expectation; default to per-conversation if unsure.**

---

## 7. Out of scope / explicitly deferred

- Builtin memory tool (`MEMORY.md` / `USER.md`) integration — deferred to a possible option-C spec.
- Running hermes agent chat loop — deferred.
- Evaluating multiple plugins in one run — each run is one plugin.
- Auto-discovery of plugins at CLI time — user explicitly names a plugin in yaml.

---

## 8. Implementation plan preview

This spec feeds into a `writing-plans` artifact. Rough phases (details in plan doc):

1. Runtime + ingestion plumbing (hermes_runtime, hermes_ingestion, tests 1–2).
2. Adapter skeleton with stub provider (hermes_adapter, test 3).
3. Registry wiring + `hermes.yaml` default.
4. Holographic variant + integration smoke (test 4).
5. Honcho/Hindsight variants (pending credentials).
