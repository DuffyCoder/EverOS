#!/usr/bin/env python3
"""
Stage 1 image build orchestrator.

Two-step build:
  1. openclaw-base:<sha>-<bundle>-<variant>    (openclaw repo Dockerfile)
  2. openclaw-eval:<sha>-<bundle>-<rev>-<variant>  (Dockerfile.eval)

Usage:
    python openclaw-eval/harness/build.py \\
        --memory-plugin evermemos \\
        --context-engine hypercompositor@0.9.6 \\
        --openclaw-repo /Data3/shutong.shan/openclaw/repo

The CLI accepts:

  --memory-plugin <id> | <id>@<version> | none
  --context-engine <id> | <id>@<version> | none
  --plugin-spec <id>=<spec>          (override registry npm package)

Plugin metadata is read from evaluation/config/plugin_registry.yaml.
After a successful build, an entry is appended to
evaluation/config/image_manifest.yaml so evaluation.cli can resolve
``--memory-plugin / --context-engine`` to a concrete image tag.

Deprecated flags (still accepted, mapped to new with DeprecationWarning):
  --install-spec / --install-plugin-id / --extra-install-spec /
  --extra-install-plugin-id

Outputs:
    Logs the two image tags. Exit non-zero if any step fails.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Optional


# Make evaluation.src.plugins.* importable when build.py runs as a script.
_HARNESS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HARNESS_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluation.src.plugins.manifest import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    ManifestEntry,
    ManifestPlugin,
    append_entry,
    now_iso,
)
from evaluation.src.plugins.registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    PluginEntry,
    load_registry,
)
from evaluation.src.plugins.resolver import (  # noqa: E402
    PluginRef,
    ResolverError,
    parse_plugin_spec_overrides,
    parse_ref,
)


def short_sha(path: Path, ref: str = "HEAD") -> str:
    res = subprocess.run(
        ["git", "rev-parse", "--short=7", ref],
        cwd=path, capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


@functools.lru_cache(maxsize=None)
def plugin_content_hash(plugin_dir: Path) -> str:
    """Hash of plugin source files for tag reproducibility.

    Skips dot-prefixed entries (relative to ``plugin_dir``) and the standard
    build/dep dirs. Earlier version inspected ``p.parts`` of the absolute
    path, which always rejected files when the plugin lived under
    ``.claude/worktrees/...`` — every file got filtered, leaving the empty
    sha256 prefix ``e3b0c44`` as the rev for any plugin in a worktree.

    Memoized: ``compute_plugin_rev`` and ``build_manifest_plugins`` both
    hash the same plugin during one build invocation. Cache lifetime is
    process-scoped, so a single build run sees a stable hash even if the
    plugin source were modified mid-build (which would be a separate bug).
    """
    if not plugin_dir.exists():
        return "0000000"
    h = hashlib.sha256()
    skip_names = {"node_modules", "dist"}
    for p in sorted(plugin_dir.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(plugin_dir).parts
        if any(part.startswith(".") or part in skip_names for part in rel_parts):
            continue
        h.update("/".join(rel_parts).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:7]


def docker_image_exists(tag: str) -> bool:
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return res.returncode == 0


def install_spec_hash(spec: str) -> str:
    """Stable 7-char hash of the install spec for image tag rev.

    Replaces ``plugin_content_hash`` when a plugin is loaded via
    ``openclaw plugins install <spec>`` rather than staged from
    ``openclaw-eval/plugins/<name>/``. Keeping a content-derived rev
    ensures different specs (e.g. v1.2.0 vs v1.2.1) produce different
    image tags so cached images don't drift.
    """
    return hashlib.sha256(spec.encode("utf-8")).hexdigest()[:7]


def derive_plugin_id_from_spec(spec: str) -> Optional[str]:
    """Best-effort plugin id derivation from an install spec.

    Cases handled:
      - ``npm:@scope/name@version`` -> ``name``
      - ``npm:name@version``         -> ``name``
      - ``clawhub:owner/name``       -> ``name``
      - ``marketplace:name``         -> ``name``

    Returns ``None`` for raw paths/archives or any spec we can't
    confidently parse. Caller should fall back to ``--install-plugin-id``.
    """
    s = spec.strip()
    if s.startswith("npm:"):
        rest = s[len("npm:"):]
        if rest.startswith("@"):
            slash = rest.find("/")
            if slash < 0:
                return None
            after_scope = rest[slash + 1:]
            at = after_scope.find("@")
            return after_scope[:at] if at > 0 else after_scope
        at = rest.find("@")
        return rest[:at] if at > 0 else rest
    if s.startswith("clawhub:"):
        rest = s[len("clawhub:"):]
        slash = rest.find("/")
        return rest[slash + 1:] if slash >= 0 else rest
    if s.startswith("marketplace:"):
        return s[len("marketplace:"):]
    return None


def run_step(label: str, cmd: list[str], cwd: Optional[Path] = None) -> None:
    print(f"\n[build] {label}")
    print(f"[build]  $ {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd)
    if res.returncode != 0:
        print(f"[build] {label} FAILED (exit {res.returncode})", file=sys.stderr)
        sys.exit(res.returncode)


def stage_external_plugin(
    plugins_dir: Path,
    plugin_name: str,
    openclaw_repo: Path,
) -> None:
    """Sync ``openclaw-eval/plugins/<name>/`` into ``<openclaw_repo>/extensions/<name>/``.

    External plugins (anything other than ``memory-core``/``noop``) live in
    *our* eval repo. The openclaw build pipeline only sees plugins under its
    own ``extensions/`` directory, so we stage the source there before
    invoking ``docker build`` and rely on ``OPENCLAW_EXTENSIONS`` to opt them
    in.

    The staging directory is overwritten on every build so the plugin source
    of truth stays in our repo. We don't delete after build because the
    openclaw repo may be re-used across builds; the staged dir is
    idempotent.
    """
    src = plugins_dir / plugin_name
    if not src.exists():
        raise SystemExit(
            f"[build] ERROR: external plugin source {src} not found"
        )
    dst = openclaw_repo / "extensions" / plugin_name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("node_modules", "dist", "*.tsbuildinfo"))
    print(f"[build] staged plugin source: {src} -> {dst}")


def build_base(
    openclaw_repo: Path,
    memory_plugin: str,
    openclaw_sha: str,
    *,
    variant: str = "slim",
    skip_if_exists: bool = True,
    plugins_dir: Optional[Path] = None,
    extra_bundled_plugins: Optional[list[str]] = None,
) -> str:
    """Step 1: build openclaw-base with OPENCLAW_EXTENSIONS opt-in.

    ``memory_plugin`` is the primary bundled plugin (or ``memory-core`` /
    ``noop`` for baseline). ``extra_bundled_plugins`` is for additional
    bundled-source plugins to stage and include in OPENCLAW_EXTENSIONS;
    used when both slots have bundled-source plugins (e.g. memory plugin
    'evermemos' + context-engine 'stub-engine'). The base tag includes
    every bundled plugin id joined by '_'.
    """
    extras = list(extra_bundled_plugins or [])

    # Tag derivation: include all bundled plugins.
    bundled_ids: list[str] = []
    if memory_plugin not in ("memory-core", "noop"):
        bundled_ids.append(memory_plugin)
    bundled_ids.extend(extras)
    bundle_name = "_".join(bundled_ids) if bundled_ids else "memory-core"
    tag = f"openclaw-base:{openclaw_sha}-{bundle_name}-{variant}"

    if skip_if_exists and docker_image_exists(tag):
        print(f"[build] base image {tag} already exists; skipping rebuild")
        return tag

    extensions_list = ["memory-core"]
    for pid in bundled_ids:
        if plugins_dir is None:
            raise SystemExit(
                f"[build] ERROR: external plugin '{pid}' requires plugins_dir"
            )
        stage_external_plugin(plugins_dir, pid, openclaw_repo)
        extensions_list.append(pid)
    extensions = " ".join(extensions_list)

    cmd = [
        "docker", "build",
        "--build-arg", f"OPENCLAW_EXTENSIONS={extensions}",
        "--build-arg", f"OPENCLAW_VARIANT={variant}",
        "-t", tag,
        ".",
    ]
    run_step(
        f"Step 1: openclaw-base ({bundle_name}, {variant})",
        cmd, cwd=openclaw_repo,
    )
    return tag


def stage_active_sidecar(eval_dir: Path, plugins_dir: Path, memory_plugin: str) -> None:
    """Stage plugins/<name>/sidecar/ into eval_dir/_active_sidecar/.

    Dockerfile.eval has a single ``COPY _active_sidecar/ /sidecar/`` that
    works uniformly across plugins. For plugins without a sidecar
    (memory-core / noop / a stub-only test), we leave the directory empty
    so the venv setup step skips itself (no requirements.txt present).
    """
    dst = eval_dir / "_active_sidecar"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    if memory_plugin in ("memory-core", "noop"):
        # Leave _active_sidecar/ empty.
        return

    src = plugins_dir / memory_plugin / "sidecar"
    if not src.exists():
        # Plugin has no sidecar dir at all (e.g. stub before .gitkeep).
        return
    # Copy contents (skip cache/test detritus).
    for entry in src.iterdir():
        if entry.name in ("__pycache__",) or entry.name.endswith(".pyc"):
            continue
        if entry.is_dir():
            shutil.copytree(entry, dst / entry.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(entry, dst / entry.name)
    print(f"[build] staged sidecar: {src} -> {dst}")


def build_eval_layer(
    eval_dir: Path,
    base_tag: str,
    memory_plugin: str,
    openclaw_sha: str,
    plugin_rev: str,
    *,
    variant: str = "slim",
    plugins_dir: Optional[Path] = None,
    install_spec: Optional[str] = None,
    install_plugin_id: Optional[str] = None,
    extra_install_specs: Optional[list[tuple[str, str]]] = None,
    tag_override: Optional[str] = None,
) -> str:
    """Step 2: layer eval-runtime on top of openclaw-base.

    When ``install_spec`` is set, the eval layer Dockerfile runs
    ``openclaw plugins install <install_spec>`` at build time. The
    plugin lands at ``$OPENCLAW_HOME/extensions/<install_plugin_id>``
    inside the image (see Dockerfile.eval) and entrypoint.sh injects
    that path into ``plugins.load.paths`` of the rendered openclaw
    config at runtime. ``memory_plugin`` is still the slot id (must
    match ``install_plugin_id`` when slot is the installed plugin).

    ``extra_install_specs`` is a list of (spec, plugin_id) pairs for
    auxiliary plugins (e.g. context-engine plugins that pair with the
    primary memory plugin). They are installed in the same build step
    and the entrypoint adds their extension dirs to
    ``plugins.load.paths`` via EXTRA_INSTALL_PLUGIN_IDS env.

    ``tag_override`` lets the new caller pass in a tag computed from
    BOTH plugin slots together. The legacy branch below derives a
    single-plugin tag and would otherwise mislabel two-plugin builds.
    """
    if plugins_dir is not None:
        stage_active_sidecar(eval_dir, plugins_dir, memory_plugin)
    if tag_override:
        tag = tag_override
    elif install_spec:
        # legacy install-mode: rev derived from spec only
        tag_extra = f"-x{len(extra_install_specs or [])}" if extra_install_specs else ""
        tag = f"openclaw-eval:{openclaw_sha}-install-{install_plugin_id}{tag_extra}-{plugin_rev}-{variant}"
    else:
        tag = f"openclaw-eval:{openclaw_sha}-{memory_plugin}-{plugin_rev}-{variant}"
    cmd = [
        "docker", "build",
        "-f", str(eval_dir / "Dockerfile.eval"),
        "--build-arg", f"BASE_IMAGE={base_tag}",
        "--build-arg", f"MEMORY_PLUGIN={memory_plugin}",
        "--build-arg", f"OPENCLAW_COMMIT={openclaw_sha}",
        "--build-arg", f"PLUGIN_REV={plugin_rev}",
    ]
    if install_spec:
        cmd += [
            "--build-arg", f"INSTALL_SPEC={install_spec}",
            "--build-arg", f"INSTALL_PLUGIN_ID={install_plugin_id}",
        ]
        if extra_install_specs:
            specs_str = " ".join(spec for spec, _id in extra_install_specs)
            ids_str = " ".join(pid for _spec, pid in extra_install_specs)
            cmd += [
                "--build-arg", f"EXTRA_INSTALL_SPECS={specs_str}",
                "--build-arg", f"EXTRA_INSTALL_PLUGIN_IDS={ids_str}",
            ]
    cmd += ["-t", tag, str(eval_dir)]
    run_step(f"Step 2: openclaw-eval ({memory_plugin})", cmd)
    return tag


# ---- Plugin-resolution helpers -------------------------------------------

def _tag_segment(ref: Optional[PluginRef]) -> Optional[str]:
    """One plugin's contribution to an image tag, or None if it doesn't appear.

    base-extension plugins (memory-core / noop) contribute nothing because
    they're already baked into openclaw-base. Bundled-source contributes
    its id; npm contributes "install-<id>" (preserves legacy tag shape).
    """
    if ref is None or ref.entry.type == "base-extension":
        return None
    if ref.entry.type == "bundled-source":
        return ref.id
    if ref.entry.type == "npm":
        return f"install-{ref.id}"
    return None


def derive_eval_tag(
    memory_ref: Optional[PluginRef],
    ce_ref: Optional[PluginRef],
    openclaw_sha: str,
    plugin_rev: str,
    variant: str,
) -> str:
    """Tag of the openclaw-eval image. Preserves legacy shapes:

    - both refs None / base-extension -> ``<sha>-memory-core-<rev>-<variant>``
    - one bundled-source memory plugin -> ``<sha>-<id>-<rev>-<variant>``
    - one npm context-engine -> ``<sha>-install-<id>-<rev>-<variant>``
    - both -> ``<sha>-<mid>_<cid_segment>-<rev>-<variant>`` (memory first)
    """
    segments = [s for s in (_tag_segment(memory_ref), _tag_segment(ce_ref)) if s]
    bundle = "_".join(segments) if segments else "memory-core"
    return f"openclaw-eval:{openclaw_sha}-{bundle}-{plugin_rev}-{variant}"


def collect_bundled_to_stage(
    memory_ref: Optional[PluginRef],
    ce_ref: Optional[PluginRef],
) -> list[str]:
    """Bundled-source plugin ids that need staging into the openclaw repo."""
    return [
        ref.id for ref in (memory_ref, ce_ref)
        if ref and ref.entry.type == "bundled-source"
    ]


def collect_npm_installs(
    memory_ref: Optional[PluginRef],
    ce_ref: Optional[PluginRef],
    overrides: dict[str, str],
) -> list[tuple[str, str]]:
    """npm specs (with plugin id) to install at eval-layer build time."""
    out: list[tuple[str, str]] = []
    for ref in (memory_ref, ce_ref):
        if ref and ref.entry.type == "npm":
            spec = overrides.get(ref.id) or ref.npm_spec()
            out.append((spec, ref.id))
    return out


def compute_plugin_rev(
    memory_ref: Optional[PluginRef],
    ce_ref: Optional[PluginRef],
    plugins_dir: Path,
    overrides: dict[str, str],
) -> str:
    """Combine each plugin's content/spec hash into one 7-char rev.

    - bundled-source: file-content hash from openclaw-eval/plugins/<id>/
    - npm: spec hash (so different versions => different image tags)
    - base-extension: contributes nothing
    Single contribution -> use as-is. Multiple -> sha256(joined).

    For npm specs we hash the BARE spec (without ``npm:`` prefix) on
    purpose: legacy --install-spec callers (e.g. smoke_stage3.sh) passed
    the spec without the prefix; Dockerfile.eval strips it before
    ``npm pack`` either way. Keeping the bare-spec hash here means
    existing image tags (``...-install-hypercompositor-5bace1f-slim``)
    are reachable from the new CLI without operators rebuilding.
    Pinned by ``test_compute_plugin_rev_single_npm_matches_legacy_hash``.
    """
    contributions: list[str] = []
    for ref in (memory_ref, ce_ref):
        if ref is None:
            continue
        if ref.entry.type == "bundled-source":
            contributions.append(plugin_content_hash(plugins_dir / ref.id))
        elif ref.entry.type == "npm":
            spec = overrides.get(ref.id) or ref.npm_spec()
            # Match legacy hash: the old --install-spec callers (e.g.
            # smoke_stage3.sh) passed the spec without the "npm:" prefix
            # since Dockerfile.eval strips it anyway. Hash on the bare
            # spec so existing images remain reachable by their tags.
            spec_for_hash = spec[len("npm:"):] if spec.startswith("npm:") else spec
            contributions.append(install_spec_hash(spec_for_hash))
    if not contributions:
        return "0000000"
    if len(contributions) == 1:
        return contributions[0]
    combined = ":".join(sorted(contributions))
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:7]


def _kind_for_manifest(ref: PluginRef) -> str:
    """Manifest stores a single kind string per plugin even if dual-kind."""
    if "memory" in ref.entry.kinds and "context-engine" in ref.entry.kinds:
        return "memory"  # arbitrary canonical for dual-kind
    return next(iter(ref.entry.kinds))


def build_manifest_plugins(
    memory_ref: Optional[PluginRef],
    ce_ref: Optional[PluginRef],
    plugins_dir: Path,
) -> dict[str, ManifestPlugin]:
    """ManifestPlugin records for everything actually present in the image.

    memory-core is always there (baked into openclaw-base) so it's
    always recorded. Other plugins added per ref kind/type.
    """
    plugins: dict[str, ManifestPlugin] = {
        "memory-core": ManifestPlugin(
            id="memory-core",
            kind="memory",
            version="bundled",
            rev=None,
            source="bundled",
        ),
    }
    for ref in (memory_ref, ce_ref):
        if ref is None or ref.entry.type == "base-extension":
            continue
        if ref.entry.type == "bundled-source":
            plugins[ref.id] = ManifestPlugin(
                id=ref.id,
                kind=_kind_for_manifest(ref),
                version="bundled",
                rev=plugin_content_hash(plugins_dir / ref.id),
                source="bundled-source",
            )
        elif ref.entry.type == "npm":
            plugins[ref.id] = ManifestPlugin(
                id=ref.id,
                kind=_kind_for_manifest(ref),
                version=ref.version or "",
                rev=None,
                source=f"npm:{ref.entry.npm_package}",
            )
    return plugins


def _version_from_npm_spec(spec: str) -> Optional[str]:
    """Extract the version trailer from ``npm:[@scope/]name@version``."""
    s = spec.strip()
    if s.startswith("npm:"):
        s = s[len("npm:"):]
    if s.startswith("@"):
        slash = s.find("/")
        if slash < 0:
            return None
        s = s[slash + 1:]
    if "@" in s:
        return s.rpartition("@")[2] or None
    return None


def _migrate_one_spec(
    spec: str,
    explicit_id: Optional[str],
    registry: dict[str, PluginEntry],
) -> tuple[str, str]:
    """Resolve a single legacy install spec into (plugin_id, version).

    Mutates only its arguments. Calls ``sys.exit`` on unrecoverable
    errors (preserving the old wording where possible so legacy
    operator scripts see familiar diagnostics).
    """
    derived_id = derive_plugin_id_from_spec(spec)
    plugin_id = explicit_id or derived_id
    if not plugin_id:
        sys.exit(
            f"[build] ERROR: --install-spec '{spec}' could not "
            f"derive a plugin id; pass --install-plugin-id explicitly."
        )
    if plugin_id in ("memory-core", "noop"):
        sys.exit(
            f"[build] ERROR: --install-spec is not supported for "
            f"'{plugin_id}' (bundled plugin, not an install target)."
        )
    if plugin_id not in registry:
        sys.exit(
            f"[build] ERROR: legacy --install-spec maps to unknown plugin "
            f"id '{plugin_id}'. Add it to evaluation/config/plugin_registry.yaml "
            f"first (set kind to memory or context-engine and type=npm), "
            f"or migrate to the new --memory-plugin / --context-engine flags."
        )
    version = _version_from_npm_spec(spec)
    if not version:
        sys.exit(
            f"[build] ERROR: legacy --install-spec '{spec}' has no extractable "
            f"version. New CLI requires --memory-plugin <id>@<version> or "
            f"--context-engine <id>@<version>. For tarballs / clawhub specs, "
            f"add an entry to plugin_registry.yaml and use --plugin-spec "
            f"<id>=<spec>."
        )
    return plugin_id, version


def migrate_old_install_args(
    args: argparse.Namespace,
    registry: dict[str, PluginEntry],
) -> tuple[Optional[str], Optional[str], dict[str, str]]:
    """Map deprecated --install-spec / --install-plugin-id / --extra-* onto
    new --memory-plugin / --context-engine values.

    Returns ``(memory_arg, ce_arg, extra_overrides)``. ``extra_overrides``
    is a dict feeding back into ``--plugin-spec`` so the original spec
    string (especially for clawhub: / marketplace: / tarball forms) is
    preserved through to ``Dockerfile.eval``.

    Old --extra-install-spec maps to whichever slot --install-spec did
    NOT take. If the old user had both, both slots get filled.
    """
    warnings.warn(
        "--install-spec / --install-plugin-id / --extra-install-spec are "
        "deprecated. Use --memory-plugin <id>@<v> / --context-engine <id>@<v>. "
        "See evaluation/config/plugin_registry.yaml.",
        DeprecationWarning,
        stacklevel=2,
    )

    primary_id, primary_version = _migrate_one_spec(
        args.install_spec, args.install_plugin_id, registry,
    )

    # Decide primary slot from registry kind. (Plugin must be registered;
    # _migrate_one_spec already exited if not.)
    primary_kinds = registry[primary_id].kinds
    primary_is_memory = "memory" in primary_kinds
    primary_is_ce = "context-engine" in primary_kinds

    extra_overrides: dict[str, str] = {}
    # Preserve the original spec verbatim so clawhub/tarball/private-registry
    # forms aren't lost. Only override when the spec deviates from what the
    # registry would synthesize (npm:<package>@<version>).
    canonical = f"npm:{registry[primary_id].npm_package}@{primary_version}" \
        if registry[primary_id].npm_package else None
    if args.install_spec.strip() != (canonical or ""):
        extra_overrides[primary_id] = args.install_spec

    if primary_is_memory and not primary_is_ce:
        memory_arg = f"{primary_id}@{primary_version}"
        if args.memory_plugin not in ("memory-core", "noop", None) \
                and args.memory_plugin != primary_id:
            sys.exit(
                f"[build] ERROR: --memory-plugin '{args.memory_plugin}' must "
                f"match installed plugin id '{primary_id}' when --install-spec "
                f"is a memory plugin. Re-run with --memory-plugin {primary_id} "
                f"or --memory-plugin memory-core for context-engine plugins."
            )
        ce_arg = None
    else:
        memory_arg = None
        ce_arg = f"{primary_id}@{primary_version}"

    # Process --extra-install-spec into the OTHER slot.
    if args.extra_install_spec:
        if len(args.extra_install_spec) > 1:
            sys.exit(
                "[build] ERROR: legacy shim accepts at most one "
                "--extra-install-spec (paired with --install-spec). Multiple "
                "extras require migration to the new CLI: --memory-plugin "
                "<a>@<v> --context-engine <b>@<v>."
            )
        extra_spec = args.extra_install_spec[0]
        explicit_extra_id = (
            args.extra_install_plugin_id[0]
            if args.extra_install_plugin_id else None
        )
        extra_id, extra_version = _migrate_one_spec(
            extra_spec, explicit_extra_id, registry,
        )
        extra_kinds = registry[extra_id].kinds
        extra_is_memory = "memory" in extra_kinds
        extra_is_ce = "context-engine" in extra_kinds

        if memory_arg is None and extra_is_memory:
            memory_arg = f"{extra_id}@{extra_version}"
        elif ce_arg is None and extra_is_ce:
            ce_arg = f"{extra_id}@{extra_version}"
        else:
            sys.exit(
                f"[build] ERROR: --extra-install-spec '{extra_spec}' resolves "
                f"to plugin '{extra_id}' (kinds={sorted(extra_kinds)}) which "
                f"can't fill the slot left by --install-spec '{args.install_spec}'. "
                f"Migrate to --memory-plugin / --context-engine directly."
            )

        extra_canonical = (
            f"npm:{registry[extra_id].npm_package}@{extra_version}"
            if registry[extra_id].npm_package else None
        )
        if extra_spec.strip() != (extra_canonical or ""):
            extra_overrides[extra_id] = extra_spec

    return memory_arg, ce_arg, extra_overrides


_NEW_CLI_DESCRIPTION = (
    "openclaw-eval image builder. Plugin selection uses the same "
    "<id>[@<version>] syntax as evaluation.cli; the registry at "
    "evaluation/config/plugin_registry.yaml maps each id to bundled / "
    "npm sources. After a successful build, an entry is appended to "
    "evaluation/config/image_manifest.yaml so eval CLI can resolve "
    "--memory-plugin / --context-engine to a concrete image tag."
)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    # New plugin-selection flags.
    parser.add_argument(
        "--memory-plugin",
        default=None,
        help=(
            "Memory plugin: <id>, <id>@<version>, or 'none' (default). "
            "Bundled ids: memory-core, noop, evermemos, mem0, stub. "
            "npm ids require @<version> (no implicit 'latest')."
        ),
    )
    parser.add_argument(
        "--context-engine",
        default=None,
        help=(
            "Context-engine plugin: same syntax as --memory-plugin. "
            "When omitted, image relies on openclaw's built-in 'legacy' "
            "engine (no extra plugin install)."
        ),
    )
    parser.add_argument(
        "--plugin-spec",
        action="append",
        default=[],
        metavar="ID=SPEC",
        help=(
            "Override the registry's npm package for one plugin id. "
            "Useful for testing forks / private packages / tarballs. "
            "Repeatable. Example: "
            "--plugin-spec my-fork=npm:@scope/foo@1.0"
        ),
    )

    # Build-environment flags (unchanged from old CLI).
    parser.add_argument(
        "--openclaw-repo",
        default="/Data3/shutong.shan/openclaw/repo",
        help="path to openclaw repo",
    )
    parser.add_argument(
        "--variant", default="slim", choices=["slim", "default"]
    )
    parser.add_argument(
        "--rebuild-base", action="store_true",
        help="force rebuild base image even if cached",
    )
    parser.add_argument(
        "--no-image-manifest", action="store_true",
        help="skip appending an entry to image_manifest.yaml",
    )
    parser.add_argument(
        "--image-manifest-out",
        default=None,
        help=(
            "path to image_manifest.yaml (default: "
            f"{DEFAULT_MANIFEST_PATH})"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve plugins + print plan, don't run docker.",
    )

    # Deprecated flags (still parsed; mapped to new with DeprecationWarning).
    parser.add_argument(
        "--install-spec", default=None,
        help="DEPRECATED. Use --memory-plugin / --context-engine <id>@<v>.",
    )
    parser.add_argument(
        "--install-plugin-id", default=None,
        help="DEPRECATED. Plugin id is now derived from registry.",
    )
    parser.add_argument(
        "--extra-install-spec", action="append", default=[], metavar="SPEC",
        help="DEPRECATED. Use --memory-plugin <a> --context-engine <b>.",
    )
    parser.add_argument(
        "--extra-install-plugin-id", action="append", default=[], metavar="ID",
        help="DEPRECATED. (was paired with --extra-install-spec)",
    )
    parser.add_argument(
        "--manifest-out", default=None,
        help="DEPRECATED build-summary JSON path. Use --image-manifest-out.",
    )


def main():
    parser = argparse.ArgumentParser(description=_NEW_CLI_DESCRIPTION)
    _add_arguments(parser)
    args = parser.parse_args()

    registry = load_registry(DEFAULT_REGISTRY_PATH)

    # Map deprecated --install-spec onto new --memory-plugin / --context-engine.
    # Conflict only when the *bare* plugin id differs (legacy callers passed
    # --memory-plugin <id> without a version; the migrated form is <id>@<v>
    # — same id, more specific).
    shim_overrides: dict[str, str] = {}
    if args.install_spec:
        migrated_memory, migrated_ce, shim_overrides = migrate_old_install_args(
            args, registry,
        )

        def _bare(arg):
            return arg.split("@", 1)[0] if arg else None

        if migrated_memory and args.memory_plugin \
                and _bare(args.memory_plugin) != _bare(migrated_memory):
            sys.exit(
                "[build] ERROR: --install-spec migrated to "
                f"--memory-plugin {migrated_memory!r} but --memory-plugin "
                f"already set to {args.memory_plugin!r}. Drop one."
            )
        if migrated_ce and args.context_engine \
                and _bare(args.context_engine) != _bare(migrated_ce):
            sys.exit(
                "[build] ERROR: --install-spec migrated to "
                f"--context-engine {migrated_ce!r} but --context-engine "
                f"already set to {args.context_engine!r}. Drop one."
            )
        args.memory_plugin = migrated_memory or args.memory_plugin
        args.context_engine = migrated_ce or args.context_engine

    try:
        overrides = parse_plugin_spec_overrides(args.plugin_spec)
        # Shim-derived overrides preserve the original tarball / clawhub /
        # private-registry spec strings so they pass through to Dockerfile.eval.
        for ovr_id, ovr_spec in shim_overrides.items():
            if ovr_id in overrides and overrides[ovr_id] != ovr_spec:
                sys.exit(
                    f"[build] ERROR: --plugin-spec {ovr_id}=... conflicts with "
                    f"the spec extracted from --install-spec ({ovr_spec!r}). "
                    f"Drop one."
                )
            overrides.setdefault(ovr_id, ovr_spec)
        memory_ref = parse_ref(
            args.memory_plugin, expected_kind="memory", registry=registry,
        )
        ce_ref = parse_ref(
            args.context_engine, expected_kind="context-engine", registry=registry,
        )
    except ResolverError as e:
        sys.exit(f"[build] ERROR: {e}")

    selected_ids = {ref.id for ref in (memory_ref, ce_ref) if ref}
    for ovr_id in overrides:
        if ovr_id not in selected_ids:
            sys.exit(
                f"[build] ERROR: --plugin-spec '{ovr_id}=...' is not a "
                f"selected plugin (selected: {sorted(selected_ids) or 'none'})"
            )

    here = Path(__file__).resolve().parents[1]
    plugins_dir = here / "plugins"

    # When --dry-run is set we just resolve plugins and print the plan.
    # Defer docker / openclaw-repo / Dockerfile.eval checks so dry-run
    # works on any machine even without docker installed or the openclaw
    # checkout present.
    if args.dry_run:
        # Best-effort sha; fall back to a sentinel when openclaw repo is
        # absent so the plan still prints.
        openclaw_repo = Path(args.openclaw_repo).resolve()
        if (openclaw_repo / ".git").exists():
            openclaw_sha = short_sha(openclaw_repo)
        else:
            openclaw_sha = "0000000"
    else:
        if shutil.which("docker") is None:
            sys.exit("[build] ERROR: docker not in PATH")
        openclaw_repo = Path(args.openclaw_repo).resolve()
        if not (openclaw_repo / "Dockerfile").exists():
            sys.exit(f"[build] ERROR: {openclaw_repo}/Dockerfile not found")
        if not (here / "Dockerfile.eval").exists():
            sys.exit(f"[build] ERROR: {here}/Dockerfile.eval not found")
        openclaw_sha = short_sha(openclaw_repo)

    plugin_rev = compute_plugin_rev(memory_ref, ce_ref, plugins_dir, overrides)
    eval_tag_target = derive_eval_tag(
        memory_ref, ce_ref, openclaw_sha, plugin_rev, args.variant
    )
    bundled_to_stage = collect_bundled_to_stage(memory_ref, ce_ref)
    npm_installs = collect_npm_installs(memory_ref, ce_ref, overrides)

    # Build-plan summary.
    print(f"[build] openclaw_sha   = {openclaw_sha}")
    print(f"[build] memory_plugin  = {args.memory_plugin or 'none'}")
    print(f"[build] context_engine = {args.context_engine or 'none'}")
    print(f"[build] plugin_rev     = {plugin_rev}")
    print(f"[build] eval_tag       = {eval_tag_target}")
    print(f"[build] bundled_stage  = {bundled_to_stage}")
    print(f"[build] npm_installs   = {[s for s, _ in npm_installs]}")

    if args.dry_run:
        print("[build] dry-run: skipping docker build")
        return

    # Step 1: openclaw-base. Stage all bundled-source plugins; OPENCLAW_EXTENSIONS
    # always includes 'memory-core' baseline plus each staged plugin id.
    if bundled_to_stage:
        primary_bundled = bundled_to_stage[0]
        extra_bundled = bundled_to_stage[1:]
    else:
        primary_bundled = "memory-core"
        extra_bundled = []
    base_tag = build_base(
        openclaw_repo,
        primary_bundled,
        openclaw_sha,
        variant=args.variant,
        skip_if_exists=not args.rebuild_base,
        plugins_dir=plugins_dir,
        extra_bundled_plugins=extra_bundled,
    )

    # Step 2: openclaw-eval. Use first npm install as primary INSTALL_SPEC,
    # remaining as EXTRA_INSTALL_SPECS (Dockerfile.eval handles both).
    if npm_installs:
        primary_install_spec, primary_install_id = npm_installs[0]
        extra_install_specs = npm_installs[1:]
    else:
        primary_install_spec = None
        primary_install_id = None
        extra_install_specs = None

    # build_eval_layer derives its own tag string from the args we pass.
    # We pass primary_bundled as ``memory_plugin`` (it controls the legacy
    # tag-shape branches in build_eval_layer) but for the new shape we let
    # the function derive it from install_spec when primary_install_spec
    # is set; otherwise the legacy bundled-source path produces the same
    # tag derive_eval_tag computes here.
    layer_tag = build_eval_layer(
        here,
        base_tag,
        primary_bundled,
        openclaw_sha,
        plugin_rev,
        variant=args.variant,
        plugins_dir=plugins_dir,
        install_spec=primary_install_spec,
        install_plugin_id=primary_install_id,
        extra_install_specs=extra_install_specs or None,
        tag_override=eval_tag_target,
    )

    print()
    print("[build] DONE")
    print(f"[build]   base_tag:  {base_tag}")
    print(f"[build]   layer_tag: {layer_tag}")

    # Append entry to image_manifest.yaml unless explicitly disabled.
    # If this fails AFTER docker succeeded, the image exists but is not
    # discoverable by evaluation.cli; surface the error loudly so the
    # operator can append by hand or rerun build.py with --rebuild-base
    # to retry. We exit non-zero to make the failure obvious in CI.
    if not args.no_image_manifest:
        manifest_path = (
            Path(args.image_manifest_out)
            if args.image_manifest_out
            else DEFAULT_MANIFEST_PATH
        )
        entry = ManifestEntry(
            image=layer_tag,
            openclaw_sha=openclaw_sha,
            built_at=now_iso(),
            plugins=build_manifest_plugins(memory_ref, ce_ref, plugins_dir),
        )
        try:
            append_entry(manifest_path, entry)
        except OSError as e:
            sys.exit(
                f"[build] WARN: image build succeeded ({layer_tag}) but "
                f"manifest append failed: {e}\n"
                f"[build] Image is usable; either add the entry manually or "
                f"rerun with --rebuild-base after fixing the manifest path."
            )
        print(f"[build]   image_manifest: {manifest_path}")

    # Deprecated --manifest-out: per-build JSON summary, kept for backward compat.
    if args.manifest_out:
        warnings.warn(
            "--manifest-out is deprecated (per-build JSON summary). "
            "image_manifest.yaml replaces it; use --no-image-manifest to skip.",
            DeprecationWarning,
            stacklevel=2,
        )
        summary = {
            "openclaw_sha": openclaw_sha,
            "memory_plugin": args.memory_plugin,
            "context_engine": args.context_engine,
            "plugin_rev": plugin_rev,
            "variant": args.variant,
            "base_tag": base_tag,
            "eval_tag": layer_tag,
        }
        Path(args.manifest_out).write_text(json.dumps(summary, indent=2))
        print(f"[build]   manifest: {args.manifest_out}")


if __name__ == "__main__":
    main()
