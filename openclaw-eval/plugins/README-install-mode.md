# Install-Mode Plugin Onboarding

Two ways to bring a plugin into the eval framework:

| Mode | When to use | Source of truth |
|---|---|---|
| **Bundled** (existing) | Self-authored or modified plugins under our control | `openclaw-eval/plugins/<name>/` (TS source) |
| **Install** (this doc) | Officially-published plugins (npm / clawhub / marketplace) we test as-is | The published package |

## Quick start (install mode)

```bash
python3 openclaw-eval/harness/build.py \
    --memory-plugin hindsight-plugin \
    --install-spec npm:hindsight-plugin@0.5.0
```

Build steps performed:

1. Reuse `openclaw-base:<sha>-memory-core-slim` (no per-plugin base build).
2. Build eval layer with `INSTALL_SPEC` + `INSTALL_PLUGIN_ID` build args; the
   image runs `openclaw plugins install npm:hindsight-plugin@0.5.0 --force --pin
   --dangerously-force-unsafe-install` at build time.
3. Plugin lands at `/opt/openclaw/extensions/hindsight-plugin/` inside the image
   (because `OPENCLAW_HOME=/opt/openclaw`).
4. Image tag: `openclaw-eval:<sha>-install-hindsight-plugin-<spec_hash>-slim`.

At runtime, `entrypoint.sh` injects the install dir into the rendered
config's `plugins.load.paths` so openclaw discovers the plugin alongside
bundled ones.

## Supported install spec formats

| Spec | Plugin id derivation | Example |
|---|---|---|
| `npm:<name>@<version>` | name (after `@scope/` if scoped) | `npm:@mem0/openclaw-plugin@1.2.0` -> `openclaw-plugin` |
| `clawhub:<owner>/<name>` | name | `clawhub:acme/cool-plugin` -> `cool-plugin` |
| `marketplace:<name>` | name | `marketplace:my-engine` -> `my-engine` |
| `local:<name>` | name | `local:openviking` -> `openviking` |
| Local path / archive | not derivable | must pass `--install-plugin-id <id>` |

## When `--install-plugin-id` is required

For raw paths or archives, build.py can't infer the id, so:

```bash
python3 openclaw-eval/harness/build.py \
    --memory-plugin foo \
    --install-spec /tmp/foo-1.0.tgz \
    --install-plugin-id foo
```

For vendored local plugins under `openclaw-eval/plugins/<name>/`, prefer:

```bash
python3 openclaw-eval/harness/build.py \
    --memory-plugin memory-core \
    --install-spec local:openviking
```

For npm/clawhub/marketplace specs, derivation is automatic; pass
`--install-plugin-id` only to override.

## Required: `--memory-plugin` matches installed id

build.py rejects mismatched ids:

```
[build] ERROR: --memory-plugin 'evermemos' must match installed plugin id
'hindsight-plugin' when --install-spec is set.
```

This is because `entrypoint.sh` uses `MEMORY_PLUGIN_ID` for plugin slot
wiring (`plugins.slots.memory`); a mismatch silently binds the slot to
a non-existent plugin.

## Refusing bundled ids

`memory-core` and `noop` are bundled-only — they're built into the openclaw
image at the base layer and have no installable npm package. build.py
rejects:

```
[build] ERROR: --install-spec is not supported for 'memory-core' (bundled
plugin, not an install target).
```

## Reproducibility

Each install spec produces a different image tag rev, derived from
`sha256(spec)[:7]`:

```
openclaw-eval:7da23c3-install-hindsight-plugin-a1b2c3d-slim   # @0.5.0
openclaw-eval:7da23c3-install-hindsight-plugin-e4f5g6h-slim   # @0.5.1
```

Different versions cannot collide; cached images are version-specific.

`--pin` (passed to `openclaw plugins install`) records the exact resolved
npm version in openclaw's persisted config, so re-runs of the same image
get the same resolution.

## Modifying installed plugins

Per the design note (`docs/superpowers/specs/2026-04-30-plugin-kinds-design-note.md`),
only Tier 1 (env/config-driven, no source change) is supported via this
flow. For source-level modifications, fork the plugin into
`openclaw-eval/plugins/<your-fork>/` and use bundled mode (Tier 3 in the
design note).

The intermediate Tier 2 (pnpm patch) is **not implemented** — per user
direction (`session 2026-04-30: 方案 A 当中的第二档修改策略不需要`).

## Limitations

- The `openclaw plugins install` step uses
  `--dangerously-force-unsafe-install` to bypass safety scanning. The
  eval framework only installs specs explicitly listed in build commands
  by an operator; if you prefer scanning, drop the flag in
  `Dockerfile.eval` and accept that flagged plugins will fail at build.
- npm-published context-engine plugins are NOT yet runnable through this
  flow — see `docs/superpowers/specs/2026-04-30-plugin-kinds-design-note.md`
  for the Stage 3 prerequisites (4 downstream code path changes,
  upstream openclaw normalization patch, etc.).
- This is a build-time install. Per-conversation containers don't run
  `openclaw plugins install` at startup — that would re-download on
  every container spawn.

## Tests

```bash
.venv/bin/python -m pytest tests/evaluation/test_build_install_spec.py -v
```

12 tests cover: spec hash determinism, plugin id derivation for every
spec format, argparse validation (memory-plugin mismatch / bundled id
rejection / underivable id requires explicit flag).
