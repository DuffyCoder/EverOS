# Repository Layout and Artifact Hygiene Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Clarify repository ownership, preserve all existing entrypoints, prevent compatibility mirrors from drifting, parameterize the OpenViking evaluation runner, and add safe inventory/archive tooling for local evaluation artifacts.

**Architecture:** Keep all current top-level paths canonical. Move OpenClaw runtime selection out of generic Python code into a declarative runtime registry, retain old commands through wrappers, and govern unavoidable duplicate files through an exact-mirror manifest. Local hygiene tooling is non-destructive by construction: it inventories and archives, but never deletes.

**Tech Stack:** Python 3.12, pytest, Bash, YAML, Git, Docker build-context checks, uv.

---

### Task 1: Repository policy, documentation boundaries, and ignore rules

**Files:**
- Modify: `.gitignore`
- Modify: `tests/evaluation/test_gitignore_dataset_coverage.py`
- Create: `docs/evaluation/README.md`
- Create: `docs/evaluation/analysis/README.md`
- Create: `docs/evaluation/analysis/TEMPLATE.md`
- Create: `evaluation/docs/README.md`
- Create: `docs/superpowers/README.md`
- Modify: `docs/README.md`
- Modify: `AGENTS.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/dev_docs/development_standards.md`

**Step 1: Extend the ignore-policy test before changing rules**

Add these cases to the existing batched `git check-ignore` test:

```python
(".env.bak.1778308789", True)
(".claire/worktrees/example/output.py", True)
(".runlogs/eval.log", True)
("docs/evaluation/analysis/local-report.md", True)
("docs/evaluation/analysis/README.md", False)
("docs/evaluation/analysis/TEMPLATE.md", False)
(".vscode/launch.json", False)
(".vscode/settings.json", False)
```

Run:

```bash
PYTHONPATH=src uv run pytest tests/evaluation/test_gitignore_dataset_coverage.py -q
```

Expected: FAIL because timestamped env backups, Claire artifacts, local analysis
reports, and run logs are not all covered, while VS Code is still broadly
ignored.

**Step 2: Rewrite `.gitignore` into labelled, non-duplicated sections**

Preserve existing ignore semantics for Python, environments, logs, evaluation
datasets/results/archives, demos, and operator files. Add precise rules for
`.env.bak*`, `.claire/`, `.runlogs/`, and local analysis reports. Keep
`docs/evaluation/analysis/README.md` and `TEMPLATE.md` trackable. Replace the
broad `.vscode/` rule with explicit allowlisting for the two tracked files.
Keep the `.claude/` deny-by-default plus tracked routine allowlist.

**Step 3: Add boundary and lifecycle documentation**

- `docs/evaluation/README.md`: durable framework/runtime/data/doc boundaries.
- `docs/evaluation/analysis/README.md`: local-only analysis policy and link to
  archives.
- `TEMPLATE.md`: required run name, dataset/system/config, commit, image digest,
  archive path, checksum, findings, and limitations fields.
- `evaluation/docs/README.md`: operator-doc scope.
- `docs/superpowers/README.md`: historical-plan status, not current execution
  instructions unless explicitly referenced.

Update the main docs index and AGENTS tree. Correct statements that call root
`config.json` the current main configuration; document it as unchanged legacy
data and direct users to `.env`, `env.template`, and typed runtime config.

**Step 4: Run tests and documentation path checks**

```bash
PYTHONPATH=src uv run pytest tests/evaluation/test_gitignore_dataset_coverage.py -q
git check-ignore -v .env.bak.1 .claire/x .runlogs/x docs/evaluation/analysis/local.md
git check-ignore -q docs/evaluation/analysis/README.md && exit 1 || true
```

Expected: pytest PASS; local artifacts ignored; policy files not ignored.

**Step 5: Commit**

```bash
git add .gitignore AGENTS.md docs evaluation/docs/README.md tests/evaluation/test_gitignore_dataset_coverage.py
git commit -m "docs(repo): define repository ownership and hygiene policy"
```

### Task 2: Declarative evaluation runtime boundary

**Files:**
- Create: `evaluation/config/runtime_registry.yaml`
- Create: `evaluation/src/plugins/runtime_registry.py`
- Modify: `evaluation/src/plugins/cli_overrides.py`
- Modify: `tests/evaluation/test_plugin_cli_overrides.py`
- Create: `tests/evaluation/test_runtime_registry.py`
- Modify: `evaluation/README.md`
- Modify: `evaluation/docs/openclaw_adapter.md`

