---
name: write-eval-adapter
description: Use after discover-memory-frameworks has produced a candidate list. For each candidate (one at a time), this skill writes an adapter at `evaluation/src/adapters/<name>_adapter.py`, a system config at `evaluation/config/systems/<name>.yaml`, and a one-line addition to `evaluation/src/adapters/registry.py`. The adapter treats the candidate as a black-box local service (in-process SDK or localhost HTTP). Triggers when the routine prompt says "write adapter for <system>", "integrate <system>", or chains into this step after discovery. Does NOT run smoke tests or open PRs — that is the next skill's job.
---

# Write Eval Adapter

This skill is the **second step** of every auto-bench routine run. Its job is to produce a working, registered adapter for ONE candidate memory framework at a time.

## Base-class choice (decide FIRST)

**Default: `OnlineAPIAdapter`.** Evidence from the repo: 5 out of 5 non-native integrated adapters (`mem0`, `zep`, `memos`, `memu`, `evermemos_api`) inherit `OnlineAPIAdapter` regardless of transport (SDK / HTTP / SaaS). The "SDK vs HTTP" dichotomy isn't the right axis — **the right axis is whether the candidate's add/search/answer can be split into the template's 4 hooks**.

`OnlineAPIAdapter` hands you for free:

1. **`answer()` built on `LLMProvider` (Sophnet)** — this is THE fairness-baseline requirement. Every integrated system's answer stage goes through this same LLMProvider path, so cross-system comparisons stay apples-to-apples. Skip it and you're on the hook for re-implementing ~40 lines AND making sure you route to Sophnet not the candidate's default LLM.
2. **Dual-perspective** for LoCoMo's `speaker_a` / `speaker_b` (~100 lines of template logic across `_search_single_perspective` / `_search_dual_perspective` / `_build_dual_search_result`).
3. **Conversation-level concurrency** — `num_workers` semaphore baked in.
4. **Batching with retry** (`_batch_messages_with_retry`), `_extract_user_id`, role/content determination, prompt loading from `evaluation/config/prompts.yaml`.

