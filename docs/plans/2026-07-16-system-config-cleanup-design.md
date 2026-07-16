# Evaluation System Configuration Cleanup Design

## Context

`evaluation/config/systems/` currently contains 36 tracked YAML files in one
flat directory. They mix public benchmark presets, compatibility aliases,
historical experiments, ablations, prototype wiring, and tool-only
configurations. The CLI treats each filename stem as a public system id:
`--system <id>` is resolved directly to `systems/<id>.yaml`, and that requested
id is also embedded in the default result path.

The files are syntactically valid, but their maintenance quality is uneven:

- two pairs contain the same parsed configuration;
- closely related families repeat most of their content;
- 689 of 2,424 lines are whole-line comments, including obsolete debugging
  history and machine-specific capacity calculations;
- the loader has no schema, inheritance, alias, or unknown-field validation;
- several configured fields are not consumed by the current implementation;
- experiments and operator-only presets appear beside long-lived public
  configurations without status or ownership metadata; and
- current scripts and documentation disagree about the latency-baseline
  presets.

The cleanup must make the configuration surface understandable and strictly
validated without breaking any existing `--system` id.

## Decision

Use compatibility-preserving option A.

All 36 existing system ids remain valid. Physical files may move, duplicate
content may be removed, and experimental presets may be classified separately,
but the CLI continues to accept every old id and continues to use the requested
id in result-directory names.

## Goals

- Preserve every existing `--system` id and its result-path naming behavior.
- Separate canonical, alias, experiment, ablation, tooling, and base configs.
- Maintain one copy of shared configuration through explicit inheritance.
- Move historical narratives and operational runbooks out of YAML files.
- Reject malformed, unknown, unsafe, or placeholder configuration values.
- Record requested and canonical identities in resolved run metadata.
- Prove that structural migration does not alter adapter-consumed behavior.
- Keep behavioral fixes separate from structure-only migration commits.

## Non-goals

- Running Docker, paid APIs, or full benchmark evaluations.
- Deleting archived evaluation sources, old worktrees, or branches.
- Changing adapter behavior as an incidental effect of moving configuration.
- Treating historical result archives as sufficient proof that an old
  experiment config can be deleted.
- Renaming existing result directories.

## Configuration registry and resolution

Add `evaluation/config/systems/index.yaml` as the authoritative registry of
public system ids. Each entry records:

- stable system id;
- target configuration path;
- adapter;
- category;
- status;
- canonical id when the entry is an alias;
- deprecation replacement when applicable;
- short purpose; and
- optional documentation reference.

The physical tree is organized as:

```text
evaluation/config/systems/
├── index.yaml
├── _bases/
├── canonical/
├── experiments/
├── ablations/
└── tooling/
```

`_bases/` entries are reusable fragments and cannot be selected directly.
Runnable configurations may declare `extends` with another physical config
path. Alias entries exist only in the index and do not duplicate YAML content.

Resolution follows this sequence:

```text
requested system id
→ index lookup
→ alias resolution
→ configuration path lookup
→ extends-chain loading
→ deterministic deep merge
→ environment marker handling
→ adapter-specific schema validation
→ resolved configuration
```

Alias and inheritance graphs must be acyclic. Paths must remain under the
systems configuration root. An alias cannot itself be an inheritance target.
The requested id remains the result-path id, while resolved metadata records
both requested and canonical ids, source paths, inheritance chain, and a
redacted configuration digest.

## Classification

Configurations are classified as follows:

- `canonical`: supported, long-lived presets and public examples;
- `alias`: old or convenient public names resolving to a canonical id;
- `experiment`: prototype or historical evaluation wiring that remains
  runnable for compatibility;
- `ablation`: a controlled variation of a canonical experiment axis;
- `tooling`: a preset whose semantics serve an operator tool rather than
  benchmark scoring;
- `base`: a non-runnable shared fragment; and
- `deprecated`: a runnable compatibility id with a documented replacement and
  warning.

The two exact duplicate pairs become explicit aliases:

- `openclaw-hybrid` aliases `openclaw`;
- `hermes` aliases `hermes-holographic`.

Near-duplicate families use bases and small overrides:

- Hermes variants share common LLM, search, answer, and repository settings.
- OpenClaw native presets express retrieval backend and flush-mode axes.
- Docker plugin presets express plugin wiring, image, networking, and tuned
  resource differences.
- Memcore session-bundle variants express only tail differences.
- OpenViking presets share their stable ingest and runtime settings, with
  separate ablation and tooling overrides.

The serial OpenViking preset remains a tooling configuration because QA-log
attribution relies on its global serialization. Experimental ids remain
runnable; they are not silently deleted because they lack current direct
references.

## Comments and documentation

