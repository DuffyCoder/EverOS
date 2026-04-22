# Hermes Memory Adapter for EverMemOS Evaluation — Design

**Date:** 2026-04-22
**Status:** Revised v2 (post adversarial review — fixes concurrency isolation, session-key contract, trust boundary)
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

**Session-key contract (fixed, one ID for the whole lifecycle):**
`session_id := conversation_id` for `initialize`, every `sync_turn`, every `prefetch`, and `on_session_end`. LoCoMo intra-conversation "sessions" (dated sub-segments) are **not** re-keyed — their boundaries are preserved by timestamps embedded in the turn strings only. This guarantees ingest and retrieval round-trip against the same namespace; no fragmentation.

For each LoCoMo conversation:

1. Create a **per-conversation sandbox** directory under `<output_dir>/artifacts/hermes/<run_id>/conversations/<conversation_id>/`.
2. Bind `HERMES_HOME=<sandbox>` **inside the serialized Hermes executor** (§3.3.1) — never at module scope, never in a concurrent thread that another task shares. Plugin storage paths (e.g. holographic's `$HERMES_HOME/memory_store.db`) land inside the sandbox; because the executor is single-threaded, no other Hermes call can observe a swapped env.
3. Instantiate a fresh plugin provider via `load_memory_provider(name)`. Call `provider.initialize(session_id=<conversation_id>, hermes_home=<sandbox>, platform="cli", agent_context="primary")`.
4. **Ingest**: iterate the conversation's messages in order, pairing consecutive `(speaker_A, speaker_B)` turns — drive `provider.sync_turn(user_content=A, assistant_content=B, session_id=<conversation_id>)` per pair. LoCoMo is typically 2-speaker; we pair whoever spoke first as "user", the respondent as "assistant". For odd-count tails, the unpaired turn is passed as `sync_turn(user_content=X, assistant_content="")` so no content is dropped. Multi-speaker conversations (≥3 speakers) fall back to round-robin pairing — logged as a warning; hermes plugin providers treat the strings opaquely so this is safe. (See §3.4 for per-plugin ingest strategy differences.)
5. **Build index** (lazy): call `provider.on_session_end(messages)` exactly **once per conversation** at the end of ingest (if configured), so plugins that extract at session boundaries get their chance. Write a `handle.json` recording the sandbox path + plugin name + ingest stats.
6. **Search**: for each question, call `provider.prefetch(query, session_id=<conversation_id>)` → formatted context string. Wrap in `SearchResult.results=[{content, score, metadata}]`.
7. **Answer**: reuse the shared mem0-compatible answer prompt (`config/prompts.yaml::online_api.default.answer_prompt_mem0`), same as `openclaw_adapter` uses.
8. **Shutdown**: `provider.shutdown()` at adapter teardown.

Providers are **synchronous**; all calls go through the serialized executor (§3.3.1) instead of raw `asyncio.to_thread(...)`.

### 3.3 Hermes source mounting

- yaml `hermes.repo_path` is primary. Env var `HERMES_REPO_PATH` is a fallback.
- At adapter construction, `hermes_runtime.ensure_hermes_importable(repo_path)` prepends the repo to `sys.path`. Imports used by the adapter: `agent.memory_provider.MemoryProvider` (type hints), `plugins.memory.load_memory_provider`. `hermes_constants.get_hermes_home` is *not* imported by the adapter — it's called indirectly by plugins (e.g. holographic's `_load_plugin_config()`), and `HERMES_HOME` is set per Hermes call via `os.environ` inside the serialized executor (§3.3.1), which those plugins pick up.
- We do **not** use `MemoryManager`; the adapter drives a single `MemoryProvider` instance directly, bypassing manager-level multiplexing (matches Option B scope).
- Import is **lazy** — failures with clear messages, not at module load time.

#### 3.3.1 Concurrency model — single Hermes executor

All Hermes provider touchpoints (`initialize`, `sync_turn`, `on_session_end`, `prefetch`, `shutdown`) are routed through a **single-worker executor** owned by the adapter instance:

```python
# module-level, owned by the adapter
_HERMES_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="hermes")
_HERMES_LOCK = asyncio.Lock()  # async-side guard for fairness ordering

async def _run_hermes(fn, *args, **kwargs):
    async with _HERMES_LOCK:
        return await asyncio.get_running_loop().run_in_executor(
            _HERMES_EXECUTOR, lambda: fn(*args, **kwargs)
        )
```

Inside the executor worker, a context manager swaps `HERMES_HOME` to the current conversation's sandbox, calls the provider, then restores the prior value. Because `max_workers=1`, no two conversations can be inside a Hermes call at the same time, so env swapping is safe.

**Why this over `asyncio.to_thread` with per-call env swaps:** `asyncio.to_thread` uses the default thread pool (multi-worker), so two concurrent conversations can be in Hermes code simultaneously and race on `os.environ`. Per-call env swaps without a global serializer are fundamentally unsafe in a multi-threaded process.

