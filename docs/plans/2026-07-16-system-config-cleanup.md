# Evaluation System Configuration Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize and strictly validate `evaluation/config/systems/` while preserving all 36 existing `--system` ids, requested-id result paths, and adapter-consumed behavior.

**Architecture:** Add a registry-backed system-config loader that resolves stable ids to categorized files, follows a single-parent `extends` chain, expands environment markers, validates adapter-specific schemas, and records redacted resolution metadata. Migrate one configuration family at a time, using aliases for duplicate public ids and compatibility fixtures to prove structure-only changes do not alter runtime semantics.

**Tech Stack:** Python 3.12, PyYAML, Pydantic 2, pytest, uv, Git.

---

## Target layout and public-id mapping

The completed tree should use this physical classification:

```text
evaluation/config/systems/
├── index.yaml
├── _bases/
│   ├── hermes.yaml
│   ├── openclaw-native.yaml
│   ├── openclaw-docker.yaml
│   ├── openclaw-memcore-session-bundle.yaml
│   └── openclaw-openviking.yaml
├── canonical/
├── experiments/
├── ablations/
└── tooling/
```

The 36 public ids remain stable:

| Category | Public ids |
| --- | --- |
| Canonical | `evermemos`, `evermemos_cloud_api`, `evermemos_local_api`, `mem0`, `memos`, `memu`, `zep`, `hermes-holographic`, `hermes-honcho`, `hermes-hindsight`, `openclaw`, `openclaw-fts`, `openclaw-fts-noflush`, `openclaw-hybrid-noflush`, `openclaw-vector`, `openclaw-vector-noflush`, `openclaw-docker`, `openclaw-docker-evermemos`, `openclaw-docker-mem0`, `openclaw-docker-memcore-session-bundle`, `openclaw-docker-openviking-session-bundle-memcore`, `openclaw-docker-openviking-session-bundle-noop` |
| Alias | `hermes` → `hermes-holographic`; `openclaw-hybrid` → `openclaw` |
| Experiment | `openclaw-agent-local`, `openclaw-hypercompositor`, `openclaw-native-embed`, `openclaw-native-noembed`, `openclaw-noop`, `openclaw-docker-hypercompositor`, `openclaw-docker-memclaw`, `openclaw-docker-stub` |
| Ablation | `openclaw-docker-memcore-session-bundle-emptytail`, `openclaw-docker-memcore-session-bundle-weaktail`, `openclaw-docker-openviking-session-bundle-noop-fixpack` |
| Tooling | `openclaw-docker-openviking-session-bundle-noop-serial` |

Physical filenames should retain the public-id basename when an entry has its
own YAML. Alias entries have no duplicate YAML.

### Task 1: Lock the 36-id legacy compatibility baseline