YAML comments are limited to:

- why a non-obvious non-default value exists;
- a safety or resource warning;
- a compatibility invariant; or
- a concise instruction that must be visible beside the field.

Historical fixes, one-off failures, provider behavior investigations, machine
RAM estimates, reproduction procedures, and long architecture explanations move
to `evaluation/docs/system-configs/`. The documentation includes:

- registry and category semantics;
- inheritance and alias rules;
- secret-handling rules;
- family-specific maintenance notes;
- OpenViking operational and historical notes; and
- a catalog of all supported ids, their canonical target, and status.

## Validation and schemas

Introduce a dedicated system-config loader and adapter-specific validation.
Every final resolved configuration must:

- be a mapping;
- contain a registered adapter;
- satisfy required fields and field types for that adapter;
- use known enum values;
- reject unknown fields;
- reject unresolved image placeholders such as `PLUGIN_REV`;
- reject path escape, alias cycles, and inheritance cycles; and
- use secret environment markers rather than materialized credential values.

Secret-bearing fields are represented as environment-variable references.
Resolved run metadata records variable names or redacted markers, never secret
values. OpenClaw embedding credentials migrate to `api_key_env` rather than a
field whose environment placeholder is expanded into the resolved dictionary.

The structure-only migration does not activate currently ignored fields.
Confirmed behavior-affecting defects are handled later in separate commits:

- `memos.request_interval` versus the adapter's
  `requests_per_second` setting;
- `memu.min_similarity` not being read from system configuration;
- `evermemos_cloud_api.search.timeout_seconds` not being consumed; and
- retry settings whose actual owner is the pipeline retry policy.

Fields proven to have no consumer, including current
`openclaw.prompts.{memory_mode,flush_mode,answer_mode}`, are removed during
migration only after compatibility tests establish that adapter-consumed
configuration remains unchanged.

## Compatibility baseline

Before moving files, capture a redacted semantic baseline for all 36 ids. The
baseline contains:

- requested id and canonical identity under the old layout;
- adapter;
- recursively expanded environment-variable markers without real values;
- adapter-consumed fields;
- default result-directory name; and
- normalized configuration digest.

The comparison deliberately excludes comments, ordering, removed unused fields,
and secrets. Except for separately approved behavior fixes, every migrated id
must resolve to the same adapter-consumed semantics.

Old ids continue to work after their flat files are removed because the CLI
uses the registry-backed loader. Missing and unknown ids return a non-zero exit
status and show close matches, alias information, or a replacement id. This
also fixes the current false-success path where a missing YAML prints an error
and returns normally.

## Migration sequence

1. Add tests that inventory and parse the existing 36 ids.
2. Capture the redacted pre-migration semantic baseline.
3. Implement registry, alias, inheritance, and validation without moving files.
4. Switch the CLI to the new loader while preserving requested-id result paths.
5. Migrate one family at a time:
   - public online and EverMemOS configs;
   - Hermes;
   - OpenClaw native;
   - OpenClaw Docker plugins;
   - Memcore session-bundle;
   - OpenViking;
   - experiments, ablations, and tooling.
6. Move long comments into durable documentation.
7. Remove superseded flat YAML files only after all 36 ids pass compatibility
   tests through the registry.
8. Correct script/documentation drift.
9. Address behavior-changing field defects in separate commits and tests.

Each family migration is independently reviewable and reversible.

## Verification

Verification has four layers:

1. **Registry tests:** all old ids exist; categories and targets are valid;
   aliases and inheritance are acyclic; every path exists.
2. **Loader tests:** deterministic merge order, environment markers, path
   containment, alias behavior, deprecation warnings, and helpful unknown-id
   errors.
3. **Schema tests:** all shipped configs validate; unknown keys, wrong types,
   unsupported enums, plaintext secrets, and placeholder images fail.
4. **Compatibility tests:** each old id preserves adapter-consumed semantics,
   requested-id result naming, and canonical identity reporting.

The final gate includes focused system-config tests, current evaluation tests,
the repository compatibility baseline, documentation-link checks, and
`git diff --check`. It does not start external services or invoke paid APIs.

## Completion criteria

- All 36 original ids resolve successfully.
- Requested ids and default result paths remain stable.
- Canonical and alias identity is visible in resolved metadata.
- Duplicate configuration bodies are eliminated.
- Near-duplicate families contain only meaningful overrides.
- OpenViking YAML comments are reduced to concise invariants.
- Every shipped configuration is covered by registry and schema tests.
- Unknown fields and unsafe secrets fail closed.
- Current scripts and docs name the same system presets.
- Structure-only commits preserve adapter-consumed semantics.
- Behavior fixes are isolated, documented, and independently testable.