**Tradeoff:** Hermes work is serialized process-wide — pipeline-level `num_workers: 5` still applies to *non-Hermes* stages (e.g. the shared answer LLM call, which is safe to parallelize because it doesn't touch Hermes). In practice Hermes `prefetch` is tens of ms for local plugins; the serialization cost is acceptable at LoCoMo scale (~10 conversations × ~1k questions).

**Future hardening (deferred):** If Hermes work becomes the pipeline bottleneck, we can promote the executor from thread-pool to **process-pool**, each worker owning a pinned `HERMES_HOME` for the conversations routed to it, eliminating env swapping entirely. Out of scope for v1 — add only if profiling shows it's needed.

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
| `search(q, conv_id)`  | Route `provider.prefetch` through the global Hermes executor (§3.3.1); build `SearchResult` with retrieval metadata incl. `retrieval_latency_ms`, `plugin`, `strategy`. `max_inflight_queries_per_conversation` in yaml is retained for interface parity with `openclaw.yaml` but is redundant — the single-worker executor already enforces process-wide serialization. |
| `answer(q, ctx)`      | Shared mem0 answer prompt via `LLMProvider` (same wiring as `openclaw_adapter._generate_answer`). |

### 3.6 Error handling & observability

- Plugin `is_available()` returning False or `initialize()` raising → adapter fails that conversation fast, writes `handle.run_status="failed"` + error string, and the pipeline records it (same pattern as `openclaw_adapter`).
- Per-conversation `handle.json` records: plugin name, strategy, ingest_turns, ingest_latency_ms, `HERMES_HOME` path, hermes commit (best-effort from `git -C <repo_path> rev-parse HEAD`).
- All bridge-equivalent calls emit debug logs. No Node bridge here — pure Python imports.

### 3.7 Threat model & trust boundary

**Assumption: the hermes repo at `hermes.repo_path` is trusted code, controlled by the same team running the evaluation.** Typical deployment: a local git clone under `/Data3/shutong.shan/hermes-agent` or similar, pinned to a known commit.

**Implications of path-mounted in-process import:**
- Plugin code runs inside the evaluator process with full access to: environment variables (incl. `LLM_API_KEY`, `HONCHO_API_KEY`, etc.), the filesystem, and shared Python globals.
- A buggy hermes checkout can corrupt the evaluator process. A malicious one could exfiltrate credentials.

**Mitigations in this design:**
- Hermes repo is **vendored explicitly** via yaml `repo_path` or env var, never auto-fetched or auto-updated. Operator controls what code runs.
- Per-conversation sandbox directory isolates filesystem side-effects (except env vars).
- `handle.json` captures the hermes commit so a run is reproducible and a post-hoc diff against known-good is possible.

**Explicitly not mitigated in v1:**
- Credential isolation (plugin code sees all process env). If evaluating untrusted plugins becomes a requirement, move to subprocess isolation with scrubbed env (same hardening path as §3.3.1's process-pool note).
- Plugin-side LLM calls can exfiltrate content over the network (that's literally what cloud plugins do — honcho/hindsight send data to their service). This is documented behavior, not a leak.

If the trust assumption ever fails (e.g. we want to benchmark a third-party hermes fork we didn't audit), **this adapter is unsafe as-is**; use subprocess isolation or vendor a pinned pip package before enabling.

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
   - `session_id` contract: stub records every `session_id` it sees across `initialize`, `sync_turn`, `on_session_end`, `prefetch` — assert the set has cardinality 1 and equals `conversation_id`.
   - Failure mode: stub's `initialize` raises → `handle.run_status="failed"` + no crash of pipeline.

4. `test_hermes_concurrency.py` (regression guard for §3.3.1)
   - Stub provider's `sync_turn` sleeps briefly and records the `HERMES_HOME` observed via `os.environ` at call time.
   - Launch `add()` for two conversations concurrently via `asyncio.gather`.
   - Assert every recorded `HERMES_HOME` matches the conversation that owned the call — no cross-contamination.
   - Assert serialization: at any instant, at most one stub call is in flight (tracked via a shared counter).

5. `test_hermes_integration_smoke.py` (marked `@pytest.mark.integration`, skipped without `HERMES_REPO_PATH`)
   - Run `add() → search() → answer()` on a 2-turn synthetic conversation using `holographic` plugin.
   - Assertions: non-empty prefetch context; answer returns non-empty string.

Coverage target: 80%+ on new adapter code (`hermes_adapter.py`, `hermes_runtime.py`, `hermes_ingestion.py`).

Not in scope: cross-plugin integration tests (each plugin has its own out-of-band dependencies).

---

## 6. Risks & open questions

1. **Plugin quality varies.** Honcho requires live API. Hindsight requires API URL. Holographic needs `numpy` (optional dep). We ship variants but don't guarantee all are runnable out of the box — `is_available()` gate handles the "skip this run" case.
2. **`sync_turn` is noisy per-turn for some plugins.** Honcho may rate-limit on bulk ingest. Adapter logs but doesn't retry — reruns handle it; if it becomes a real problem we'll add backoff in a follow-up.
3. **Numpy / sqlite availability.** Holographic imports `numpy` only when HRR is used; missing `numpy` → degrades gracefully. If the evaluation env doesn't have numpy, we skip that plugin in `is_available()`.
4. **Plugin-side LLM calls.** Some plugins (honcho auto-summarization) make their own LLM calls inside `initialize`/`sync_turn`. That LLM key is configured in the plugin's own env vars, not in the adapter's `llm:` block. The yaml must make this distinction clear — plugin LLM config stays outside the adapter's `llm:` section.
5. **Single-executor throughput.** §3.3.1 serializes all Hermes calls process-wide. If a plugin's `sync_turn` is slow (e.g. cloud round-trip) and LoCoMo has many turns, add-stage wall-clock grows linearly. Acceptable at current scale; if bottleneck appears, promote to process-pool (§3.3.1 future-hardening note).

**Previously open, now resolved in this revision:**
- ~~HERMES_HOME isolation~~ → §3.3.1 single-executor + per-call env swap.
- ~~Session boundary semantics~~ → §3.2 fixed session-key contract: `session_id = conversation_id`; `on_session_end` called once per conversation.
- ~~Path-mounted import trust~~ → §3.7 threat model documents trusted-repo assumption; subprocess hardening deferred.

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