You only implement 4 hooks:
- `_add_user_messages(conv, messages, speaker, **kwargs)` — ingest one speaker's messages for one conversation
- `_search_single_user(query, conversation_id, user_id, top_k, **kwargs)` — retrieve top-k memories for one user
- `_build_single_search_result(...)` — wrap results into a `SearchResult`
- `_build_dual_search_result(...)` — dual-perspective variant (usually 5-10 lines using the template's formatted-context helper)

Reference: `evermemos_api_adapter.py` (HTTP), `mem0_adapter.py` (SaaS SDK), `zep_adapter.py` (SaaS SDK). All three are OnlineAPIAdapter regardless of transport — proving the decision is about shape, not transport.

### Use `BaseAdapter` ONLY when one of these is true

Verified against live routine outputs (simplemem / amem / gam):

**(a) The candidate bundles search + answer in a single call.** GAM's only query API is `wf.request(question)` — it runs retrieval + reasoning + answer together in one ReAct loop. There is no way to split it into `_search_single_user` + template `answer()`. GAM's BaseAdapter choice was correct.

**(b) You're writing a native re-implementation.** Like `evermemos_adapter.py` itself — heavy multi-stage pipelines, rich progress bars, depth-first custom orchestration. The template would be in the way.

**(c) The candidate's shape truly doesn't map to 4 hooks.** Rare. If you find yourself faking `_add_user_messages` to do nothing or stuffing an unrelated call into `_search_single_user`, the template is the wrong fit.

### Cautionary tale — simplemem and amem got it wrong

Live routine runs produced BaseAdapter adapters for simplemem and amem. Both candidates DO fit the 4 hooks (`add_dialogue` → `_add_user_messages`, `retrieve` / `search` → `_search_single_user`). Both adapters ended up MANUALLY re-implementing `answer()` with `from memory_layer.llm.llm_provider import LLMProvider` — exactly the 40-line boilerplate OnlineAPIAdapter hands you free. Don't repeat this. If the candidate has split `add` + `search` entry points, use OnlineAPIAdapter.

Do NOT use `mem0_adapter.py` as the API-method-naming template — it wraps Mem0's SaaS endpoint which Rule 1 rejects for new candidates. But DO use it as the **structural** template for how a typical OnlineAPIAdapter subclass is shaped.

## Patterns from live routine runs (2026-04)

These are patterns the routine agent rediscovered on its own across `simplemem` / `amem` / `gam` — promote them here so subsequent runs don't burn cycles re-deriving them.

### Repo clone via YAML `repo:` block

Candidates often ship a PyPI package that is NOT the reference implementation (or ship no PyPI package at all). When you need the git repo, declare:

```yaml
repo:
  git_url: "https://github.com/<owner>/<name>.git"
  clone_dir: "/tmp/candidate/<name>"
```

The run-bench skill (TODO on run-bench: add) can `git clone` this at smoke time. Until then, the ROUTINE_PROMPT workflow step 1 tells the agent to clone into `/tmp/candidate/<name>/` explicitly.

### `_ensure_<name>_importable()` helper

Standardized shape — insert the cloned repo at the **front** of `sys.path`, optionally evict conflicting `sys.modules` entries:

```python
<NAME>_REPO_DIR = Path(os.environ.get("<NAME>_REPO_DIR", "/tmp/candidate/<name>"))

def _ensure_<name>_importable(repo_dir: Path) -> None:
    if not repo_dir.exists():
        raise RuntimeError(
            f"<name> repo not found at {repo_dir}. Clone it: "
            f"`git clone <git_url> {repo_dir}` or set <NAME>_REPO_DIR."
        )
    repo_str = str(repo_dir.resolve())
    sys.path[:] = [repo_str] + [p for p in sys.path if p != repo_str]

    # OPTIONAL — only if the candidate has generic top-level packages (core, utils,
    # main, database, models, config) that collide with EverOS's src/ layout.
    # SimpleMem has this problem; A-MEM and GAM do not. When in doubt, include it —
    # it's a no-op if nothing conflicts.
    conflicting = ("config", "main", "core", "database", "utils", "models")
    for mod_name in list(sys.modules):
        if mod_name in conflicting or any(mod_name.startswith(p + ".") for p in conflicting):
            del sys.modules[mod_name]
```

### Config-injection pattern (two variants)

Candidates consume LLM/embed config differently. Pick:

- **Env-var injection** (A-MEM, GAM) — simpler. Set `os.environ["OPENAI_API_KEY"]` / `os.environ["OPENAI_BASE_URL"]` BEFORE the first `from candidatepkg import ...`. The openai SDK honors these on client construction.
- **Module-constant mutation** (SimpleMem) — when the candidate has a `config.py` with module-level constants that its components read at init time (not per-call), `import config as simplemem_config` and overwrite the constants explicitly. Env vars alone don't work for this pattern.

If you can't tell which pattern fits from the README, do BOTH (env vars + module mutation). Double-setting is harmless.

### Per-conversation isolation

LoCoMo has 10 conversations and the harness processes them serially. Pick ONE isolation strategy:

- **Per-conversation instance** (SimpleMem): construct one `<System>` object per conv, persisted under `output_dir/<store>/` with a table name derived from the conv id. Clean boundaries.
- **Shared instance + tag filter** (A-MEM): one system for the whole run, tag every write with `conv:<conversation_id>`, over-fetch + filter at search time. Use when the candidate's `__init__` is expensive (model loads, big index builds).
- **Per-conversation directory** (GAM): filesystem-backed candidates work well with `output_dir/<conversation_id>/` and let the candidate reload its state from disk.

Never share state across conversations without a tag or directory — you'll get cross-conv leakage and misleading scores.

## When to use

- After `discover-memory-frameworks` returns a non-empty `candidates` list.
- For each candidate: invoke this skill once. Sequential, not parallel — each call touches `registry.py` and can race.
- Do NOT use this skill on `evermemos` or any `status: integrated` system in `seen_systems.json`.
- Do NOT use this skill to modify an existing adapter — this skill only creates new files.

## Preflight: upstream fork-sync collision check

Run this BEFORE writing any file for a new candidate. The routine operates on the `DuffyCoder/EverOS` fork; if the upstream `EverMind-AI/EverMemOS` already added an adapter with the same name (via a human PR, not this routine), writing our skeleton will either clobber their file on the next sync or produce a conflicting registry entry:

```bash
# Require the upstream remote to be configured — setup.sh should ensure this,
# but be defensive because routine sessions don't always control git state.
git remote get-url upstream >/dev/null 2>&1 || {
  echo "ABORT: no 'upstream' remote configured; cannot check for name collision"
  exit 1
}

git fetch upstream main --quiet || {
  echo "ABORT: upstream fetch failed; cannot verify adapter name is unclaimed"
  exit 1
}

# Check the two files this skill is about to create.
for path in \
  "evaluation/src/adapters/<name>_adapter.py" \
  "evaluation/config/systems/<name>.yaml"
do
  if git show "upstream/main:${path}" >/dev/null 2>&1; then
    echo "ABORT: upstream/main already contains ${path}"
    echo "       Record candidate as status=needs-revisit in seen_systems.json"
    echo "       and skip. Do NOT write files that would collide with upstream."
    exit 1
  fi
done

# Also check the registry entry key — upstream may have added a mapping
# even if their file path differs.
if git show "upstream/main:evaluation/src/adapters/registry.py" 2>/dev/null | \
   grep -q "\"<name>\":"
then
  echo "ABORT: upstream/main registry.py already maps '<name>' to an adapter"
  exit 1
fi
```

If any of these aborts fires, mark the candidate with `status: "needs-revisit"` in `seen_systems.json` with a note explaining the upstream collision, and let a human resolve the name conflict in a separate PR.

## Hard rules (the non-negotiables)

**Rule A — Black-box local integration.** The new adapter MUST inherit from `OnlineAPIAdapter` (from `evaluation.src.adapters.online_base`) — see the naming clarification above. Do NOT inherit from `BaseAdapter` directly. Do NOT import anything from `src/memory_layer/` or `src/agentic_layer/` — those are EverMemOS internals reserved for the privileged `evermemos_adapter.py` path. The candidate must be treated as a black box: call its public SDK or HTTP endpoints only, never reach into EverMemOS primitives.

**Rule B — Force LLM/embedding to the fairness-baseline provider.** The system config's `llm:` block MUST read `api_key: "${LLM_API_KEY}"` and `base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"` and `model: "openai/gpt-4.1-mini"`. The baseline is **Sophnet** (same stack as the integrated `evermemos` system — keeps cross-system comparisons apples-to-apples). If the candidate also has its own internal LLM/embedding config (e.g. writes calls to Ollama or a bundled local model), the adapter's `__init__` MUST override those at runtime using `os.environ["LLM_BASE_URL"]` and `os.environ["LLM_API_KEY"]` before constructing the candidate's client.

**Rule C — Declare candidate deps in YAML, never in `pyproject.toml`.** If the candidate needs pip packages that are NOT in `pyproject.toml [project.optional-dependencies] evaluation-full`, declare them in the candidate's system YAML under a `python_deps:` list (see template below). The run-bench skill installs them ephemerally via `uv run --with <pkg>` at smoke/full time. This overlay:

- leaves `pyproject.toml` and `uv.lock` completely untouched (verified — main-project hashes are byte-identical before and after);
- caches by the `--with` content hash, so repeat runs for the same candidate are warm;
- is transparent to the main EverOS venv — `import <candidatepkg>` in the main venv still fails (no pollution, no locking).

So: never edit `pyproject.toml`. Always populate `python_deps:` in the YAML. The adapter module's `import <candidatepkg>` line can be unconditional — the package WILL be present at smoke/full time because run-bench puts it there.

Keep a defensive `try/except ImportError` around the candidate import ONLY if the import is expensive (big ML stack) or if you want a friendlier error when someone runs the adapter outside `uv run --with` (e.g. from a dev REPL). For most candidates, plain `from candidatepkg import X` is fine.

If `uv run --with` itself fails to install the declared deps (version pin unresolvable, wheel build fail, network fail), run-bench emits `[install-failed]`. If it installs but conflicts with the main venv's resolved deps, run-bench emits `[dep-conflict]`. Neither case is handled by this skill — just declare the deps accurately.

## Adapter file template

Write to `evaluation/src/adapters/<name>_adapter.py` (flat file, not subdir — the repo convention is flat files; only `evermemos/` and `openclaw/` are subdirs because they are privileged re-implementation paths).

Use this skeleton. Fill in the four clearly-marked TODO blocks.

```python
"""
<SystemName> Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- <One paragraph, paraphrased from candidate README. No more than 15 words verbatim.>
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


@register_adapter("<name>")
class <SystemName>Adapter(OnlineAPIAdapter):
    """
    <SystemName> adapter (local deployment, black-box integration).

    Config example:
    ```yaml
    adapter: "<name>"
    base_url: "http://localhost:<port>"  # or SDK-only: omit
    api_key: ""
    num_workers: 5
    llm:
      model: "openai/gpt-4.1-mini"
      api_key: "${LLM_API_KEY}"
      base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
    ```
    """

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)

        # --- TODO 1: force-rewrite candidate's LLM/embedding env (Rule B) ---
        # If the candidate reads LLM config from env at import time, set env vars
        # BEFORE importing / constructing its client. Example:
        os.environ.setdefault("OPENAI_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
        os.environ.setdefault("OPENAI_API_KEY", os.environ.get("LLM_API_KEY", ""))

        # --- TODO 2: construct the candidate client (Rule A, Rule C) ---
        # Plain import — Rule C says the candidate package will be present
        # at smoke/full time via run-bench's `uv run --with` overlay built
        # from the `python_deps:` block in this candidate's system YAML.
        # Wrap in try/except only if the ML stack is huge or you want a
        # friendlier error when someone runs the adapter outside uv run --with.
        from candidatepkg import CandidateClient  # type: ignore

        self.base_url = str(config.get("base_url", "") or "").rstrip("/")
        self.api_key = str(config.get("api_key", "") or "")
        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))

        self.client = CandidateClient(
            base_url=self.base_url or None,
            api_key=self.api_key or None,
            # If the client accepts llm overrides, wire them from config["llm"] here.
        )
        self.console = Console()
        print(f"   <SystemName> client constructed (base_url={self.base_url or 'sdk-local'})")

    # ---- Rule: most candidates do not support dual-perspective group chat. ----
    #           Override only if the candidate explicitly supports multiple user_ids.
    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return super()._need_dual_perspective(speaker_a, speaker_b)

    # --- TODO 3: ingest (Stage 1 — add) ---
    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        user_id = self._extract_user_id(conv, speaker=speaker)
        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        for attempt in range(self.max_retries):
            try:
                # Call the candidate's ingest API. Shape guesses:
                #   self.client.add(messages=messages, user_id=user_id, ...)
                # or looped per-message:
                #   for m in messages: self.client.ingest(user_id, m["content"], ...)
                # Use whichever matches the candidate's public API.
                await asyncio.to_thread(
                    self.client.add,          # TODO: replace with real method name
                    messages=messages,
                    user_id=user_id,
                )
                break
            except Exception as e:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        if progress is not None and task_id is not None:
            progress.update(task_id, advance=len(messages))
        if self.request_interval > 0:
            await asyncio.sleep(self.request_interval)
        return None

    # --- TODO 4: retrieve (Stage 2 — search) ---
    async def _search_single_user(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        raw = await asyncio.to_thread(
            self.client.search,           # TODO: replace with real method name
            query=query,
            user_id=user_id,
            top_k=top_k,
        )

        # Normalize to standard format required by OnlineAPIAdapter
        out: List[Dict[str, Any]] = []
        for item in (raw or []):
            content = item.get("text") or item.get("memory") or item.get("content") or ""
            ts = item.get("timestamp") or item.get("created_at") or ""
            out.append({
                "content": f"{ts}: {content}".strip(": ").strip() if ts else content,
                "score": float(item.get("score", 0.0)),
                "user_id": user_id,
                "metadata": {"raw": item},
            })
        out.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return out[: int(top_k)]

    def _build_single_search_result(
        self,
        query: str,
        conversation_id: str,
        results: List[Dict[str, Any]],
        user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> SearchResult:
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results[: int(top_k)],
            retrieval_metadata={
                "system": "<name>",
                "top_k": int(top_k),
                "dual_perspective": False,
                "user_ids": [user_id],
            },
        )

    def _build_dual_search_result(
        self,
        query: str,
        conversation_id: str,
        all_results: List[Dict[str, Any]],
        results_a: List[Dict[str, Any]],
        results_b: List[Dict[str, Any]],
        speaker_a: str,
        speaker_b: str,
        speaker_a_user_id: str,
        speaker_b_user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> SearchResult:
        # Reuse default template from prompts.yaml
        speaker_a_text = "\n".join(r["content"] for r in results_a) if results_a else "(No memories found)"
        speaker_b_text = "\n".join(r["content"] for r in results_b) if results_b else "(No memories found)"
        template = self._prompts["online_api"].get("templates", {}).get("default", "")
        formatted = template.format(
            speaker_1=speaker_a,
            speaker_1_memories=speaker_a_text,
            speaker_2=speaker_b,
            speaker_2_memories=speaker_b_text,
        )
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=all_results,
            retrieval_metadata={
                "system": "<name>",
                "top_k": int(top_k),
                "dual_perspective": True,
                "user_ids": [speaker_a_user_id, speaker_b_user_id],
                "formatted_context": formatted,
            },
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "<SystemName>",
            "type": "online_api",
            "adapter": "<SystemName>Adapter",
        }
```

### Two skeleton variants — pick at TODO 2

**Variant A — Python SDK (in-process).** The candidate exposes a class you construct with kwargs. Pattern: `CandidateClient(...)` with in-memory storage, OR a client that talks to localhost via its own transport. Use `asyncio.to_thread(self.client.method, ...)` in `_add_user_messages` and `_search_single_user` to bridge sync SDKs. Example reference: the Mem0 local mode pattern (if it were implemented) or any `Memory()` class.

**Variant B — HTTP API.** The candidate ships a docker-compose file with a REST server on `localhost:<port>`. Pattern: use `aiohttp.ClientSession` directly (see `evermemos_api_adapter.py` at `_request_json_with_retry`). Do NOT use `requests` — this codebase is async. Pass `base_url` and `api_key` through config.

## System config template

Write to `evaluation/config/systems/<name>.yaml`:

```yaml
# <SystemName> System Configuration (auto-generated by write-eval-adapter skill)

name: "<name>"
version: "1.0"
description: "<SystemName> — <one-line paraphrased purpose>"

adapter: "<name>"

# <SystemName>-specific configuration
base_url: "http://localhost:<port>"  # only for HTTP candidates; remove for SDK-only
api_key: ""                           # most local candidates don't require auth
max_retries: 3
request_interval: 0.0

# Candidate's pip deps (Rule C). Installed EPHEMERALLY via `uv run --with <pkg>`
# at smoke/full time — does NOT touch pyproject.toml or uv.lock. Pin versions
# that you observed in the candidate's README / pypi page; use `>=x.y.z` if the
# candidate has a stable release history, `==x.y.z` if you want reproducibility.
# Omit or leave empty list [] if the candidate is already in evaluation-full
# (mem0, zep) or is pure-stdlib.
python_deps:
  - "candidatepkg>=1.0.0"
  - "candidate-extra-dep>=0.5"

# If the candidate's PyPI package is not the reference implementation (or it
# has no PyPI release), point at the git repo so the agent can clone it into
# /tmp/candidate/<name>/ and sys.path-inject. See "Patterns from live routine
# runs" above for the full shape.
repo:
  git_url: "https://github.com/<owner>/<name>.git"
  clone_dir: "/tmp/candidate/<name>"

# Concurrency (conversation-level; keep low until smoke test passes)
num_workers: 5

# Search configuration
search:
  top_k: 20

# LLM configuration — FORCED to fairness-baseline provider (Sophnet) per Rule B, non-negotiable
llm:
  provider: "openai"
  model: "openai/gpt-4.1-mini"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
  temperature: 0
  max_tokens: 32768

answer:
  max_retries: 3
```

If the candidate needs additional env vars (e.g. a license key required just for startup, not for inference), add them under a top-level `env:` key for documentation only. They must be actual env vars on the cloud container — do NOT hardcode values.

## Register the adapter

Add ONE line to `evaluation/src/adapters/registry.py` inside `_ADAPTER_MODULES`:

```python
"<name>": "evaluation.src.adapters.<name>_adapter",
```

Place it alongside the other `# Online API systems` comment block. Do not reorder existing entries. Do not remove any entry.

## Batching contract for Rule 3 (RAM-aware split)

The evaluation CLI supports `--from-conv I --to-conv J` natively on LoCoMo. The adapter does NOT need to do anything special for batching — the CLI's `--clean-groups` + `--from-conv`/`--to-conv` pair is sufficient, provided:

- The adapter's `add()` is idempotent within a conversation (safe to re-run a conv on the same `user_id` if a batch is retried). Most external APIs are idempotent by message ID; if the candidate is not, include `clean_before_add: true` in its config and implement the cleanup in `prepare()` following `mem0_adapter.prepare`.
- The adapter writes nothing to `evaluation/results/` directly — the harness owns that path.

## Failure modes and what to record

When writing the adapter, pre-decide how the next-step (run-bench) skill will classify a failure:

| Symptom at smoke time | Cause | seen_systems.json status |
|---|---|---|
| `uv run --with` exits non-zero (version pin unresolvable, wheel build fail, network fail) | Rule C install path failed | `status: failed`, `rejection_reason: "ephemeral install failed: <tail of uv output>"`, PR tag `[install-failed]` |
| `uv run --with` succeeds but harness imports break with `ImportError` / `VersionConflict` | candidate dep conflicts with main evaluation-full deps | `status: failed`, `rejection_reason: "dep conflict with main venv"`, PR tag `[dep-conflict]` |
| `ImportError` on candidate package AFTER uv install succeeded | `python_deps:` listed wrong package name | fix `python_deps:` in YAML, re-run smoke |
| HTTP 401/403 on `base_url` | candidate requires auth that this env can't provide | `status: failed`, `rejection_reason: "auth required"` |
| `AttributeError: 'CandidateClient' has no attribute 'add'` | TODO 2/3 method names wrong | fix in-place, re-run smoke |
| Candidate returns empty results for all queries on `--smoke` | search API wired incorrectly OR candidate requires background indexing | try `post_add_wait_seconds: 60` in config; if still empty → `status: failed` |
| OOM during smoke with `--smoke-messages 20` | candidate has hidden infra requirement | mark `tier: oversize-infra`, do NOT open PR from this run |
| Candidate works but scores 0 on LoCoMo | adapter is wired but output format mismatches | leave adapter, open PR with `[zero-score]` tag so humans can debug |

## What NOT to do in this skill

- Do NOT run smoke tests. That is the next skill.
- Do NOT open a PR. The routine's main prompt opens the PR at the end.
- Do NOT git commit. The routine's main prompt batches commits.
- Do NOT touch `evermemos_adapter.py`, `evermemos/` subdir, or `openclaw/` subdir.
- Do NOT modify existing system YAMLs.
- Do NOT guess the candidate's API method names — read the README and at least one example from the candidate repo. If the README is ambiguous, prefer the lowest-risk guess (`.add()` for ingest, `.search()` for retrieval) but note the uncertainty in a `# TODO(auto-bench):` comment.
- Do NOT add comments explaining the 4-stage pipeline inside the adapter — the base class docstring already covers it. Keep the adapter file tight.
- Do NOT copy `evermemos_adapter.py` as a starting point — it's the privileged re-implementation path and does not inherit from `OnlineAPIAdapter`. Use `evermemos_api_adapter.py` as the reference instead.
- Do NOT mirror `mem0_adapter.py` — it targets Mem0's SaaS endpoint, which violates Rule 1. Rule 1 was already enforced at the discovery step, so any candidate reaching this skill is local; use the local reference.