**Step 1: Write failing runtime-registry tests**

Specify a registry API with these behaviours:

```python
registry = load_runtime_registry(path)
entry = get_runtime(registry, "openclaw-docker")
cmd = render_command(entry.build_command, repo_root=repo, python=Path("/py"))
assert cmd == ["/py", str(repo / "custom" / "build.py")]
```

Cover malformed top-level data, missing/empty commands, unknown placeholders,
unknown runtime ids, and prevention of relative-path escape outside repo root.

Extend the build-missing test so a temporary runtime registry points at a
custom builder and assert `subprocess.run` receives that path. Do not inspect
the source text for a hard-coded string; test observable command resolution.

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_runtime_registry.py \
  tests/evaluation/test_plugin_cli_overrides.py -q
```

Expected: FAIL because the registry module and injectable registry path do not
exist.

**Step 2: Implement the minimal registry**

Use a frozen dataclass and `yaml.safe_load`. The committed YAML declares:

```yaml
openclaw-docker:
  build_command:
    - "{python}"
    - "{repo_root}/openclaw-eval/harness/build.py"
```

Only `{python}` and `{repo_root}` are valid placeholders. Resolve path-bearing
arguments against the repository root and reject `..` escape. Add an optional
`runtime_registry_path` parameter to `apply_plugin_overrides`; its default is
the committed registry. `_invoke_build` loads `openclaw-docker` and appends the
existing plugin and manifest arguments. Preserve output text, exit behaviour,
and all old call sites.

**Step 3: Run focused and adjacent tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_runtime_registry.py \
  tests/evaluation/test_plugin_cli_overrides.py \
  tests/evaluation/test_build_new_cli.py -q
```

Expected: PASS.

**Step 4: Document the dependency direction and commit**

Explain that the framework consumes a declarative runtime command while the
OpenClaw runtime imports framework plugin metadata. Then commit:

```bash
git add evaluation tests/evaluation/test_runtime_registry.py tests/evaluation/test_plugin_cli_overrides.py
git commit -m "refactor(eval): declare runtime build integration"
```

### Task 3: Govern compatibility mirrors

**Files:**
- Create: `evaluation/config/repository_mirrors.yaml`
- Create: `tests/evaluation/test_repository_mirrors.py`
- Modify: `data/README.md`
- Modify: `evaluation/README.md`
- Modify: `evaluation/scripts/openclaw_eval_bridge_lib.mjs`
- Modify: `openclaw-eval/container/openclaw_eval_bridge_lib.mjs`
- Modify: `openclaw-eval/plugins/evermemos/src/prompt-builders.ts`
- Modify: `openclaw-eval/plugins/mem0/src/prompt-builders.ts`

**Step 1: Write a failing manifest-driven mirror test**

The manifest schema is:

```yaml
- id: locomo-demo-compat
  canonical: evaluation/data/locomo/locomo10.json
  mirrors:
    - data/locomo10.json
  strategy: exact
  reason: legacy demo and adapter path compatibility
```

Add entries for the exact bridge helper mirror and the two independently staged
plugin prompt builders. The test must reject absolute paths, `..` escapes,
missing files, duplicate ids, canonical paths repeated as mirrors, unsupported
strategies, and byte inequality. It must assert each shipped entry has a
non-empty reason.

Run:

```bash
PYTHONPATH=src uv run pytest tests/evaluation/test_repository_mirrors.py -q
```

Expected: FAIL because the manifest does not exist.

**Step 2: Add the mirror manifest and ownership headers**

Declare the three known exact mirror groups. Add short headers/comments to text
mirrors naming the canonical path and the verification test. Do not alter
runtime code or JSON bytes. Document that similarly named system YAML files are
independent configurations and may diverge; they are not mirrors.

**Step 3: Run mirror and bridge tests**

```bash
PYTHONPATH=src uv run pytest \
  tests/evaluation/test_repository_mirrors.py \
  tests/evaluation/test_openclaw_bridge_lib.py \
  tests/evaluation/test_openclaw_bridge_engine_import.py -q
```

Expected: PASS.

**Step 4: Commit**

