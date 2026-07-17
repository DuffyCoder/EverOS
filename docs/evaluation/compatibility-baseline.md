# Repository-layout compatibility baseline

## Historical repository-layout snapshot (2026-07-15/16)

> This section preserves the original snapshot as historical evidence. Its
> commit, counts, and “unchanged” statements describe only the dated
> `1641ab2` audit and are not claims about the current branch tip.

This document records the compatibility surface verified by the
`refactor/repository-layout` branch on 2026-07-15 and 2026-07-16. The
implementation snapshot under test was `1641ab2` and its upstream base was
`origin/main@f99797b411d2089645a001ecb96c88fc797d346e`.

The audit is intentionally non-destructive. It did not run a paid evaluation,
start a service, create a runtime image, or change local evaluation artifacts.
Known upstream failures are recorded rather than repaired as part of repository
layout work.

### Public commands and scripts

| Surface | Verification | Result |
| --- | --- | --- |
| Generic evaluation CLI | `PYTHONPATH=src uv run python -m evaluation.cli --help` | Requires local environment setup before argparse; without `.env` it exits 1. This is unchanged from `origin/main`. |
| OpenClaw image builder | `PYTHONPATH=src uv run python openclaw-eval/harness/build.py --help` | Exit 0; existing build/plugin/image options remain available. |
| Root OpenViking wrapper | `./build.sh --help` | Exit 0; accepts `[--dry-run] [--yes-reset] RUN_NAME [evaluation arguments...]`. |
| Bootstrap entry point | `uv run bootstrap --help` | Exit 0 and prints usage. It also emits an existing unhandled asynchronous `SystemExit` traceback. |
| Web entry point | `uv run web --help` | Exit 0; no service was started. |
| Management entry point | `uv run manage --help` | Exit 0; exposes `shell`, `list-commands`, and `tenant-init`. |

The project-script mappings remain:

```toml
bootstrap = "src.bootstrap:main"
web = "src.run:main"
manage = "src.manage:cli"
```

The corresponding files are `src/bootstrap.py`, `src/run.py`, and
`src/manage.py`. Those files, `pyproject.toml`, `evaluation/cli.py`, and
`openclaw-eval/harness/build.py` are unchanged from `origin/main`.

When environment initialization is isolated, the evaluation argparse surface
still includes dataset, system, stage, smoke, conversation-range, output,
retry, plugin, context-engine, image, and build controls. The build harness
still includes memory-plugin, context-engine, OpenClaw checkout, variant,
image-manifest, dry-run, pruning, eval-base, push, and registry controls.

### Authoritative data and compatibility mirrors

`evaluation/data/` remains authoritative for benchmark inputs.
`data/locomo10.json` remains a compatibility mirror, not a second source of
truth. All groups declared in `evaluation/config/repository_mirrors.yaml` were
verified byte for byte:

| Mirror group | Bytes per file | SHA-256 |
| --- | ---: | --- |
| `evaluation/data/locomo/locomo10.json` and `data/locomo10.json` | 2,805,274 | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| OpenClaw evaluation bridge library pair | 5,952 | `5a7b752f41224c3f0567cff4a6c313e7c6994c4343fe0772c787948119c113c6` |
| EverMemOS and mem0 prompt-builder pair | 5,366 | `8c0c9294ba80262b336ef34f29fce1c62b545e0cfc2cf7dbdcbd6a170fb14db5` |

The mirror-specific suite passed with 14 tests.

### Docker and Compose paths

The audit checked existing build contexts and every repository-owned asset
copied by the evaluation Dockerfiles:

- the root `Dockerfile` uses the repository root and copies the application;
- `Dockerfile.eval`, `Dockerfile.eval-base`, and `Dockerfile.eval-plugin` use
  `openclaw-eval/` as their context;
- `Dockerfile.ov-patch` uses `openclaw-eval/`;
- `openclaw-eval/Dockerfile.ov-plugin-patch` uses
  `openclaw-eval/plugins/openviking/`; and
- the OpenClaw base build continues to use an external OpenClaw checkout.

