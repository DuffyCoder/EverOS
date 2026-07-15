# Evaluation Framework Operations

This directory contains operational documentation coupled to code in
`evaluation/`: adapter behavior, run lifecycle, recovery procedures, and other
instructions that must change with the framework implementation.

The target framework boundary is runtime-agnostic: `evaluation/` is intended to
own benchmark protocols, datasets, metrics, configuration, adapters, and public
CLI entry points, while `openclaw-eval/` owns the OpenClaw-specific container,
plugin, build, and harness runtime. This separation is not fully implemented
yet; the planned runtime registry will enforce the dependency direction. Until
then, existing OpenClaw coupling is compatibility code, and new framework code
must not add dependencies on the internal `openclaw-eval/` directory layout.

Current component notes:

- [`openclaw_adapter.md`](openclaw_adapter.md) documents the framework adapter's
  fidelity and comparison semantics.
- [`round-finish.md`](round-finish.md) documents the legacy full-snapshot and
  reset operation, including its security and size warnings. Its path is
  retained for compatibility.

Use [`docs/evaluation/`](../../docs/evaluation/README.md) for durable
architecture, ownership, reproducibility, and analysis policy. Human analysis
reports belong in the ignored `docs/evaluation/analysis/` workspace; raw PDFs
and supporting evidence belong in the ignored `evaluation/archives/` tree.