```bash
git add evaluation/config/repository_mirrors.yaml evaluation/README.md \
  evaluation/scripts/openclaw_eval_bridge_lib.mjs data/README.md \
  openclaw-eval/container/openclaw_eval_bridge_lib.mjs \
  openclaw-eval/plugins/evermemos/src/prompt-builders.ts \
  openclaw-eval/plugins/mem0/src/prompt-builders.ts \
  tests/evaluation/test_repository_mirrors.py
git commit -m "test(repo): lock compatibility mirrors"
```

### Task 4: Parameterize the OpenViking local evaluation runner

**Files:**
- Modify: `build.sh`
- Create: `openclaw-eval/scripts/run_openviking_local_eval.sh`
- Create: `tests/evaluation/test_run_openviking_local_eval.py`
- Modify: `openclaw-eval/README.md` if present, otherwise `evaluation/docs/openclaw_adapter.md`

**Step 1: Write failing subprocess tests**

Tests must establish:

- `build.sh --help` delegates and exits zero;
- `--dry-run RUN_NAME` prints resolved repository, fork, plugin, config, log,
  Python, and system values without executing `kill`, `rm`, `npm`, `curl`, or
  the evaluation CLI;
- environment variables override every external path;
- a non-interactive real run without `--yes-reset` exits before destructive
  work and explains the required flag;
- the old `build.sh RUN_NAME <evaluation args>` positional grammar is accepted.

Run:

```bash
PYTHONPATH=src uv run pytest tests/evaluation/test_run_openviking_local_eval.py -q
```

Expected: FAIL because the portable runner does not exist and the root script
still hard-codes personal paths.

**Step 2: Implement the root wrapper and portable runner**

The root script only resolves its own directory and `exec`s the new script.
The new script derives `REPO_ROOT` from its location and supports:

- `OPENVIKING_FORK`
- `OPENVIKING_PLUGIN_DIR`
- `OPENVIKING_CONFIG`
- `OPENVIKING_SERVER_BIN`
- `EVAL_PYTHON`
- `EVAL_SYSTEM`
- `EVAL_LOG_DIR`
- `TCMALLOC_PATH` (optional)

Add `--help`, `--dry-run`, and `--yes-reset`. In an interactive terminal, an
exact `yes` response may replace `--yes-reset`; non-interactive execution must
require the flag. Validate required paths before stopping a process. Preserve
the current direct-proxy cleanup, health wait, plugin mount, and trailing
evaluation arguments.

**Step 3: Run tests and shell syntax checks**

```bash
bash -n build.sh openclaw-eval/scripts/run_openviking_local_eval.sh
PYTHONPATH=src uv run pytest tests/evaluation/test_run_openviking_local_eval.py -q
```

Expected: PASS.

**Step 4: Commit**

```bash
git add build.sh openclaw-eval/scripts/run_openviking_local_eval.sh \
  tests/evaluation/test_run_openviking_local_eval.py \
  evaluation/docs/openclaw_adapter.md
git commit -m "refactor(eval): make local OpenViking runner portable"
```

### Task 5: Add non-destructive artifact inventory and compact archive tooling

**Files:**
- Create: `evaluation/tools/artifact_hygiene/__init__.py`
- Create: `evaluation/tools/artifact_hygiene/__main__.py`
- Create: `evaluation/tools/artifact_hygiene/inventory.py`
- Create: `evaluation/tools/artifact_hygiene/archive.py`
- Create: `tests/evaluation/test_artifact_hygiene.py`
- Create: `evaluation/docs/artifact-lifecycle.md`
- Modify: `evaluation/docs/README.md`

**Step 1: Write failing classification tests**

Specify a pure `classify_result(name, *, referenced, pinned, successful,
is_latest_success)` function. Required precedence:

1. pinned -> `keep`;
2. referenced -> `keep`;
3. successful latest full run -> `keep`;
4. names containing `smoke`, `debug`, `retry`, `calib`, `test`, or
   `incomplete` -> `delete_candidate`;
5. everything else -> `review`.

Each result includes machine-readable reasons. Add filesystem tests for size
collection and JSON inventory output. Environment backup entries may include
path, size, mode, and mtime but never file content.

**Step 2: Write failing compact-archive tests**

Given a synthetic result tree, `plan_archive` must include top-level metrics,
resolved configs, reports, logs, answer/search/evaluation JSON, and lightweight
artifact evidence. It must exclude paths containing `openclaw-workspaces`,
`node_modules`, `.cache`, `.venv`, `__pycache__`, compiled Python files, vector
indexes, BM25 indexes, and model caches. Every exclusion has a reason.