The tracked container assets referenced by normal evaluation builds exist:

| Asset | Bytes |
| --- | ---: |
| `openclaw-eval/container/openclaw.template.json` | 1,372 |
| `openclaw-eval/container/entrypoint.sh` | 7,586 |
| `openclaw-eval/container/openclaw_eval_bridge.mjs` | 28,373 |
| `openclaw-eval/container/openclaw_eval_bridge_lib.mjs` | 5,952 |

Bundled plugin directories for EverMemOS, mem0, stub memory, stub context
engine, and OpenViking all contain tracked files.

Some builds retain explicit local prerequisites:

- `openclaw-eval/_active_sidecar/` is an ignored staging directory generated
  by `build.py` before a build;
- the ignored `openclaw-openviking-2026.6.04.tgz` must be produced with the
  documented `npm pack` workflow before either OpenViking patch image is built;
- the root Compose file binds `docker/mongodb/init`, which is not shipped in
  `origin/main`; and
- the OpenViking Compose override is valid only when merged with the upstream
  OpenViking Compose file.

`docker compose -f docker-compose.yaml config --quiet` passed, apart from the
existing warning that the top-level `version` field is obsolete. The
OpenViking override also passed when merged with the available OpenViking
Compose file.

### Retained repository paths

The following compatibility paths were checked and intentionally retained:

- root `config.json` is tracked, unchanged from `origin/main`, and documented
  as a deprecated compatibility file rather than an authority;
- `.vscode/launch.json` and `.vscode/settings.json` remain tracked and
  unchanged, while other `.vscode/` files remain ignored;
- `docs/README.md`, `docs/evaluation/README.md`, `evaluation/README.md`,
  `evaluation/docs/README.md`, and `docs/superpowers/README.md` exist and are
  tracked;
- 52 checked local Markdown links resolve to existing targets; and
- historical material under `docs/superpowers/` and `docs/plans/` remains at
  its old paths for old commits, reviews, and references. It is historical
  context, not the current repository contract.

The names EverOS, EverMemOS, and `memsys` continue to coexist on their existing
compatibility surfaces. This branch does not attempt a naming migration.

### Verification results

```text
PYTHONPATH=src uv run pytest tests/evaluation tests/routine -q
694 passed, 1 skipped, 1 warning in 16.87s
```

The warning is the existing unregistered `pytest.mark.integration` marker.
A fresh local clone of this branch, with no `.env` or ignored archive tree,
passed the same focused suite with 694 passed and 1 skipped in 17.54 seconds;
its Git status remained clean. The clone used the already resolved development
environment only for Python dependencies.

Shell syntax passed:

```text
bash -n build.sh openclaw-eval/scripts/*.sh
exit 0
```

The complete suite exited 2 after collecting 996 items and stopped after 3.03
seconds on exactly three accepted `origin/main` errors (with 41 warnings):

1. `tests/test_embedding_reranker_providers.py` imports the stale
   `get_text_embedding` symbol;
2. `tests/test_keyword_vocabulary_milvus_repository.py` imports a missing
   keyword-vocabulary repository module; and
3. `tests/test_stability_integration.py` imports undeclared `psutil`.

The affected tests, source paths, `pyproject.toml`, and `uv.lock` are unchanged
from `origin/main`. These failures are baseline debt, not regressions introduced
by repository cleanup.

The branch diff passed `git diff --check origin/main...HEAD`. A clean-clone
verification must additionally supply the documented local environment and
Docker prerequisites; it must not fabricate `.env`, ignored package archives,
or external checkouts as tracked repository content.

### Reproduction commands

```bash
PYTHONPATH=src uv run pytest tests/evaluation tests/routine -q
PYTHONPATH=src uv run pytest tests/
PYTHONPATH=src uv run pytest tests/evaluation/test_repository_mirrors.py -q
bash -n build.sh openclaw-eval/scripts/*.sh
PYTHONPATH=src uv run python openclaw-eval/harness/build.py --help
./build.sh --help
uv run bootstrap --help
uv run web --help
uv run manage --help
git diff --check origin/main...HEAD
git status --short --branch
```

