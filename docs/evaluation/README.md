# Evaluation Documentation

This directory contains durable, repository-level documentation for evaluation
architecture, ownership, reproducibility, and analysis policy. For commands and
configuration supported by the evaluation code, start with the
[evaluation framework guide](../../evaluation/README.md).

## Ownership Boundaries

| Path | Owner and purpose |
| --- | --- |
| `evaluation/` | Generic benchmark framework: protocols, datasets, metrics, configuration, adapters, and public CLI entry points. |
| `openclaw-eval/` | OpenClaw-specific build, container, plugin, and harness runtime. It may depend on public interfaces from `evaluation/`. |
| `docs/evaluation/` | Long-lived architecture, policy, reproducibility, and analysis guidance. |
| `evaluation/docs/` | Operational notes that must evolve with the framework implementation. |
| `docs/superpowers/` | Historical plans, specifications, runbooks, and phase records; not the current repository contract. |

The dependency direction is one way: `openclaw-eval/` depends on the generic
framework. Code in `evaluation/` must not import or reach into the
`openclaw-eval/` file layout. Runtime-specific behavior crosses the boundary
through framework adapters, explicit configuration, or other public contracts.
Existing public paths and commands remain compatibility surfaces.

## Data Ownership

`evaluation/data/` is the authoritative location for benchmark datasets used by
the evaluation framework. Operator-fetched or generated large datasets remain
local according to `.gitignore`; intentionally bundled fixtures stay tracked.

`data/` contains product demo and example inputs. `data/locomo10.json` is a
compatibility mirror of `evaluation/data/locomo/locomo10.json`, not a second
authoritative benchmark source. Changes to a controlled mirror must preserve
byte-for-byte or explicitly tested equivalence.

## Analysis and Archives

Human-authored analysis reports are local artifacts under
[`docs/evaluation/analysis/`](analysis/README.md). Git tracks only the policy
README and report template in that directory. Each local report records its run
identifier, code commit, archive location, and checksums.

Raw PDFs, supporting data, manifests, and compact reproducibility packages live
under the ignored `evaluation/archives/` tree. Do not commit credentials,
resolved secrets, full runtime workspaces, caches, or reproducible intermediates.

## Naming Compatibility

The repository currently contains the names EverOS, EverMemOS, and `memsys` in
different user-facing and technical compatibility surfaces. This ownership
policy does not rename or consolidate them.

## Related Documentation

- [Repository-layout compatibility baseline](compatibility-baseline.md)
- [Framework operations](../../evaluation/docs/README.md)
- [Local analysis policy and template](analysis/README.md)
- [Historical evaluation records](../superpowers/README.md)