`create_archive` writes to a new directory only when `execute=True`, copies no
excluded file, and writes `manifest.json` with source, Git SHA, included file
sizes and SHA-256 values, excluded paths/reasons, and archive timestamp. It
must fail if the destination exists. It never deletes or mutates the source.

Run:

```bash
PYTHONPATH=src uv run pytest tests/evaluation/test_artifact_hygiene.py -q
```

Expected: FAIL because the package does not exist.

**Step 3: Implement the minimal package and CLI**

Expose:

```bash
python -m evaluation.tools.artifact_hygiene inventory \
  --repo-root PATH --output PATH
python -m evaluation.tools.artifact_hygiene archive \
  --result PATH --archive-root PATH --execute
```

Without `--execute`, archive prints the plan as JSON and writes nothing. There
is deliberately no delete subcommand. The inventory reads an optional local
`evaluation/archives/KEEP` file and reports registered Git worktrees, ignored
environment backups, results, archives, and analysis files without reading
secrets.

**Step 4: Document lifecycle and run tests**

Document keep/archive/delete criteria, compact contents, recovery inspection,
and the rule that deletion requires a separately reviewed manifest.

```bash
PYTHONPATH=src uv run pytest tests/evaluation/test_artifact_hygiene.py -q
python -m evaluation.tools.artifact_hygiene --help
```

Expected: PASS and help exits zero.

**Step 5: Commit**

```bash
git add evaluation/tools/artifact_hygiene evaluation/docs \
  tests/evaluation/test_artifact_hygiene.py
git commit -m "feat(eval): add safe artifact inventory and archive tools"
```

### Task 6: Compatibility audit and full branch verification

**Files:**
- Create: `docs/evaluation/compatibility-baseline.md`
- Modify as findings require: documentation and compatibility wrappers only

**Step 1: Record the compatibility surface**

List and verify at minimum:

- `python -m evaluation.cli --help`
- `python openclaw-eval/harness/build.py --help`
- `./build.sh --help`
- project scripts `bootstrap`, `web`, and `manage`
- `data/locomo10.json` and `evaluation/data/locomo/locomo10.json`
- Docker contexts and copied container assets
- root `config.json`, tracked VS Code paths, docs indexes, and historical plan
  paths.

Record the three accepted pristine full-suite collection errors separately.

**Step 2: Run focused verification**

```bash
PYTHONPATH=src uv run pytest tests/evaluation tests/routine -q
bash -n build.sh openclaw-eval/scripts/*.sh
PYTHONPATH=src uv run python -m evaluation.cli --help >/dev/null
PYTHONPATH=src uv run python openclaw-eval/harness/build.py --help >/dev/null
./build.sh --help >/dev/null
git diff --check origin/main...HEAD
git status --short
```

Expected baseline for pytest: 576 passed, 1 skipped, plus any new tests added by
this plan passing. No complete paid evaluation is run.

**Step 3: Re-run the complete suite to confirm only baseline errors remain**

```bash
PYTHONPATH=src uv run pytest tests/
```

Expected: collection stops only on the three recorded origin/main errors:
stale `get_text_embedding`, missing keyword-vocabulary repository module, and
missing `psutil`. Any new error is a regression and must be fixed.

**Step 4: Commit verification documentation**

```bash
git add docs/evaluation/compatibility-baseline.md
git commit -m "docs(repo): record compatibility verification surface"
```

### Task 7: Generate the real local cleanup manifest without deleting data

**Files:**
- Create locally but keep ignored: `evaluation/archives/repository-cleanup-2026-07-15.json`
- Do not modify tracked files unless a tool defect is found through TDD

**Step 1: Inventory the original worktree**

Run the new tool against `/Data3/shutong.shan/memory/refs/EverMemOS`, not the
implementation worktree. Include all result directories, archives, three linked
worktrees, `.claire`, local analyses, and environment backup metadata.

**Step 2: Curate policy-driven candidates**

Mark referenced runs, latest successful full runs per configuration group, and
local KEEP entries. Produce three explicit lists: retain in place, compact and
archive, and delete after archive/approval. Record estimated bytes reclaimed.

**Step 3: Verify the manifest is non-destructive**

Compare Git status and filesystem counts before and after inventory. They must
be unchanged except for the ignored manifest itself.

**Step 4: Stop for user review**

Do not move environment backups, create large archives, remove worktrees, or
delete any result until the user approves this concrete manifest.