No command in this baseline authorizes artifact deletion, worktree removal,
environment-backup movement, image publication, or a paid evaluation run.

## Final system-configuration cleanup snapshot (2026-07-17)

This record describes the completed evaluation system-configuration cleanup on
`refactor/repository-layout`, verified on 2026-07-17. The upstream boundary is
`origin/main@f99797b411d2089645a001ecb96c88fc797d346e`; the committed range is
`origin/main@f99797b411d2089645a001ecb96c88fc797d346e..HEAD`. `HEAD` intentionally
includes this record. The implementation head immediately before the final
documentation work was `109b096`.

The verification was non-destructive. It did not start Docker, build or push
an image, call a memory-system API, run a paid evaluation, or run a service. It
did not modify `evaluation/results/`, `evaluation/archives/`, ignored analysis
or experiment artifacts, or any external checkout. It did not push, merge,
remove the worktree, or delete the branch.

### Public system-ID contract

`evaluation/config/systems/index.yaml` exposes exactly 36 requested IDs:

```text
evermemos
evermemos_cloud_api
evermemos_local_api
mem0
memos
memu
zep
hermes-holographic
hermes-honcho
hermes-hindsight
openclaw
openclaw-fts
openclaw-fts-noflush
openclaw-hybrid-noflush
openclaw-vector
openclaw-vector-noflush
openclaw-docker
openclaw-docker-evermemos
openclaw-docker-mem0
openclaw-docker-memcore-session-bundle
openclaw-docker-openviking-session-bundle-memcore
openclaw-docker-openviking-session-bundle-noop
hermes
openclaw-hybrid
openclaw-agent-local
openclaw-hypercompositor
openclaw-native-embed
openclaw-native-noembed
openclaw-noop
openclaw-docker-hypercompositor
openclaw-docker-memclaw
openclaw-docker-stub
openclaw-docker-memcore-session-bundle-emptytail
openclaw-docker-memcore-session-bundle-weaktail
openclaw-docker-openviking-session-bundle-noop-fixpack
openclaw-docker-openviking-session-bundle-noop-serial
```

The category split is 22 canonical, 2 alias, 8 experiment, 3 ablation, and 1
tooling entry. The two aliases are:

- `hermes` → `hermes-holographic`
- `openclaw-hybrid` → `openclaw`

Both aliases remain selectable. The CLI shows requested-to-canonical
resolution, metadata records both values, and the default result directory
continues to use the requested ID. Deprecated entries, if introduced, emit a
replacement warning; the 12 current experiment/ablation/tooling entries emit
an experimental warning.

The physical tree contains 34 categorized runnable leaves and 5 reachable
inheritance bases. Every leaf is registered exactly once, every base is used,
and the only root-level YAML under `evaluation/config/systems/` is
`index.yaml`. Strict resolution succeeded for all 36 IDs. The immutable
pre-cleanup fixture remains unchanged; approved deltas enumerate intentional
raw-structure or behavior corrections to legacy IDs.

### Authoritative data and compatibility mirrors

`evaluation/data/` is authoritative for benchmark input.
`data/locomo10.json` remains an exact compatibility mirror of
`evaluation/data/locomo/locomo10.json`, not a second source of truth. All
groups in `evaluation/config/repository_mirrors.yaml` passed byte-for-byte
verification:

| Mirror group | Bytes per file | SHA-256 |
|---|---:|---|
| LoCoMo authoritative data and legacy path | 2,805,274 | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| OpenClaw evaluation bridge library pair | 5,952 | `5a7b752f41224c3f0567cff4a6c313e7c6994c4343fe0772c787948119c113c6` |
| EverMemOS and mem0 prompt-builder pair | 5,366 | `8c0c9294ba80262b336ef34f29fce1c62b545e0cfc2cf7dbdcbd6a170fb14db5` |

The mirror suite passed with 14 tests.

### Verification results

All pytest commands disabled bytecode and the pytest cache provider so final
verification did not create source-adjacent cache artifacts.

