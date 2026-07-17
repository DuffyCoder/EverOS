# Repository Layout and Artifact Hygiene Design

## Context

The repository combines the EverMemOS application, a generic evaluation
framework, an OpenClaw-specific runtime, demos, publishable examples, historical
engineering records, and large local evaluation artifacts. The tracked tree is
roughly 15 MiB, while ignored evaluation results and linked worktrees account
for tens of GiB. The cleanup therefore has two distinct responsibilities:

1. make tracked ownership and dependency boundaries explicit without breaking
   existing paths; and
2. give local artifacts a safe retention, archive, and deletion workflow.

The work is based on the latest `origin/main` of the DuffyCoder fork. Migrating
to the unrelated upstream EverOS history is explicitly out of scope.

## Goals

- Preserve every existing public path, import, command, and Docker build
  context.
- Leave the `src/` package structure and current project naming unchanged.
- Make `evaluation/` the generic framework and `openclaw-eval/` the
  OpenClaw-specific runtime, with a one-way runtime-to-framework dependency.
- Establish authoritative sources for compatibility mirrors and prevent drift.
- Separate durable documentation, component operator docs, local analyses,
  active results, and archived evidence.
- Replace machine-bound operational scripts with parameterized implementations
  while retaining their root compatibility entrypoints.
- Inventory and compact local results before any destructive cleanup.

## Non-goals

- Renaming EverOS, EverMemOS, or the `memsys` distribution.
- Repackaging or moving the application modules under `src/`.
- Reorganizing the existing test tree or changing existing test commands.
- Running a complete paid LoCoMo evaluation as a structural-change gate.
- Pushing the branch, deleting remote branches, or deleting any local artifact
  without a reviewed manifest.

## Repository boundaries

The current top-level directories remain canonical; no directory symlinks or
parallel replacement trees are introduced.

- `evaluation/` owns generic protocols, datasets, configuration, metrics,
  adapters, plugin interfaces, active results, and local archives. Runtime
  commands are selected from declarative configuration rather than hard-coded
  filesystem traversal.
- `openclaw-eval/` owns the OpenClaw Docker context, image builder, runtime
  container assets, and OpenClaw plugins. It may import public evaluation
  interfaces; the generic framework must not know its internal layout.
- `evaluation/data/` is authoritative for benchmark datasets. `data/` owns demo
  samples. `data/locomo10.json` remains as an exact compatibility mirror.
- `demo/` remains the runnable Python demonstration package. `examples/`
  remains the home of independently installable or publishable integrations.
- `docs/evaluation/` owns durable evaluation architecture and policy.
  `evaluation/docs/` owns implementation-coupled operator instructions.
  `docs/superpowers/` remains at its current paths and is labelled historical.

## Compatibility and duplicate governance

Before implementation, existing imports, commands, data paths, Docker contexts,
and documentation entrypoints form the compatibility baseline. Old entrypoints
remain the public façade even when implementation moves behind them.

Exact duplicates that exist because of distribution or build-context boundaries
are recorded in a mirror manifest. Each mirror group has one canonical path,
one or more compatibility paths, a reason, and an exact-content check. Files
that merely happen to be equal but are allowed to evolve independently are not
declared mirrors.

The root `build.sh` remains callable with its existing positional run name. Its
implementation moves to `openclaw-eval/scripts/`, discovers the repository,
accepts environment overrides for external checkouts and credentials, and
requires explicit confirmation before stopping services or clearing data.

The root `config.json` remains byte-compatible and is documented as deprecated
legacy configuration. Tracked VS Code files remain tracked; ignore rules use
explicit exceptions instead of contradicting tracked state.

## Artifact lifecycle

`evaluation/results/` remains the active run directory.
`evaluation/archives/` remains a local, Git-ignored archive directory.

A completed run is retained when it is referenced by tracked documentation or
history, is the latest successful full run for a dataset/system/configuration
group, or is explicitly pinned in a local KEEP list. Smoke, debug, retry,
calibration, and incomplete runs default to deletion candidates.

Compact archives retain final metrics, resolved configuration, code and image
identity, essential logs, answer/search/evaluation evidence, and checksums. They
exclude reproducible indexes, caches, virtual environments, duplicated models,
and OpenClaw workspaces. Archive creation never deletes the source.

Actual analysis Markdown lives under the ignored portion of
`docs/evaluation/analysis/`; Git tracks only its README and template. Raw PDFs
and supporting data live in `evaluation/archives/`. Environment backups move to
a private directory outside the repository with mode `0600`.

The three existing linked worktrees are removed with Git only after their
needed logs are archived. Their branch refs remain until final verification.
`git clean -xfd` is forbidden for this cleanup.

## Verification and rollback

The relevant pristine baseline is 576 passing evaluation/routine tests and one
skip. The full pristine test collection has three accepted pre-existing errors:
two stale module/API references and one undeclared `psutil` dependency. These
are recorded rather than fixed in this branch.

Verification covers the existing relevant tests, old imports and CLI help,
script paths, Docker contexts, documentation links, mirror equality, ignore
coverage, and a clean checkout. No local source artifact is removed until the
tracked changes pass these gates and a keep/archive/delete manifest is reviewed.
Atomic commits provide tracked rollback; archive checksums provide local-data
rollback evidence.
