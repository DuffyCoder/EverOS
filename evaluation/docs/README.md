# Evaluation Framework Operations

This directory contains operational documentation coupled to code in
`evaluation/`: adapter behavior, run lifecycle, recovery procedures, and other
instructions that must change with the framework implementation.

The implemented framework boundary is declarative but intentionally not fully
runtime-agnostic. `evaluation/` owns benchmark protocols, datasets, metrics,
system/plugin/runtime registries, adapters, and public CLI entry points.
`openclaw-eval/` owns the OpenClaw-specific container, plugin, build, and
harness runtime. For `--build-missing`, evaluation reads the registered base
builder command from `evaluation/config/runtime_registry.yaml` and appends the
validated OpenClaw plugin/image arguments. The adapter, runtime ID, and those
flags remain OpenClaw-specific; new generic framework code must not hard-code
additional internal `openclaw-eval/` paths.

Current component notes:

- [`artifact-lifecycle.md`](artifact-lifecycle.md) documents conservative
  inventory decisions, explicit KEEP pins, compact archives, and restore and
  deletion-safety gates.
- [`openclaw_adapter.md`](openclaw_adapter.md) documents the framework adapter's
  fidelity and comparison semantics.
- [`system-configs/README.md`](system-configs/README.md) is the active registry,
  category, inheritance, secret, and 36-ID governance guide.
- [`system-configs/openviking.md`](system-configs/openviking.md) records the
  active OpenViking session-bundle and operational invariants.
- [`round-finish.md`](round-finish.md) documents the legacy full-snapshot and
  reset operation, including its security and size warnings. Its path is
  retained for compatibility.

Use [`docs/evaluation/`](../../docs/evaluation/README.md) for durable
architecture, ownership, reproducibility, and analysis policy. Human analysis
reports belong in the ignored `docs/evaluation/analysis/` workspace; raw PDFs
and supporting evidence belong in the ignored `evaluation/archives/` tree.