| Gate | Result |
|---|---|
| Focused system-config contract (8 files) | 730 passed in 9.86s |
| Adjacent adapters/tools, including Tasks 13A/13B/13C | 213 passed in 3.51s |
| `tests/evaluation tests/routine` regression | 1,482 passed, 1 skipped, 1 warning in 28.12s |
| Full `tests/` collect-only | Exit 2; 1,785 tests collected, 3 errors, 41 warnings in 3.55s |
| Repository mirrors | 14 passed in 0.08s |

The regression warning is the existing unregistered
`pytest.mark.integration` marker in
`tests/evaluation/test_hermes_integration_smoke.py`. The full collect-only run
stopped on exactly these three accepted pre-existing debts:

1. `tests/test_embedding_reranker_providers.py` imports the removed
   `get_text_embedding` symbol.
2. `tests/test_keyword_vocabulary_milvus_repository.py` imports the absent
   `keyword_vocabulary_milvus_repository` module.
3. `tests/test_stability_integration.py` imports undeclared `psutil`.

No attempt was made to repair those unrelated collection debts. No fourth
collection error appeared.

Static checks also passed:

- `bash -n` for `evaluation/scripts/run_latency_baseline.sh` and
  `openclaw-eval/scripts/run_openviking_local_eval.sh`;
- catalog local-link validation;
- root-level runnable-YAML rejection;
- 36/36 strict system resolution;
- `git diff --check` for both the Task 14 diff and the full branch range; and
- a clean tracked worktree after the documentation commit.

The protected ignored-artifact metadata fingerprint was unchanged before and
after Task 14:

```text
bea40651a95963623ed46a65e7db58593b0a35d78734e990081bda49b3d0c133  -
```

Recompute it from the repository root with the exact metadata pipeline used by
this audit:

```bash
git ls-files --others -i --exclude-standard -z -- \
  evaluation/archives docs/evaluation/analysis evaluation/results \
  evaluation/experiments openclaw-eval/_active_sidecar \
  evaluation/read_ov_search.py \
  evaluation/memory_evaluation/extract_room_data.py \
  evaluation/memory_evaluation/test.py \
| LC_ALL=C sort -z \
| xargs -0 -r stat --printf='%n\0%s\0%y\0%z\0%f\0' \
| sha256sum
```

The fingerprint covers ignored archives, analysis, results, experiments,
OpenClaw sidecar staging, and these three protected ignored helper paths:

- `evaluation/read_ov_search.py`
- `evaluation/memory_evaluation/extract_room_data.py`
- `evaluation/memory_evaluation/test.py`

Existing ignored caches and archives were left in place.

### Reproduction commands

```bash
# Focused registry contract
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run pytest \
  -p no:cacheprovider \
  tests/evaluation/test_system_config_legacy_baseline.py \
  tests/evaluation/test_system_config_index.py \
  tests/evaluation/test_system_config_loader.py \
  tests/evaluation/test_system_config_schema.py \
  tests/evaluation/test_system_config_policy.py \
  tests/evaluation/test_system_config_cli.py \
  tests/evaluation/test_system_config_metadata.py \
  tests/evaluation/test_system_config_catalog.py -q

# Adjacent adapter/tool coverage
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run pytest \
  -p no:cacheprovider \
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

# Evaluation regression and accepted full-collection debt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run pytest \
  -p no:cacheprovider tests/evaluation tests/routine -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run pytest \
  -p no:cacheprovider tests/ --collect-only -q

# Static and catalog checks
bash -n evaluation/scripts/run_latency_baseline.sh
bash -n openclaw-eval/scripts/run_openviking_local_eval.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run pytest \
  -p no:cacheprovider tests/evaluation/test_system_config_catalog.py -q
test -z "$(find evaluation/config/systems -maxdepth 1 -type f \
  -name '*.yaml' ! -name 'index.yaml' -print -quit)"
git diff --check origin/main...HEAD
git status --short --branch
```

These commands authorize verification only. They do not authorize artifact
deletion, worktree or branch removal, image publication, API use, or a paid
evaluation run.
