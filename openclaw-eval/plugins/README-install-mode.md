# Install-mode plugin onboarding

OpenClaw evaluation images can include plugins from two sources:

| Mode | Use | Source of truth |
|---|---|---|
| Bundled source | Repository-owned or modified plugins | `openclaw-eval/plugins/<id>/` plus `evaluation/config/plugin_registry.yaml` |
| npm install | Published plugins tested without source changes | A pinned npm package declared in `evaluation/config/plugin_registry.yaml` |

The active registry and builder are authoritative. Historical design notes
under `docs/superpowers/` are retained for archaeology but are not current
policy. See the [system-configuration guide](../../evaluation/docs/system-configs/README.md)
and [context-engine authoring guide](README-context-engine.md) for the current
evaluation contracts.

## Quick start

Plugin selection uses the same `<id>[@<version>]` syntax in the image builder
and evaluation CLI. npm-backed plugins require an explicit version:

```bash
# Registered npm memory plugin
uv run python openclaw-eval/harness/build.py \
  --memory-plugin hindsight-plugin@0.5.0 \
  --dry-run

# Registered npm context engine, paired with the built-in memory baseline
uv run python openclaw-eval/harness/build.py \
  --memory-plugin memory-core \
  --context-engine hypercompositor@0.9.6 \
  --dry-run
```

The registry validates each plugin's kind (`memory` or `context-engine`) and
maps the ID to its package. The build then:

1. builds or reuses the OpenClaw base;
2. runs `npm pack` for each pinned npm spec and extracts it into
   `/opt/openclaw/extensions/<id>/` in the evaluation image;
3. makes the extension discoverable through the rendered
   `plugins.load.paths` and the appropriate plugin slot; and
4. appends the concrete image and plugin metadata to
   `evaluation/config/image_manifest.yaml`, unless
   `--no-image-manifest` is supplied.

Use `--dry-run` to inspect resolution without invoking Docker.

## Registering an npm plugin

Add the ID, kind, type, and package to
`evaluation/config/plugin_registry.yaml`:

```yaml
your-engine:
  kind: context-engine
  type: npm
  npm_package: "@example/your-engine"
```

Then select `your-engine@<version>`. Do not use an unregistered package name
as a public plugin ID, and do not rely on `latest`; explicit versions keep
resolution and image tags reproducible.

For an alternate package in an npm registry that `npm pack` can reach from the
Docker build, keep the registered ID and override only its install spec:

```bash
uv run python openclaw-eval/harness/build.py \
  --context-engine hypercompositor@0.9.6 \
  --plugin-spec hypercompositor=npm:@your-scope/hypercompositor@0.9.6 \
  --dry-run
```

This flow does not promise access to a host-local path or archive: `npm pack`
runs inside the Docker build environment, where an arbitrary host path is not
present. Publish or otherwise expose the package through a registry reachable
from that build environment.

The legacy `--install-spec`, `--install-plugin-id`, and
`--extra-install-spec` flags remain as deprecated compatibility shims. They
resolve through the same registry and emit a deprecation warning; new scripts
must use `--memory-plugin`, `--context-engine`, and `--plugin-spec`.

## Reproducibility and image selection

The plugin revision in an image tag is content-derived for bundled source and
spec-derived for npm plugins. Different pinned npm versions therefore produce
different tags. A successful normal build records the tag in the image
manifest, after which evaluation can resolve the plugin selection:

```bash
uv run python -m evaluation.cli \
  --dataset locomo \
  --system openclaw-docker \
  --memory-plugin memory-core \
  --context-engine hypercompositor@0.9.6
```

An explicit `--image <tag>` bypasses manifest lookup. Use only a concrete,
locally available or pullable tag; system YAML rejects image placeholders.

## Source modifications

Environment- or configuration-only changes can use a pinned published
package. For source changes, add a repository-owned fork under
`openclaw-eval/plugins/<fork-id>/`, register it as `bundled-source`, and build
that ID. There is no pnpm-patch onboarding tier.

## Operational limits

- npm packages are executed as part of the built image. Selection is an
  explicit operator trust decision; the direct `npm pack` extraction path is
  not a security review of third-party code.
- Installation happens at image-build time. Per-conversation containers do
  not download plugins at startup.
- Packaging and slot wiring do not import benchmark history into a context
  engine. The native `engine_import_history` bridge path is currently unwired;
  each engine needs a documented, tested ingest path before its scores are
  treated as comparable.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run pytest \
  -p no:cacheprovider \
  tests/evaluation/test_build_new_cli.py \
  tests/evaluation/test_build_install_spec.py \
  tests/evaluation/test_plugin_registry.py \
  tests/evaluation/test_plugin_resolver.py -q
```

These tests cover registry resolution, kind and version validation, deprecated
flag migration, deterministic revisions, and build-plan emission. They do not
build an image or execute a third-party plugin.
