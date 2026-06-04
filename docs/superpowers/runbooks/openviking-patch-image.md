# OpenViking Plugin Patch Image Runbook

This runbook records the fast path for rebuilding the OpenViking plugin layer
after editing files under `openclaw-eval/plugins/openviking/`.

Use this when evaluation should pick up local OpenViking plugin changes without
rebuilding OpenClaw or replacing the already validated eval harness.

## Recommended Path

Start from a known-good OpenViking eval image and replace only the installed
OpenViking plugin directory:

```text
ghcr.io/duffycoder/openclaw-eval-plugins:7da23c3-openviking-b7e6bcb-findlast-slim
```

This preserves the base image's existing eval runtime fixes, including:

- `openclaw_eval_bridge.mjs` reading the last assistant payload as the final QA answer.
- `archive_session` support for resetting short-term QA transcripts.
- Existing entrypoint and OpenClaw runtime wiring.

The patch only replaces:

```text
/opt/openclaw/extensions/openviking
```

## Regenerate The Tarball

The OpenViking eval image does not read plugin source directly from the host
checkout. Runtime plugin code comes from this tarball:

```text
openclaw-eval/plugins/openviking/openclaw-openviking-2026.6.04.tgz
```

Confirm changed files are packageable:

```bash
cd /home/ze.qian/workspace/EverOS

git status --short openclaw-eval/plugins/openviking
git diff --name-only -- openclaw-eval/plugins/openviking
```

Check `openclaw-eval/plugins/openviking/package.json`. Its `files` list controls
what `npm pack` includes. As of this runbook, root `*.ts`, `commands/setup.ts`,
`openclaw.plugin.json`, and `package.json` are packaged.

Dry-run the package first:

```bash
cd /home/ze.qian/workspace/EverOS/openclaw-eval/plugins/openviking
npm pack --dry-run
```

Confirm changed runtime files, for example `index.ts`, appear in `Tarball Contents`.

Generate the tarball:

```bash
cd /home/ze.qian/workspace/EverOS/openclaw-eval/plugins/openviking
npm pack
```

Verify it was updated:

```bash
ls -l openclaw-openviking-2026.6.04.tgz
```

## Build The Plugin Patch Image

Use the plugin directory as Docker build context. The Dockerfile is:

```text
openclaw-eval/Dockerfile.ov-plugin-patch
```

Build:

```bash
docker build \
  -f /home/ze.qian/workspace/EverOS/openclaw-eval/Dockerfile.ov-plugin-patch \
  -t openclaw-eval-plugins:7da23c3-openviking-2026.6.04-on-findlast-slim \
  /home/ze.qian/workspace/EverOS/openclaw-eval/plugins/openviking
```

The build should show `node_modules` under:

```text
/opt/openclaw/extensions/openviking/
```

That confirms `npm install --omit=dev --omit=optional` actually ran inside the
patched plugin directory.

## Verify The Image

Inspect the local image:

```bash
docker image inspect \
  openclaw-eval-plugins:7da23c3-openviking-2026.6.04-on-findlast-slim \
  --format '{{.Id}} {{.RepoTags}}'
```

Verify the eval harness fixes from the base image are still present:

```bash
docker run --rm --entrypoint grep \
  openclaw-eval-plugins:7da23c3-openviking-2026.6.04-on-findlast-slim \
  -n "findLast\\|archive_session" /eval/openclaw_eval_bridge.mjs
```

Both `findLast` and `archive_session` should appear. If they do not, the image
is not based on the expected fixed OpenViking eval image.

## Use In Evaluation

For a local run, point the OpenViking docker system config at the local tag:

```yaml
openclaw_docker:
  image: "openclaw-eval-plugins:7da23c3-openviking-2026.6.04-on-findlast-slim"
```

One existing OpenViking config is:

```text
evaluation/config/systems/openclaw-docker-openviking-session-bundle-noop.yaml
```

If sharing the image with another machine, retag and push it to the registry,
then pin the pushed tag or digest in the system config.

## Clean Eval-Base Alternative

`openclaw-eval/Dockerfile.ov-patch` layers the plugin on top of:

```text
ghcr.io/duffycoder/openclaw-eval-base:7da23c3-clean-edf6d4d-slim
```

That path is more fragile because the clean eval-base may not contain all eval
runtime fixes that were already present in a validated OpenViking plugin image.
If using that path, the Dockerfile must also copy current eval harness files
from `openclaw-eval/container/` into `/eval/` and `/etc/openclaw/`.

Prefer `Dockerfile.ov-plugin-patch` when a known-good OpenViking image is
available.

## Relationship To build.py

`openviking` is registered in `evaluation/config/plugin_registry.yaml` as:

```yaml
openviking:
  kind: context-engine
  type: bundled-source
```

The generic `openclaw-eval/harness/build.py --eval-base-image ...` split-build
path supports npm-installed plugins. It rejects bundled-source plugins because
those normally need staging into the OpenClaw repo and inclusion via
`OPENCLAW_EXTENSIONS`.

`Dockerfile.ov-plugin-patch` is a deliberate fast-path override: it installs the
locally packed OpenViking tarball into `/opt/openclaw/extensions/openviking` on
top of a known-good OpenViking eval image.
