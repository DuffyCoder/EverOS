# Evaluation system configurations

[`evaluation/config/systems/index.yaml`](../../config/systems/index.yaml) is
the public registry. Select a system with its registry ID (for example,
`openclaw-docker-openviking-session-bundle-noop`), never with a YAML filename.
The registry path is an internal implementation detail and may move without
changing the public ID.

## Categories

- `canonical/` contains active, supported benchmark presets.
- `experiments/` contains exploratory variants whose behavior may still move.
- `ablations/` contains controlled one-variable comparisons.
- `tooling/` contains diagnostic presets that are not benchmark defaults.
- `alias` entries exist only in `index.yaml` and preserve compatibility IDs.
- `_bases/` contains inheritance fragments and is not a public category.

The full 36-ID catalog is intentionally deferred. Until it is published, use
`index.yaml` as the authoritative list of IDs, category, status, and docs.

## Inheritance rules

A leaf may declare `extends: "_bases/<name>.yaml"`. Inheritance recursively
merges mappings; lists and scalar values replace their parent value. Put a
field in a base only when every leaf has that field with the same complete
value. Optional keys must remain absent from leaves that did not define them.
Keep each family to the shortest useful source chain, normally one base and
one categorized leaf.

The session-bundle family starts with the two bases
`_bases/openclaw-memcore-session-bundle.yaml` and
`_bases/openclaw-openviking.yaml`. See the [OpenViking operational
notes](openviking.md) before changing the OpenViking leaves.

The three memcore leaves intentionally differ only in
`openclaw.ingest_session_tail`. The emptytail value `""` explicitly disables
the tail; it does not fall back to an adapter default.

## Secrets and paths

Commit only environment markers such as `${LLM_API_KEY}`; never put a secret
value in a system YAML, fixture, result, or document. Runtime secret names must
be explicitly allowlisted where a container needs them.

Every registry `path` and `extends` target is a relative POSIX path contained
under `evaluation/config/systems/`. Absolute paths, `..`, backslashes, symlink
traversal, and direct extension of an alias are rejected by the loader.