**Files:**
- Create: `tests/evaluation/fixtures/system_configs_before_cleanup.json`
- Create: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`
- Create: `tests/evaluation/generate_system_config_baseline.py`
- Create: `tests/evaluation/system_config_legacy.py`
- Create: `tests/evaluation/test_system_config_legacy_baseline.py`

**Step 1: Write the legacy inventory test**

Add a test that reads only the current flat `evaluation/config/systems/*.yaml`
files, explicitly excludes the future reserved name `index.yaml`, and asserts
the exact 36 stems listed above.

```python
def test_legacy_system_id_surface_is_locked() -> None:
    assert legacy_system_ids(REPO_ROOT) == EXPECTED_SYSTEM_IDS
```

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_legacy_baseline.py::test_legacy_system_id_surface_is_locked \
  -q
```

Expected: PASS with 36 ids.

**Step 2: Define redacted raw and effective normalization**

In `system_config_legacy.py`, implement:

```python
def normalized_raw_config(path: Path) -> dict[str, Any]: ...
def normalized_effective_config(system_id: str, raw: dict[str, Any]) -> dict[str, Any]: ...
def semantic_sha256(value: Any) -> str: ...
def json_pointer_differences(before: Any, after: Any) -> set[str]: ...
```

The raw view preserves parsed fields but never expands environment markers.
The effective view removes only fields already proven unused:

- `answer.max_retries` for adapters that do **not** inherit
  `OnlineAPIAdapter`;
- `openclaw.prompts`;
- `memos.request_interval`;
- `memu.min_similarity`; and
- `evermemos_cloud_api.search.timeout_seconds`.

Keep `answer.max_retries` for `mem0`, `memos`, `memu`, `zep`, and
`evermemos_api`: `OnlineAPIAdapter.answer()` consumes it as an inner LLM-call
retry count. Do not remove adapter-owned top-level retry or timeout fields.

Normalize the legacy OpenClaw embedding secret representation:

```yaml
api_key: "${SOPH_API_KEY}"
```

to the effective marker form:

```yaml
api_key_env: "SOPH_API_KEY"
```

This lets the later disk-secret fix prove behavior equivalence.

**Step 3: Capture and review the fixture**

Create one fixture entry per id with:

```json
{
  "adapter": "openclaw",
  "canonical_id": "openclaw",
  "default_result_suffix": "locomo-openclaw",
  "raw_config": {},
  "effective_config": {},
  "effective_sha256": "<64 hex>",
  "raw_sha256": "<64 hex>"
}
```

Use deterministic JSON ordering and preserve `${VAR}` markers. Review the
fixture to ensure it contains no expanded credentials or machine-local secret
values.

Create `system_config_approved_deltas.yaml` with an initially empty `deltas`
mapping. Later tasks add exact JSON pointers, classification
(`structure-only`, `security-fix`, or `behavior-fix`), and rationale before a
known raw/effective difference is accepted. The compatibility test must fail on
any unlisted difference. Store the redacted raw mapping itself, not only its
digest, so the test can calculate those exact JSON-pointer differences after
files begin moving.

Implement a deterministic one-shot generator:

```bash
PYTHONPATH=src uv run python \
  tests/evaluation/generate_system_config_baseline.py \
  --output tests/evaluation/fixtures/system_configs_before_cleanup.json
```

It must sort ids and mapping keys, reject `index.yaml`, refuse to overwrite an
existing fixture unless `--force` is explicitly supplied, and never read
secrets from the process environment. The committed fixture remains immutable
after Task 1; later migrations compare registry-resolved configs against it
rather than regenerating it.

**Step 4: Add fixture integrity tests**

Assert:

- every YAML parses to a mapping;
- every entry has an adapter;
- all adapters appear in `list_adapters()`;
- the fixture has exactly the same 36 ids;
- only the two approved duplicate ids have a different `canonical_id`; and
- recomputed raw/effective digests match.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_legacy_baseline.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/evaluation/fixtures/system_configs_before_cleanup.json \
  tests/evaluation/fixtures/system_config_approved_deltas.yaml \
  tests/evaluation/generate_system_config_baseline.py \
  tests/evaluation/system_config_legacy.py \
  tests/evaluation/test_system_config_legacy_baseline.py
git commit -m "test(eval): lock legacy system configuration surface"
```

### Task 2: Add the public system index

**Files:**
- Create: `evaluation/config/systems/index.yaml`
- Create: `evaluation/src/config/__init__.py`
- Create: `evaluation/src/config/system_index.py`
- Create: `tests/evaluation/test_system_config_index.py`

**Step 1: Write failing index-model tests**

Specify immutable entry models:

```python
@dataclass(frozen=True)
class SystemIndexEntry:
    system_id: str
    adapter: str
    category: Literal["canonical", "alias", "experiment", "ablation", "tooling"]
    status: Literal["active", "compatibility", "experimental", "deprecated"]
    path: PurePosixPath | None
    alias_of: str | None
    replacement: str | None
    description: str
    docs: PurePosixPath | None
```

Tests must reject:

- unknown top-level or entry keys;
- invalid ids;
- entries with both or neither of `path` and `alias_of`;
- aliases whose category is not `alias`;
- aliases whose declared adapter differs from their resolved target;
- non-alias entries without paths;
- unregistered adapters;
- duplicate paths for independent runnable entries; and
- a total public-id set different from the legacy 36.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py -q
```

Expected: FAIL because the index and loader do not exist.

**Step 2: Implement strict index loading**

Implement:

```python
class SystemIndexError(ValueError): ...

def load_system_index(path: Path = DEFAULT_SYSTEM_INDEX_PATH) -> SystemIndex: ...
def get_system_entry(index: SystemIndex, system_id: str) -> SystemIndexEntry: ...
def resolve_alias(index: SystemIndex, system_id: str) -> tuple[str, tuple[str, ...]]: ...
```

Use `yaml.safe_load`, explicit allowed-key sets, `PurePosixPath`, and
`list_adapters()`. Alias resolution must detect cycles and unknown targets.

**Step 3: Create the initial index without moving files**

Point non-alias entries at the current flat YAML basenames. Define:

```yaml
schema_version: 1
systems:
  openclaw:
    adapter: openclaw
    category: canonical
    status: active
    path: openclaw.yaml
    description: Default hybrid OpenClaw preset.
  openclaw-hybrid:
    adapter: openclaw
    category: alias
    status: compatibility
    alias_of: openclaw
    description: Compatibility alias for the default hybrid preset.
```

Do the same for the Hermes alias and classify all other ids according to the
mapping above.

**Step 4: Run tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_legacy_baseline.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add evaluation/config/systems/index.yaml evaluation/src/config \
  tests/evaluation/test_system_config_index.py
git commit -m "feat(eval): index public system configurations"
```

### Task 3: Implement alias and inheritance resolution

**Files:**
- Create: `evaluation/src/config/system_loader.py`
- Create: `tests/evaluation/test_system_config_loader.py`

**Step 1: Write failing loader tests**

Use temporary roots and indexes to cover:

```python
resolved = resolve_system_config(
    "legacy-name",
    index_path=index_path,
    systems_root=systems_root,
    environ={"LLM_API_KEY": "test-key"},
)
assert resolved.requested_id == "legacy-name"
assert resolved.canonical_id == "canonical-name"
assert resolved.alias_chain == ("legacy-name", "canonical-name")
```

Also test:

- one-parent and multi-level `extends`;
- recursive dictionary merge;
- scalar and list replacement;
- deterministic source-chain ordering;
- missing parent;
- inheritance cycle;
- symlink or `..` escape outside the systems root;
- alias-to-alias cycle;
- alias entries cannot be inherited;
- environment substitution compatibility: a set variable wins,
  `${VAR:default}` uses its default, `${VAR:}` becomes empty, and an unset
  `${VAR}` remains the existing empty-string behavior; and
- index adapter must equal the resolved config adapter.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_loader.py -q
```

Expected: FAIL because the loader does not exist.

**Step 2: Implement the resolution object**

```python
@dataclass(frozen=True)
class ResolvedSystemConfig:
    requested_id: str
    canonical_id: str
    category: str
    status: str
    adapter: str
    config: dict[str, Any]
    raw_config: dict[str, Any]
    source_paths: tuple[Path, ...]
    alias_chain: tuple[str, ...]
    warning: str | None
```

`raw_config` retains environment markers. `config` contains the current
runtime substitutions. Neither object mutates caller dictionaries.

**Step 3: Implement secure path and merge handling**

Implement:

```python
def resolve_system_config(...) -> ResolvedSystemConfig: ...
def deep_merge_config(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict: ...
```

Resolve every file strictly under the configured systems root. Reject symlinked
config files. Interpret every index `path` and `extends` value as a POSIX path
relative to the systems root, not relative to the child file or current
working directory. Remove the loader-only `extends` key before returning or
schema validation. Keep single-parent inheritance; do not add list-append
directives.

**Step 4: Verify current aliases**

Add integration assertions:

```python
assert resolve_system_config("openclaw-hybrid").raw_config == \
       resolve_system_config("openclaw").raw_config
assert resolve_system_config("hermes").raw_config == \
       resolve_system_config("hermes-holographic").raw_config
```

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add evaluation/src/config/system_loader.py \
  tests/evaluation/test_system_config_loader.py
git commit -m "feat(eval): resolve system aliases and inheritance"
```

### Task 4: Add strict adapter-specific schemas and policy checks

**Files:**
- Create: `evaluation/src/config/system_schema.py`
- Create: `evaluation/src/config/system_policy.py`
- Create: `tests/evaluation/test_system_config_schema.py`
- Create: `tests/evaluation/test_system_config_policy.py`
- Modify: `evaluation/src/config/system_loader.py`

**Step 1: Write failing schema tests**

Create Pydantic models with `ConfigDict(extra="forbid", strict=True)` for:

- common `llm`, `search`, and `answer` blocks;
- EverMemOS and EverMemOS API;
- Mem0, MemOS, MemU, and Zep;
- Hermes;
- OpenClaw;
- OpenClaw Docker;
- nested `agent_llm`, embedding, OV ingest, Docker, plugin config, and dataset
  override blocks.

Use `int | float` for YAML numeric fields that legitimately appear as either
`0` or `0.0`; strict validation must reject strings without rejecting those
equivalent YAML number forms.

Tests must reject:

- unknown top-level and nested fields;
- wrong types;
- invalid fixed OpenClaw enum values such as backend, retrieval, flush,
  visibility, and answer modes;
- `openclaw.memory_mode` or `openclaw.context_engine_mode` ids absent from
  `evaluation/config/plugin_registry.yaml`, or registered under the wrong
  plugin kind;
- missing adapter-specific required fields;
- an index/config adapter mismatch; and
- non-mapping YAML.

Validation is performed against the runtime config but returns the original
dictionary so Pydantic coercion cannot silently change behavior.

**Step 2: Write failing secret and placeholder tests**

On the raw merged config, require:

- every schema-declared `api_key`, token, or credential value to be
  `${ENV_NAME}` or `${ENV_NAME:}`;
- every `*_env` field, recursively—including `agent_llm.api_key_env`,
  `embedding.api_key_env`, and `ov_ingest.api_key_env`—to be a bare valid
  environment-variable name;
- no `openclaw.embedding.api_key` in final strict mode;
- no likely plaintext secret values; and
- no Docker image containing `PLUGIN_REV`, `TODO`, or unresolved `${...}`.

Allow one narrow authentication exception: `evermemos_api.api_key` may be an
empty string only when its parsed `base_url` host is loopback (`localhost`,
`127.0.0.1`, or `::1`). Add positive and negative tests so a hosted URL cannot
use the empty-key exception.

Initially allow the known legacy OpenClaw embedding field and stub placeholder
through an explicit `allow_legacy=True` policy mode, returning findings rather
than silently accepting them. This mode is removed in Task 11 after both
shipped-config migrations are complete.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py -q
```

Expected: FAIL.

**Step 3: Implement schema selection**

```python
def validate_system_config(
    adapter: str,
    config: Mapping[str, Any],
) -> None: ...
```

Use a model mapping keyed by adapter id. Model all currently observed fields;
do not use unrestricted `dict[str, Any]` for nested runtime blocks except
documented payload maps whose keys are genuinely external.

Do not model plugin selectors as closed `Literal` enums.
`openclaw.memory_mode` and `openclaw.context_engine_mode` remain strings, then
receive kind-aware cross-validation against the existing plugin registry so
future valid ids such as `hindsight-plugin` work without a schema release.
Keep unrelated nested fields such as
`openclaw_docker.environment.memory_mode: native_compiled` in their own fixed
schema.

**Step 4: Integrate validation into the loader**

Resolution order must be:

1. parse and merge raw YAML;
2. validate raw secret/image policy;
3. substitute environment markers;
4. validate adapter schema; and
5. return `ResolvedSystemConfig`.

Add a shipped-config test that resolves all 36 ids in legacy policy mode with a
deterministic fake environment.

**Step 5: Run tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add evaluation/src/config tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py
git commit -m "feat(eval): validate system configuration schemas"
```

### Task 5: Integrate the loader with the evaluation CLI

**Files:**
- Create: `evaluation/src/config/system_metadata.py`
- Create: `evaluation/src/config/cli_support.py`
- Create: `tests/evaluation/test_system_config_cli.py`
- Create: `tests/evaluation/test_system_config_metadata.py`
- Modify: `evaluation/cli.py`
- Modify: `evaluation/README.md`
- Modify: `tests/evaluation/test_artifact_hygiene.py`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`

**Step 1: Write failing CLI-support tests**

Specify:

```python
resolution = resolve_system_for_cli("openclaw-hybrid", ...)
assert resolution.canonical_id == "openclaw"

path = default_result_dir(
    evaluation_root, dataset_id="locomo",
    requested_system_id="openclaw-hybrid", run_name=None,
)
assert path.name == "locomo-openclaw-hybrid"
```

Unknown ids must raise `SystemExit(2)` with close matches. Deprecated entries
must return a warning containing their replacement.

Add plugin-override compatibility tests:

- only resolved adapter `openclaw-docker` calls `apply_plugin_overrides`;
- the same flags on every other adapter remain accepted no-ops, matching the
  current README contract; and
- Docker overrides accept registry ids of the correct kind and reject
  wrong-kind ids before mutating config.

**Step 2: Write failing redacted metadata tests**

Specify `resolved-system-config.json` with:

```json
{
  "schema": "evaluation-system-config/v1",
  "provenance_status": "verified",
  "dataset_id": "locomo",
  "requested_id": "openclaw-hybrid",
  "canonical_id": "openclaw",
  "adapter": "openclaw",
  "category": "alias",
  "status": "compatibility",
  "alias_chain": ["openclaw-hybrid", "openclaw"],
  "source_paths": [],
  "raw_config_sha256": "<sha256>",
  "runtime_config_sha256": "<sha256>",
  "environment_references": ["LLM_API_KEY"],
  "runtime_context": {
    "dataset_name": "locomo",
    "clean_groups": false
  },
  "config": {}
}
```

The recorded config must redact secret fields and retain environment-variable
names. No actual test secret may appear in the file. Record source paths
relative to the systems root, never as machine-specific absolute paths.
Compute both hashes from deterministic redacted representations; a credential
value or a digest derived from an unredacted credential must never be written.
The runtime digest covers both the redacted final config and
`runtime_context`, because those transient values are consumed by adapters.

Add resume-safety tests:

- absent metadata plus an empty/new output directory writes atomically;
- identical existing metadata is verified and reused without rewriting;
- a different dataset, alias/source chain, raw or runtime digest, or runtime
  context raises `SystemExit(2)` before adapter construction; and
- a non-empty legacy output directory with no metadata is refused rather than
  silently mixing an unverifiable checkpoint with a new configuration; and
- explicit `--adopt-legacy-result-dir` performs a one-time adoption, records
  `provenance_status: adopted-legacy-unverified`, emits a prominent warning,
  and never claims that the old checkpoint's configuration was verified.
  Permit adoption only when a recognized legacy checkpoint/progress artifact
  exists and no metadata file exists.

**Step 3: Implement CLI support and metadata writing**

Move CLI-only config handling into:

```python
def resolve_system_for_cli(system_id: str, ...) -> ResolvedSystemConfig: ...
def default_result_dir(...) -> Path: ...
def write_resolved_system_metadata(
    resolution: ResolvedSystemConfig,
    final_runtime_config: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    output_path: Path,
    dataset_id: str,
    adopt_legacy: bool = False,
) -> None: ...
```

Write metadata only after dataset overrides and plugin CLI overrides are
applied, but before adapter construction. Re-run adapter schema validation and
the non-secret runtime policy checks (including image placeholders) after those
overrides. Raw secret policy has already covered every dataset override before
environment expansion; CLI overrides cannot set secret fields.

Build a separate `runtime_context` containing `dataset_name` and
`clean_groups`, include it in metadata and its digest, then inject it into the
adapter config only after final schema validation. Implement metadata as
write-or-verify using a temporary file plus `os.replace`; never overwrite
different provenance in an existing result directory.

Default resume remains automatic for runs that already contain matching
metadata. A pre-migration non-empty result directory requires the explicit
one-time `--adopt-legacy-result-dir` acknowledgement; after adoption, normal
write-or-verify resume applies. Document this migration path next to the
existing automatic-resume instructions in `evaluation/README.md`.

**Step 4: Replace direct flat-file loading**

In `evaluation/cli.py`:

- replace `systems/<id>.yaml` construction with `resolve_system_for_cli`;
- preserve `args.system` for output naming and rerank cleanup;
- call `apply_plugin_overrides` only when the resolved adapter is
  `openclaw-docker`; for other adapters keep all plugin/image flags as accepted
  no-ops and print one concise ignored-flags warning;
- print requested → canonical identity for aliases;
- print experimental/deprecation warnings;
- raise non-zero `SystemExit` for unknown or invalid ids; and
- use the shared `deep_merge_config`.

Do not alter dataset YAML loading.

Switch the legacy compatibility test from direct flat-file loading to
`resolve_system_config()` for all 36 ids. From this point onward:

- stop comparing the live top-level YAML stems to 36; compare the immutable
  fixture id set to the index id set instead (`legacy_system_ids()` remains a
  pre-migration generator helper only);
- effective digests must match unless an approved behavior/security delta says
  otherwise; and
- raw differences must be limited to the exact JSON pointers declared in
  `system_config_approved_deltas.yaml`.

**Step 5: Ensure compact archives retain the record**

Add an artifact-hygiene test proving
`resolved-system-config.json` receives the `resolved_config` inclusion reason.
The existing matcher already recognizes this filename; production archive code
should not change unless the test demonstrates otherwise.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_cli.py \
  tests/evaluation/test_system_config_metadata.py \
  tests/evaluation/test_artifact_hygiene.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add evaluation/cli.py evaluation/src/config evaluation/README.md \
  tests/evaluation/test_system_config_cli.py \
  tests/evaluation/test_system_config_metadata.py \
  tests/evaluation/test_artifact_hygiene.py \
  tests/evaluation/test_system_config_legacy_baseline.py
git commit -m "refactor(eval): load systems through compatibility registry"
```

### Task 6: Migrate EverMemOS and public online-system configs

**Files:**
- Create: `evaluation/config/systems/canonical/evermemos.yaml`
- Create: `evaluation/config/systems/canonical/evermemos_cloud_api.yaml`
- Create: `evaluation/config/systems/canonical/evermemos_local_api.yaml`
- Create: `evaluation/config/systems/canonical/mem0.yaml`
- Create: `evaluation/config/systems/canonical/memos.yaml`
- Create: `evaluation/config/systems/canonical/memu.yaml`
- Create: `evaluation/config/systems/canonical/zep.yaml`
- Modify: `evaluation/config/systems/index.yaml`
- Modify: `evaluation/README.md`
- Delete: the seven corresponding flat YAML files
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`

**Step 1: Add migration compatibility assertions**

Parameterize the seven ids and compare the new loader's effective normalized
configuration to `system_configs_before_cleanup.json`.

Run before moving files:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_legacy_baseline.py -q
```

Expected: PASS against flat paths.

**Step 2: Move and trim comments**

Move the seven files into `canonical/`. Keep field values unchanged. Remove
obvious prose headers and comments that only restate field names, but retain
API semantics and safety notes.

Do not yet rename or activate:

- `memos.request_interval`;
- `memu.min_similarity`;
- `evermemos_cloud_api.search.timeout_seconds`; or
- `answer.max_retries`.

Those are handled separately.

**Step 3: Update index paths**

Change only physical paths. Public ids and status remain unchanged.
Update the active custom-config example in `evaluation/README.md` so it copies
from `canonical/evermemos.yaml`, writes into the appropriate category, and
registers the new id in `index.yaml`; do not keep instructions that create
unindexed root-level YAML.

**Step 4: Run compatibility and schema tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_legacy_baseline.py -q
```

Expected: PASS with unchanged effective digests.

**Step 5: Commit**

```bash
git add evaluation/config/systems evaluation/README.md \
  tests/evaluation/test_system_config_legacy_baseline.py
git commit -m "refactor(eval): classify public memory system configs"
```

### Task 7: Deduplicate the Hermes family

**Files:**
- Create: `evaluation/config/systems/_bases/hermes.yaml`
- Create: `evaluation/config/systems/canonical/hermes-holographic.yaml`
- Create: `evaluation/config/systems/canonical/hermes-honcho.yaml`
- Create: `evaluation/config/systems/canonical/hermes-hindsight.yaml`
- Modify: `evaluation/config/systems/index.yaml`
- Delete: `evaluation/config/systems/hermes.yaml`
- Delete: `evaluation/config/systems/hermes-holographic.yaml`
- Delete: `evaluation/config/systems/hermes-honcho.yaml`
- Delete: `evaluation/config/systems/hermes-hindsight.yaml`
- Modify: `tests/evaluation/test_system_config_loader.py`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`

**Step 1: Add family-specific inheritance tests**

Assert:

- `hermes` resolves through its alias to `hermes-holographic`;
- the source chain includes `_bases/hermes.yaml`;
- each explicit variant changes only its plugin-specific block and required
  ingest-strategy override (`session_end` for holographic,
  `sync_per_turn` for honcho/hindsight); and
- all four effective digests match the legacy fixture.

Expected before migration: FAIL because no Hermes base exists.

**Step 2: Extract the common base**

Move shared adapter, LLM, search, answer, repository, prompt, and ingest
settings into `_bases/hermes.yaml`. Variant files contain:

```yaml
extends: "_bases/hermes.yaml"

hermes:
  plugin: "honcho"
  ingest_strategy: "sync_per_turn"
  plugin_config: {}
```

Do not create a physical `hermes.yaml`; retain it only as the index alias.

**Step 3: Run tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_hermes_adapter.py -q
```

Expected: PASS.

**Step 4: Commit**

```bash
git add evaluation/config/systems \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_legacy_baseline.py
git commit -m "refactor(eval): deduplicate Hermes system presets"
```

### Task 8: Migrate and simplify host-side OpenClaw presets

**Files:**
- Create: `evaluation/config/systems/_bases/openclaw-native.yaml`
- Create or move under `canonical/`:
  - `openclaw.yaml`
  - `openclaw-fts.yaml`
  - `openclaw-fts-noflush.yaml`
  - `openclaw-hybrid-noflush.yaml`
  - `openclaw-vector.yaml`
  - `openclaw-vector-noflush.yaml`
- Create or move under `experiments/`:
  - `openclaw-agent-local.yaml`
  - `openclaw-hypercompositor.yaml`
  - `openclaw-native-embed.yaml`
  - `openclaw-native-noembed.yaml`
  - `openclaw-noop.yaml`
- Modify: `evaluation/config/systems/index.yaml`
- Delete: corresponding flat YAML files, including `openclaw-hybrid.yaml`
- Modify: `evaluation/src/adapters/openclaw/adapter.py`
- Modify: `evaluation/scripts/openclaw_eval_bridge.mjs`
- Modify: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`
- Modify: `tests/evaluation/test_hypercompositor_yaml.py`
- Modify: `tests/evaluation/test_openclaw_bridge_payload.py`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Modify: `tests/evaluation/test_openclaw_resolved_config.py`
- Modify: `evaluation/scripts/run_latency_baseline.sh`
- Modify: `docs/latency-alignment-runbook.md`

**Step 1: Add family compatibility tests**

Assert all 12 host-side OpenClaw ids preserve effective legacy semantics and:

```python
assert resolve_system_config("openclaw-hybrid").canonical_id == "openclaw"
```

Update the Hypercompositor structural test to load by public id through
`resolve_system_config`, not through a hard-coded flat path.

**Step 2: Extract the native base and small overrides**

Put only fields present with the same value in **every** inheriting preset into
`_bases/openclaw-native.yaml`: common LLM, search, answer, repository, and
prompt-independent runtime settings. Do not put optional `embedding` or
`agent_llm` blocks into the common base; `fts` and `noembed` presets must
continue to have those keys absent. Keep optional blocks in the variants that
use them rather than inventing null-as-delete semantics.

Each preset should express only meaningful axes such as:

```yaml
extends: "_bases/openclaw-native.yaml"

openclaw:
  backend_mode: "fts_only"
  flush_mode: "disabled"
```

Remove `openclaw.prompts` from every migrated preset because it has no code
consumer. Before removal, add the exact affected JSON pointers to
`system_config_approved_deltas.yaml` as `structure-only`. Keep the
behavior-neutral removal covered by the effective baseline.

**Step 3: Migrate embedding secrets**

Replace:

```yaml
api_key: "${SOPH_API_KEY}"
```

with:

```yaml
api_key_env: "SOPH_API_KEY"
```

Add resolved-config tests proving the written OpenClaw config contains the
literal `${SOPH_API_KEY}` marker and never a materialized secret.
Record the `api_key` → `api_key_env` raw pointer replacement as a
`security-fix`; the effective-normalization rule from Task 1 must keep its
adapter-consumed digest stable.

Broaden the existing bridge whitelist construction without changing the
payload key name: `agent_llm_env_vars` becomes the deterministic, deduplicated
union of explicit `agent_llm.env_vars`,
`agent_llm.api_key_env`, and `embedding.api_key_env`. Keep `ov_ingest` secrets
host-side; they are not referenced by the spawned OpenClaw process. Update the
bridge comments and add tests proving a host preset with no `agent_llm` still
passes `SOPH_API_KEY` for embedding, invalid names are rejected, and no secret
value is serialized into the payload.

**Step 4: Correct latency documentation drift**

Treat the current executable array as authoritative:

```bash
SYSTEMS=("evermemos" "openclaw" "openclaw-fts")
```

Update the script header and `docs/latency-alignment-runbook.md` so they no
longer claim the active run uses `openclaw-native-*`. Keep the native ids
available as experimental compatibility presets.

**Step 5: Run tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_hypercompositor_yaml.py \
  tests/evaluation/test_openclaw_bridge_payload.py \
  tests/evaluation/test_openclaw_resolved_config.py \
  tests/evaluation/test_system_config_policy.py -q
bash -n evaluation/scripts/run_latency_baseline.sh
```

Expected: PASS.

**Step 6: Commit**

```bash
git add evaluation/config/systems \
  evaluation/src/adapters/openclaw/adapter.py \
  evaluation/scripts/openclaw_eval_bridge.mjs \
  evaluation/scripts/run_latency_baseline.sh \
  docs/latency-alignment-runbook.md tests/evaluation
git commit -m "refactor(eval): deduplicate host OpenClaw presets"
```

### Task 9: Migrate OpenClaw Docker plugin presets

**Files:**
- Create: `evaluation/config/systems/_bases/openclaw-docker.yaml`
- Create or move under `canonical/`:
  - `openclaw-docker.yaml`
  - `openclaw-docker-evermemos.yaml`
  - `openclaw-docker-mem0.yaml`
- Create or move under `experiments/`:
  - `openclaw-docker-hypercompositor.yaml`
  - `openclaw-docker-memclaw.yaml`
  - `openclaw-docker-stub.yaml`
- Modify: `evaluation/config/systems/index.yaml`
- Delete: corresponding flat YAML files
- Modify: `env.template`
- Modify: `openclaw-eval/plugins/README-context-engine.md`
- Modify: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Modify: `tests/evaluation/test_system_config_policy.py`
- Modify: `tests/evaluation/test_plugin_cli_overrides.py`
- Create: `tests/evaluation/test_stub_memory_plugin_image_tag.py`

**Step 1: Add Docker-family compatibility tests**

Compare the six ids to the effective baseline. Assert canonical entries retain
their non-trivial image, network, timeout, concurrency, memory-mode, and
context-engine differences.

**Step 2: Extract the common Docker base**

Use `_bases/openclaw-docker.yaml` only for fields present with the same value in
all six inheritors. Do not place optional `embedding` or other plugin-specific
blocks in the common base: EverMemOS, Mem0, and stub presets intentionally lack
embedding, and deep merge has no delete operation. Overrides retain all
operational tuning called out by commit `cdde24c`.

Do not collapse plugin configs merely because CLI overrides could select the
same plugin; retain presets with additional tuning.

Update the active `env.template` comment and context-engine plugin authoring
guide so they reference categorized config paths and index registration rather
than new root-level YAML files.

**Step 3: Verify the structure-only migration in legacy policy mode**

Keep the stub image placeholder unchanged for this step. Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_plugin_cli_overrides.py -q
```

Expected: PASS in legacy policy mode with unchanged effective digests.

**Step 4: Commit the structure-only migration**

```bash
git add evaluation/config/systems env.template \
  openclaw-eval/plugins/README-context-engine.md tests/evaluation
git commit -m "refactor(eval): classify Docker plugin presets"
```

**Step 5: Replace the stub placeholder**

Compute the current bundled stub revision with the existing build helper and
use a concrete derived tag:

```text
openclaw-eval:7da23c3-stub-c1e9088-slim
```

Add a no-Docker test that recomputes the plugin content revision and asserts the
tag matches. Retain the index entry's experimental status and note in its
description that the image may need to be built locally; do not check image
existence in schema validation. Add the exact image pointer to
`system_config_approved_deltas.yaml` as a `behavior-fix`.

**Step 6: Run strict placeholder tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_plugin_cli_overrides.py \
  tests/evaluation/test_stub_memory_plugin_image_tag.py -q
```

Expected: PASS in strict image-policy mode. The stub raw/effective digest
change is limited to the approved image pointer.

**Step 7: Commit the stub image correction separately**

```bash
git add evaluation/config/systems/index.yaml \
  evaluation/config/systems/experiments/openclaw-docker-stub.yaml \
  tests/evaluation/fixtures/system_config_approved_deltas.yaml \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_stub_memory_plugin_image_tag.py
git commit -m "fix(eval): replace stub image placeholder"
```

### Task 10: Deduplicate session-bundle and OpenViking presets

**Files:**
- Create: `evaluation/config/systems/_bases/openclaw-memcore-session-bundle.yaml`
- Create: `evaluation/config/systems/_bases/openclaw-openviking.yaml`
- Create or move under `canonical/`:
  - `openclaw-docker-memcore-session-bundle.yaml`
  - `openclaw-docker-openviking-session-bundle-memcore.yaml`
  - `openclaw-docker-openviking-session-bundle-noop.yaml`
- Create or move under `ablations/`:
  - `openclaw-docker-memcore-session-bundle-emptytail.yaml`
  - `openclaw-docker-memcore-session-bundle-weaktail.yaml`
  - `openclaw-docker-openviking-session-bundle-noop-fixpack.yaml`
- Create or move under `tooling/`:
  - `openclaw-docker-openviking-session-bundle-noop-serial.yaml`
- Create: `evaluation/docs/system-configs/README.md`
- Create: `evaluation/docs/system-configs/openviking.md`
- Modify: `evaluation/config/systems/index.yaml`
- Modify: `evaluation/README.md`
- Modify: `evaluation/docs/openclaw_adapter.md`
- Modify: `docs/locomo-fair-baseline.md`
- Modify: `openclaw-eval/scripts/run_openviking_local_eval.sh`
- Modify: `evaluation/tools/qa_logs/cli.py`
- Modify: `tests/evaluation/test_run_openviking_local_eval.py`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Delete: corresponding flat YAML files

**Step 1: Add compatibility and category tests**

Assert:

- the fair-baseline three ids remain canonical;
- emptytail, weaktail, and fixpack are ablations;
- serial is tooling;
- all seven ids retain effective semantics;
- keys absent in a legacy variant remain absent after inheritance;
- the runner and QA-log defaults still resolve; and
- docs and scripts reference ids present in the index.

**Step 2: Extract session-bundle bases**

Make the Memcore variants override only `ingest_session_tail` and truly
variant-specific fields.

Make OpenViking canonical, fixpack, and serial configs inherit shared ingest,
tenant, model, and Docker settings. Lists retain replacement semantics; it is
acceptable for fixpack to repeat its short expanded environment whitelist
rather than introducing a custom list-merge language. As in Tasks 8–9, put a
field in a base only when every inheritor has it with the same value; keep
optional blocks in the relevant leaves because inheritance has no delete
operator.

**Step 3: Move long narratives into documentation**

In `evaluation/docs/system-configs/openviking.md`, preserve:

- direct SDK ingest versus plugin hooks;
- tenant and agent-id invariants;
- timeout and idle-streaming rationale;
- serial QA-log attribution semantics;
- fixpack environment knobs;
- cleanup retry behavior; and
- image rebuild/pinning notes.

Remove obsolete claims such as an outer 90-second timeout when the value is
240, old one-off QA incidents, and the 377-GB host estimate from YAML.

Keep only short field-adjacent invariants and safety warnings.

**Step 4: Update operational references**

Keep every existing public id in scripts and docs. Add links from the fair
baseline and OpenClaw operator docs to the new catalog. Do not change the
default OpenViking system id.

**Step 5: Run tests and link checks**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_run_openviking_local_eval.py \
  tests/tools/qa_logs -q
bash -n openclaw-eval/scripts/run_openviking_local_eval.sh
```

Expected: PASS.

**Step 6: Commit**

```bash
git add evaluation/config/systems evaluation/docs/system-configs \
  evaluation/README.md evaluation/docs/openclaw_adapter.md \
  docs/locomo-fair-baseline.md openclaw-eval/scripts \
  evaluation/tools/qa_logs tests
git commit -m "refactor(eval): organize session and OpenViking presets"
```

### Task 11: Enforce complete index and strict shipped-config validation

**Files:**
- Create: `tests/evaluation/test_system_config_catalog.py`
- Modify: `evaluation/src/config/system_index.py`
- Modify: `evaluation/src/config/system_policy.py`
- Modify: `evaluation/src/config/system_schema.py`
- Modify: `evaluation/config/systems/index.yaml`
- Modify: `evaluation/docs/system-configs/README.md`
- Modify: `tests/evaluation/test_system_config_index.py`
- Modify: `tests/evaluation/test_system_config_policy.py`
- Modify: `tests/evaluation/test_system_config_schema.py`

**Step 1: Write final tree-coverage tests**

Assert:

- the index exposes exactly the legacy 36 ids;
- every non-base YAML below `canonical/`, `experiments/`, `ablations/`, and
  `tooling/` is referenced exactly once;
- every `_bases/*.yaml` is reached by at least one inheritance chain;
- no superseded runnable YAML remains at the systems root;
- aliases have no physical YAML;
- no orphan or duplicate physical config exists; and
- docs contain a row for every id; and
- relative Markdown links in the new catalog/OpenViking docs and modified
  active operator docs resolve locally (ignore external URLs and anchors).

Expected before final cleanup: FAIL if any orphan or flat file remains.

**Step 2: Remove legacy validation mode**

Make shipped configs pass strict mode:

- no `openclaw.embedding.api_key`;
- no placeholder image;
- no `openclaw.prompts`;
- no unknown keys; and
- no plaintext credentials.

Keep the currently shipped `answer.max_retries` surface through this task.
Task 12 narrows it to the adapters that actually consume it after dedicated
ownership tests exist.

**Step 3: Add a catalog table**

Document each id with category, status, adapter, canonical id, physical config,
and intended use. Explicitly label serial as not suitable for benchmark
scoring.

**Step 4: Run tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_catalog.py \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_legacy_baseline.py -q
```

Expected: PASS for all 36 ids in strict mode.

**Step 5: Commit**

```bash
git add evaluation/config/systems evaluation/src/config \
  evaluation/docs/system-configs tests/evaluation
git commit -m "test(eval): enforce system configuration catalog"
```

### Task 12: Remove retry fields only where they are confirmed no-ops

**Files:**
- Modify: migrated `evermemos`, Hermes, and OpenClaw system YAMLs containing
  `answer.max_retries`
- Modify: `evaluation/src/config/system_schema.py`
- Modify: `evaluation/docs/system-configs/README.md`
- Modify: `evaluation/README.md`
- Create: `tests/evaluation/test_retry_policy_config_ownership.py`
- Modify: `tests/evaluation/test_system_config_schema.py`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Modify: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`

**Step 1: Write ownership tests before deleting anything**

Prove both retry layers:

- `OnlineAPIAdapter.answer()` consumes `answer.max_retries` as the retry count
  for one adapter invocation;
- `answer_stage` uses `benchmark_context.max_retries_for()` for outer
  harness-level retries;
- `mem0`, `memos`, `memu`, `zep`, and `evermemos_api` schemas retain a positive
  `answer.max_retries`; and
- non-online adapters reject that field because their answer methods never
  read it.

Use a minimal `OnlineAPIAdapter` test subclass with a mocked LLM provider that
fails predictably; do not call an API.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_retry_policy_config_ownership.py \
  tests/evaluation/test_system_config_schema.py -q
```

Expected: FAIL while non-online schemas/configs still accept the no-op field.

**Step 2: Remove only the fields with no code consumer**

Delete `answer.max_retries` from EverMemOS, Hermes, OpenClaw, and OpenClaw
Docker bases or overrides. Preserve it for every `OnlineAPIAdapter`-based
system. Do not remove top-level adapter retry fields such as
`mem0.max_retries`, `evermemos_api.max_retries`, or `memos.max_retries`.

Make `answer.max_retries` adapter-specific in the schema rather than globally
allowed. List the exact removed JSON pointers as `structure-only` approved
deltas. Document that an online answer can have inner adapter retries nested
inside the outer benchmark retry policy; changing that behavior is out of
scope for this cleanup.

**Step 3: Run focused stage tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_retry_policy_config_ownership.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_latency_invariants.py \
  tests/evaluation/test_pipeline_benchmark_outputs.py -q
```

Expected: PASS with unchanged effective digests and retained online-adapter
retry behavior.

**Step 4: Commit**

```bash
git add evaluation/config/systems evaluation/src/config/system_schema.py \
  evaluation/docs/system-configs evaluation/README.md tests/evaluation
git commit -m "refactor(eval): scope answer retry settings to consumers"
```

### Task 13: Fix miswired adapter settings in isolated commits

#### Task 13A: MemOS rate-limit field

**Files:**
- Modify: `evaluation/config/systems/canonical/memos.yaml`
- Modify: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Modify: `tests/evaluation/test_system_config_schema.py`
- Create: `tests/evaluation/test_memos_adapter.py`

**Step 1: Write a failing adapter test**

Construct `MemosAdapter` with `requests_per_second=4` and assert the limiter is
configured for four requests per second. Patch session creation; do not call
the API.

**Step 2: Rename the config field**

Replace:

```yaml
request_interval: 0.1
```

with:

```yaml
requests_per_second: 10
```

This matches the adapter's current default and intended ten-RPS behavior.
Remove `request_interval` from the MemOS schema. Record the exact
`request_interval` removal and `requests_per_second` addition as an approved
`behavior-fix`; the adapter test must prove that the effective rate remains ten
requests per second.

**Step 3: Run and commit**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_memos_adapter.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_schema.py -q
git add evaluation/config/systems/canonical/memos.yaml \
  evaluation/src/config/system_schema.py tests/evaluation
git commit -m "fix(eval): align MemOS rate-limit configuration"
```

#### Task 13B: MemU similarity threshold

**Files:**
- Modify: `evaluation/config/systems/canonical/memu.yaml`
- Modify: `evaluation/src/adapters/memu_adapter.py`
- Modify: `evaluation/src/config/system_schema.py`
- Modify: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Create: `tests/evaluation/test_memu_adapter.py`

**Step 1: Write failing search tests**

Construct with:

```python
{"search": {"min_similarity": 0.65}}
```

Mock the request and assert all MemU retrieval paths use `0.65` when no method
kwarg overrides it, while an explicit kwarg still wins.

**Step 2: Implement config-backed default**

Set:

```python
self.min_similarity = float(
    (config.get("search") or {}).get("min_similarity", 0.3)
)
```

Replace each `kwargs.get("min_similarity", 0.3)` with
`kwargs.get("min_similarity", self.min_similarity)`.

Move the YAML field under `search`.
Record the old and new exact JSON pointers as an approved `behavior-fix`.

**Step 3: Run and commit**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_memu_adapter.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_schema.py -q
git add evaluation/config/systems/canonical/memu.yaml \
  evaluation/src/adapters/memu_adapter.py \
  evaluation/src/config/system_schema.py tests/evaluation
git commit -m "fix(eval): honor MemU similarity configuration"
```

#### Task 13C: Search timeout ownership

**Files:**
- Modify: `evaluation/src/adapters/base.py`
- Modify: `evaluation/src/core/stages/search_stage.py`
- Modify: `evaluation/src/config/system_schema.py`
- Modify: `tests/evaluation/fixtures/system_config_approved_deltas.yaml`
- Modify: `tests/evaluation/system_config_legacy.py`
- Modify: `tests/evaluation/test_system_config_legacy_baseline.py`
- Create: `tests/evaluation/test_search_stage_timeout.py`

**Step 1: Write failing timeout tests**

Assert:

```python
adapter = FakeAdapter({"search": {"timeout_seconds": 42}})
assert adapter.get_search_timeout_seconds() == 42
```

Patch `asyncio.wait_for` in `search_stage` and assert it receives 42. Also
assert adapters without the field retain 300 seconds.

**Step 2: Implement the adapter hook**

Add to `BaseAdapter`:

```python
def get_search_timeout_seconds(self) -> float:
    return float((self.config.get("search") or {}).get("timeout_seconds", 300.0))
```

Use the hook in `search_stage` instead of the hard-coded value. Keep
`evermemos_cloud_api.search.timeout_seconds: 300`, so shipped behavior remains
unchanged while the field becomes functional. Stop excluding that pointer from
effective normalization and record its activation as an approved
`behavior-fix`; raw config remains unchanged.

**Step 3: Run and commit**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_search_stage_timeout.py \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_schema.py -q
git add evaluation/src/adapters/base.py evaluation/src/core/stages/search_stage.py \
  evaluation/src/config/system_schema.py tests/evaluation
git commit -m "fix(eval): honor per-system search timeouts"
```

### Task 14: Final documentation and compatibility verification

**Files:**
- Modify: `env.template`
- Modify: `evaluation/README.md`
- Modify: `evaluation/docs/README.md`
- Modify: `evaluation/docs/openclaw_adapter.md`
- Modify: `openclaw-eval/plugins/README-context-engine.md`
- Modify: `docs/usage/USAGE_EXAMPLES.md`
- Modify: `docs/evaluation/compatibility-baseline.md`
- Modify: `AGENTS.md`
- Modify: `docs/plans/2026-07-16-system-config-cleanup.md` only if commands or
  paths changed during implementation

**Step 1: Document the supported workflow**

Document:

- how to select a stable id;
- how aliases and deprecation warnings behave;
- how to add canonical, experiment, ablation, and tooling entries;
- how `extends` merges dictionaries and replaces lists;
- how to use secret markers;
- how to run the config validation tests; and
- why old ids and result-directory names remain stable.

Do not present `docs/superpowers/` as current policy.
Search active documentation outside `docs/superpowers/` for obsolete
root-level config paths. Update custom-config instructions to use a categorized
path plus an index entry; leave historical plans/specs unchanged.

**Step 2: Run focused system-config verification**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_cli.py \
  tests/evaluation/test_system_config_metadata.py \
  tests/evaluation/test_system_config_catalog.py -q
```

Expected: PASS; 36/36 ids resolve.

**Step 3: Run adjacent adapter and tool tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_hypercompositor_yaml.py \
  tests/evaluation/test_openclaw_bridge_payload.py \
  tests/evaluation/test_openclaw_resolved_config.py \
  tests/evaluation/test_plugin_cli_overrides.py \
  tests/evaluation/test_stub_memory_plugin_image_tag.py \
  tests/evaluation/test_retry_policy_config_ownership.py \
  tests/evaluation/test_memos_adapter.py \
  tests/evaluation/test_memu_adapter.py \
  tests/evaluation/test_search_stage_timeout.py \
  tests/evaluation/test_run_openviking_local_eval.py \
  tests/evaluation/test_artifact_hygiene.py \
  tests/tools/qa_logs -q
```

Expected: PASS.

**Step 4: Run the repository evaluation regression**

```bash
PYTHONPATH=src uv run pytest tests/evaluation tests/routine -q
```

Expected: PASS with only the already documented skip/warning surface.

Run full collection:

```bash
PYTHONPATH=src uv run pytest tests/ --collect-only
```

Expected: only the three accepted pre-existing collection debts recorded in
`docs/evaluation/compatibility-baseline.md`; no new collection error.

**Step 5: Run static and documentation checks**

```bash
bash -n evaluation/scripts/run_latency_baseline.sh
bash -n openclaw-eval/scripts/run_openviking_local_eval.sh
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_system_config_catalog.py -q
test -z "$(find evaluation/config/systems -maxdepth 1 -type f \
  -name '*.yaml' ! -name 'index.yaml' -print -quit)"
git diff --check
git status --short
```

The catalog test performs the executable local-Markdown-link checks described
in Task 11. The `find` assertion fails if any root-level runnable YAML remains.

**Step 6: Update the compatibility record**

Record:

- all 36 requested ids;
- alias mappings;
- focused and regression test results;
- accepted pre-existing full-suite debts;
- no Docker/API execution;
- no result/archive/worktree mutation; and
- the final commit range.

**Step 7: Commit**

```bash
git add AGENTS.md env.template docs/evaluation docs/usage \
  evaluation/README.md evaluation/docs \
  openclaw-eval/plugins/README-context-engine.md \
  docs/plans/2026-07-16-system-config-cleanup.md
git commit -m "docs(eval): document system configuration governance"
```

**Step 8: Verify the committed range**

```bash
git diff --check origin/main...HEAD
git status --short
```

Expected: no whitespace errors and a clean tracked worktree. Existing ignored
archives/caches are not removed or staged.

## Final handoff gate

Before claiming completion:

1. Use `verification-before-completion`.
2. Request an independent code review with `requesting-code-review`.
3. Confirm the worktree contains no ignored archive or source mutations caused
   by this task.
4. Do not push, merge, delete worktrees, or remove branches unless separately
   requested.
