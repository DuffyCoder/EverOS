#!/usr/bin/env python3
"""
Stage 1 image build orchestrator.

Two-step build:
  1. openclaw-base:<sha>-<plugin>-slim    (from openclaw repo Dockerfile)
  2. openclaw-eval:<sha>-<plugin>-slim    (from openclaw-eval/Dockerfile.eval)

Usage:
    python -m openclaw_eval.harness.build \\
        --memory-plugin memory-core \\
        --openclaw-repo /Data3/shutong.shan/openclaw/repo

Outputs:
    Logs the two image tags. Exit non-zero if any step fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def short_sha(path: Path, ref: str = "HEAD") -> str:
    res = subprocess.run(
        ["git", "rev-parse", "--short=7", ref],
        cwd=path, capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _hash_files_to_rev(named_files: list[tuple[str, Path]]) -> str:
    """Hash a list of (logical_name, file_path) into a 7-char rev.

    Missing files are skipped silently so eval_base_rev() returns a stable
    rev even on partially-empty trees. Used by both content-derived plugin
    revs and framework-derived eval-base revs.
    """
    h = hashlib.sha256()
    for name, path in named_files:
        if not path.is_file():
            continue
        h.update(name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:7]


def plugin_content_hash(plugin_dir: Path) -> str:
    """Hash of plugin source files for tag reproducibility.

    Skips dot-prefixed entries (relative to ``plugin_dir``) and the standard
    build/dep dirs. Earlier version inspected ``p.parts`` of the absolute
    path, which always rejected files when the plugin lived under
    ``.claude/worktrees/...`` — every file got filtered, leaving the empty
    sha256 prefix ``e3b0c44`` as the rev for any plugin in a worktree.
    """
    if not plugin_dir.exists():
        return "0000000"
    skip_names = {"node_modules", "dist"}
    named: list[tuple[str, Path]] = []
    for p in sorted(plugin_dir.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(plugin_dir).parts
        if any(part.startswith(".") or part in skip_names for part in rel_parts):
            continue
        named.append(("/".join(rel_parts), p))
    return _hash_files_to_rev(named)


def docker_image_exists(tag: str) -> bool:
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return res.returncode == 0


def eval_layer_tag(
    *,
    openclaw_sha: str,
    memory_plugin: str,
    plugin_rev: str,
    variant: str,
    install_plugin_id: Optional[str] = None,
    extra_count: int = 0,
) -> str:
    """Compose the openclaw-eval image tag. Single source of truth so the
    legacy full-build and split-build paths can't drift apart.

    Tag shape:
      install-mode: openclaw-eval:<sha>-install-<id>[-x<n>]-<rev>-<variant>
      bundled:      openclaw-eval:<sha>-<plugin>-<rev>-<variant>
    """
    if install_plugin_id:
        suffix = f"-x{extra_count}" if extra_count else ""
        return f"openclaw-eval:{openclaw_sha}-install-{install_plugin_id}{suffix}-{plugin_rev}-{variant}"
    return f"openclaw-eval:{openclaw_sha}-{memory_plugin}-{plugin_rev}-{variant}"


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


# Eval-layer minimal-prune contract. The upstream openclaw runtime image
# bundles 101 unused extension dist outputs (~210M), 53 skills, 14M of docs,
# and 6 large node_modules packages tied to extensions we never enable.
# Dockerfile.eval prunes them in a single RUN layer so the eval image stays
# under ~700M instead of ~1.5G. See discussion in the 2026-05-06 session.

# Packages whose only known consumers are extensions we explicitly do not
# enable (memory-lancedb, nano-pdf-style PDF tools, tlon channel, feishu,
# diagnostics-otel, and node-llama-cpp's local LLM provider — memory-core
# treats node-llama-cpp as ERR_MODULE_NOT_FOUND-tolerant optional). Never
# add koffi / @napi-rs / rolldown / oxlint / typescript here without an
# explicit smoke gate — they're hoisted across many transitive deps.
PRUNE_NODE_MODULES_PACKAGES: tuple[str, ...] = (
    "@lancedb",
    "pdfjs-dist",
    "@tloncorp",
    "node-llama-cpp",
    "@node-llama-cpp",
    "@larksuiteoapi",
    "@opentelemetry",
)

# Bundled plugins openclaw's bootstrap path imports unconditionally as
# "public surfaces" (e.g. speech-core/runtime-api.js, llm-task/runtime-api.js).
# Removing these breaks `agent --local` workspace prebootstrap with
# "Unable to resolve bundled plugin public surface <id>/runtime-api.js"
# even when plugins.allow doesn't list them. Discovered empirically while
# pruning the hypercompositor image (2026-05-06).
#
# These are kept on top of whatever the caller explicitly asks for. They
# are tiny (~140KB total compiled) so the overhead is negligible.
BOOTSTRAP_KEEP_EXTENSIONS: tuple[str, ...] = (
    "speech-core",
    "image-generation-core",
    "video-generation-core",
    "media-understanding-core",
    "llm-task",
)


def compute_keep_extensions(
    memory_plugin: str,
    install_plugin_id: Optional[str],
    extra_install_plugin_ids: Optional[list[str]],
) -> list[str]:
    """Return the sorted-unique extension whitelist for KEEP_EXTENSIONS.

    memory-core is always kept — even in noop mode the entrypoint binds
    plugins.slots.memory to "memory-core" (memorySearch is just disabled
    via the agents.defaults flag), so the extension dir must remain
    loadable. Other slots (memory_plugin when external, primary install
    id, paired context-engine ids) are added on top.

    Sorting + dedup makes the docker --build-arg value stable so layer
    caching isn't perturbed by argument order.
    """
    keep: set[str] = {"memory-core"}
    keep.update(BOOTSTRAP_KEEP_EXTENSIONS)
    if memory_plugin and memory_plugin not in ("memory-core", "noop"):
        keep.add(memory_plugin)
    if install_plugin_id:
        keep.add(install_plugin_id)
    for pid in extra_install_plugin_ids or []:
        if pid:
            keep.add(pid)
    return sorted(keep)


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
) -> str:
    """Step 1: build openclaw-base with OPENCLAW_EXTENSIONS opt-in."""
    tag = f"openclaw-base:{openclaw_sha}-{memory_plugin}-{variant}"
    if skip_if_exists and docker_image_exists(tag):
        print(f"[build] base image {tag} already exists; skipping rebuild")
        return tag

    extensions = "memory-core"
    if memory_plugin not in ("memory-core", "noop"):
        if plugins_dir is None:
            raise SystemExit(
                f"[build] ERROR: external plugin '{memory_plugin}' requires plugins_dir"
            )
        stage_external_plugin(plugins_dir, memory_plugin, openclaw_repo)
        # External plugin still needs memory-core in the image (used as fallback
        # baseline for the harness comparison).
        extensions = f"memory-core {memory_plugin}"

    cmd = [
        "docker", "build",
        "--build-arg", f"OPENCLAW_EXTENSIONS={extensions}",
        "--build-arg", f"OPENCLAW_VARIANT={variant}",
        "-t", tag,
        ".",
    ]
    run_step(f"Step 1: openclaw-base ({memory_plugin}, {variant})", cmd, cwd=openclaw_repo)
    return tag


def eval_base_rev(eval_dir: Path) -> str:
    """Stable 7-char hash of eval-base inputs (Dockerfile + bridge + entrypoint).

    Lets the eval-base image tag track framework changes (bridge.mjs edits,
    minimal-prune rule tweaks) even when the underlying OpenClaw sha stays
    the same. Hashed inputs are exactly what Dockerfile.eval-base COPYs in
    plus its own contents.
    """
    paths = [
        eval_dir / "Dockerfile.eval-base",
        eval_dir / "container" / "openclaw.template.json",
        eval_dir / "container" / "entrypoint.sh",
        eval_dir / "container" / "openclaw_eval_bridge.mjs",
        eval_dir / "container" / "openclaw_eval_bridge_lib.mjs",
    ]
    return _hash_files_to_rev([(p.name, p) for p in paths])


def build_eval_base(
    eval_dir: Path,
    base_tag: str,
    openclaw_sha: str,
    *,
    variant: str = "slim",
    skip_if_exists: bool = True,
    minimal_prune: bool = True,
) -> str:
    """Build the plugin-agnostic eval-base layer (push target).

    Stacks on ``openclaw-base:<sha>-memory-core-<variant>`` (always
    memory-core — the eval-base whitelist must not depend on which
    downstream plugin will be installed). Tag includes both the OpenClaw
    sha and an eval_base_rev so framework-level bridge / minimal-prune
    changes invalidate the cache without piggybacking on OpenClaw bumps.
    """
    rev = eval_base_rev(eval_dir)
    tag = f"openclaw-eval-base:{openclaw_sha}-clean-{rev}-{variant}"
    if skip_if_exists and docker_image_exists(tag):
        print(f"[build] eval-base image {tag} already exists; skipping rebuild")
        return tag

    cmd = [
        "docker", "build",
        "-f", str(eval_dir / "Dockerfile.eval-base"),
        "--build-arg", f"BASE_IMAGE={base_tag}",
        "--build-arg", f"OPENCLAW_COMMIT={openclaw_sha}",
        "--build-arg", f"EVAL_BASE_REV={rev}",
    ]
    if minimal_prune:
        # eval-base whitelist: memory-core + bootstrap public surfaces
        # only. Plugin layer installs land under /opt/openclaw/extensions/
        # which prune never touches, so the whitelist is plugin-agnostic.
        keep = compute_keep_extensions("memory-core", None, None)
        cmd += [
            "--build-arg", f"KEEP_EXTENSIONS={' '.join(keep)}",
            "--build-arg",
            f"PRUNE_NODE_MODULES_PACKAGES={' '.join(PRUNE_NODE_MODULES_PACKAGES)}",
        ]
    cmd += ["-t", tag, str(eval_dir)]
    run_step("eval-base (clean, plugin-agnostic)", cmd)
    return tag


def push_eval_base(local_tag: str, registry: str) -> str:
    """Retag the local eval-base under <registry>/<image>:<tag> and push.

    No-op if the registry-qualified tag already exists locally with the
    same image id (idempotent re-push).
    """
    image_part = local_tag.split(":", 1)
    if len(image_part) != 2:
        raise SystemExit(f"[build] ERROR: malformed eval-base tag: {local_tag}")
    name, version = image_part
    remote_tag = f"{registry.rstrip('/')}/{name}:{version}"
    run_step(f"retag {local_tag} -> {remote_tag}", ["docker", "tag", local_tag, remote_tag])
    run_step(f"push {remote_tag}", ["docker", "push", remote_tag])
    return remote_tag


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


def build_eval_plugin_layer(
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
) -> str:
    """Plugin-only layer on top of an already-built eval-base.

    Used by the split-build pipeline (--eval-base-image). Skips the
    apt/prune/bridge steps that the eval-base layer already covers, so
    this build is small (sidecar pip install + npm pack) and fast on
    cached layers.
    """
    if plugins_dir is not None:
        stage_active_sidecar(eval_dir, plugins_dir, memory_plugin)
    tag = eval_layer_tag(
        openclaw_sha=openclaw_sha,
        memory_plugin=memory_plugin,
        plugin_rev=plugin_rev,
        variant=variant,
        install_plugin_id=install_plugin_id if install_spec else None,
        extra_count=len(extra_install_specs or []),
    )
    cmd = [
        "docker", "build",
        "-f", str(eval_dir / "Dockerfile.eval-plugin"),
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
    run_step(f"eval-plugin layer ({memory_plugin or install_plugin_id})", cmd)
    return tag


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
    minimal_prune: bool = True,
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
    """
    if plugins_dir is not None:
        stage_active_sidecar(eval_dir, plugins_dir, memory_plugin)
    tag = eval_layer_tag(
        openclaw_sha=openclaw_sha,
        memory_plugin=memory_plugin,
        plugin_rev=plugin_rev,
        variant=variant,
        install_plugin_id=install_plugin_id if install_spec else None,
        extra_count=len(extra_install_specs or []),
    )
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
    if minimal_prune:
        extra_ids = [pid for _spec, pid in (extra_install_specs or [])]
        keep = compute_keep_extensions(memory_plugin, install_plugin_id, extra_ids)
        cmd += [
            "--build-arg", f"KEEP_EXTENSIONS={' '.join(keep)}",
            "--build-arg",
            f"PRUNE_NODE_MODULES_PACKAGES={' '.join(PRUNE_NODE_MODULES_PACKAGES)}",
        ]
    cmd += ["-t", tag, str(eval_dir)]
    run_step(f"Step 2: openclaw-eval ({memory_plugin})", cmd)
    return tag


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-plugin", default="memory-core",
                        help="memory-core | noop | <external_plugin_id>")
    parser.add_argument("--openclaw-repo",
                        default="/Data3/shutong.shan/openclaw/repo",
                        help="path to openclaw repo")
    parser.add_argument("--variant", default="slim", choices=["slim", "default"])
    parser.add_argument("--rebuild-base", action="store_true",
                        help="force rebuild base image even if cached")
    parser.add_argument("--install-spec",
                        default=None,
                        help=("Install plugin via 'openclaw plugins install <spec>' "
                              "inside the eval-layer image instead of staging from "
                              "openclaw-eval/plugins/. Examples: "
                              "'npm:@mem0/openclaw-plugin@1.2.0', "
                              "'clawhub:owner/name', 'marketplace:foo'. When set, "
                              "--memory-plugin must equal the installed plugin id "
                              "(or the slot will not bind). See "
                              "docs/superpowers/specs/2026-04-30-plugin-kinds-design-note.md"))
    parser.add_argument("--install-plugin-id",
                        default=None,
                        help=("Plugin id used in plugins.allow / slots.memory + the "
                              "install destination directory name. Required with "
                              "--install-spec when the spec doesn't reveal the id "
                              "(e.g. raw paths). For npm/clawhub/marketplace specs "
                              "this is auto-derived if omitted."))
    parser.add_argument(
        "--extra-install-spec",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "Auxiliary plugin to install alongside the primary --install-spec. "
            "Repeatable. Each is run via 'openclaw plugins install' and its "
            "extension dir is added to plugins.load.paths at runtime. Use for "
            "context-engine plugins paired with a memory plugin (e.g. "
            "--install-spec npm:@psiclawops/hypermem@0.9.6 "
            "--extra-install-spec npm:@psiclawops/hypercompositor@0.9.6)."
        ),
    )
    parser.add_argument(
        "--extra-install-plugin-id",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "Plugin id for each --extra-install-spec, in matching order. "
            "Auto-derived from the spec when omitted (npm:@scope/name@v -> name)."
        ),
    )
    parser.add_argument("--manifest-out",
                        help="optional path to write build-manifest JSON")
    parser.add_argument(
        "--no-minimal-prune",
        action="store_true",
        help=(
            "Disable the minimal-prune layer (KEEP_EXTENSIONS + node_modules "
            "package removal). Use only for debugging upstream behavior — "
            "normal eval images strip ~830M of unused content by default."
        ),
    )
    parser.add_argument(
        "--build-eval-base",
        action="store_true",
        help=(
            "Split-build mode: build only the plugin-agnostic eval-base "
            "layer (Dockerfile.eval-base) and exit. Tag: "
            "openclaw-eval-base:<sha>-clean-<eval_base_rev>-<variant>. "
            "Combine with --push-eval-base to publish to the shared registry."
        ),
    )
    parser.add_argument(
        "--push-eval-base",
        action="store_true",
        help=(
            "After --build-eval-base, retag and `docker push` to "
            "<registry>/openclaw-eval-base:<tag>. Requires --registry."
        ),
    )
    parser.add_argument(
        "--registry",
        default=None,
        help=(
            "Container registry path (e.g. ghcr.io/duffycoder) used when "
            "--push-eval-base or --eval-base-image is a local tag to retag."
        ),
    )
    parser.add_argument(
        "--eval-base-image",
        default=None,
        help=(
            "Skip openclaw-base + eval-base build; use this image as the "
            "BASE_IMAGE for the eval-plugin layer (Dockerfile.eval-plugin). "
            "Typically a registry-qualified tag pulled from the shared "
            "registry, e.g. ghcr.io/duffycoder/openclaw-eval-base:"
            "7da23c3-clean-XXXXXXX-slim. Only valid with --install-spec."
        ),
    )
    args = parser.parse_args()

    if args.push_eval_base and not args.build_eval_base:
        print("[build] ERROR: --push-eval-base requires --build-eval-base", file=sys.stderr)
        sys.exit(1)
    if args.push_eval_base and not args.registry:
        print("[build] ERROR: --push-eval-base requires --registry", file=sys.stderr)
        sys.exit(1)
    if args.eval_base_image and args.build_eval_base:
        print(
            "[build] ERROR: --eval-base-image and --build-eval-base are mutually "
            "exclusive (the former skips base build; the latter only builds base)",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.eval_base_image and not args.install_spec:
        print(
            "[build] ERROR: --eval-base-image only supports install-spec mode "
            "(bundled plugins still need to be staged into openclaw-base)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate install-spec mode early.
    if args.install_spec:
        derived_id = derive_plugin_id_from_spec(args.install_spec)
        plugin_id = args.install_plugin_id or derived_id
        if not plugin_id:
            print(
                f"[build] ERROR: --install-spec '{args.install_spec}' could not "
                f"derive a plugin id; pass --install-plugin-id explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Pre-Stage-3 invariant: memory plugin slot had to match installed id
        # because install was assumed memory-only. Stage 3 onboards
        # context-engine plugins (kind: context-engine) which install via
        # the same --install-spec but bind to a different slot at runtime
        # via CONTEXT_ENGINE_PLUGIN_ID. Allow memory_plugin in {memory-core,
        # noop} regardless of installed id; only enforce the match when
        # memory_plugin is a non-bundled id (memory plugin install path).
        if args.memory_plugin not in ("memory-core", "noop") and args.memory_plugin != plugin_id:
            print(
                f"[build] ERROR: --memory-plugin '{args.memory_plugin}' must "
                f"match installed plugin id '{plugin_id}' when --install-spec "
                f"is a memory plugin. Re-run with --memory-plugin {plugin_id} "
                f"or --memory-plugin memory-core for context-engine plugins.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Disallow install-mode for memory-core / noop (they're bundled,
        # not installable specs).
        if plugin_id in ("memory-core", "noop"):
            print(
                f"[build] ERROR: --install-spec is not supported for "
                f"'{plugin_id}' (bundled plugin, not an install target).",
                file=sys.stderr,
            )
            sys.exit(1)
        # Stash resolved id back so downstream code uses it uniformly.
        args.install_plugin_id = plugin_id

    # Resolve extra install specs into (spec, plugin_id) pairs.
    extra_pairs: list[tuple[str, str]] = []
    if args.extra_install_spec:
        if not args.install_spec:
            print(
                "[build] ERROR: --extra-install-spec requires --install-spec "
                "(extras are auxiliary to a primary install).",
                file=sys.stderr,
            )
            sys.exit(1)
        explicit_ids = list(args.extra_install_plugin_id)
        for i, spec in enumerate(args.extra_install_spec):
            if i < len(explicit_ids):
                pid = explicit_ids[i]
            else:
                pid = derive_plugin_id_from_spec(spec)
            if not pid:
                print(
                    f"[build] ERROR: cannot derive plugin id from extra spec "
                    f"'{spec}'; pass --extra-install-plugin-id matching it.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if pid in ("memory-core", "noop"):
                print(
                    f"[build] ERROR: extra spec '{spec}' resolves to bundled "
                    f"plugin '{pid}'; bundled plugins aren't install-mode targets.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if pid == args.install_plugin_id:
                print(
                    f"[build] ERROR: extra spec '{spec}' duplicates primary "
                    f"plugin id '{pid}'.",
                    file=sys.stderr,
                )
                sys.exit(1)
            extra_pairs.append((spec, pid))
    args._extra_pairs = extra_pairs

    if shutil.which("docker") is None:
        print("[build] ERROR: docker not in PATH", file=sys.stderr)
        sys.exit(1)

    openclaw_repo = Path(args.openclaw_repo).resolve()
    if not (openclaw_repo / "Dockerfile").exists():
        print(f"[build] ERROR: {openclaw_repo}/Dockerfile not found", file=sys.stderr)
        sys.exit(1)

    here = Path(__file__).resolve().parents[1]
    if not (here / "Dockerfile.eval").exists():
        print(f"[build] ERROR: {here}/Dockerfile.eval not found", file=sys.stderr)
        sys.exit(1)

    openclaw_sha = short_sha(openclaw_repo)
    plugin_rev = "0000000"
    if args.install_spec:
        # rev derived from spec (different versions => different images)
        plugin_rev = install_spec_hash(args.install_spec)
    elif args.memory_plugin not in ("memory-core", "noop"):
        plugin_dir = here / "plugins" / args.memory_plugin
        plugin_rev = plugin_content_hash(plugin_dir)

    if args.install_spec:
        print(
            f"[build] openclaw_sha={openclaw_sha}  install_spec={args.install_spec}  "
            f"plugin_id={args.install_plugin_id}  rev={plugin_rev} (install mode)"
        )
    else:
        print(f"[build] openclaw_sha={openclaw_sha}  plugin={args.memory_plugin}  rev={plugin_rev}")

    # ---------------------------------------------------------------- split mode
    # Path A — only build the plugin-agnostic eval-base layer (push target).
    if args.build_eval_base:
        base_tag = build_base(
            openclaw_repo, "memory-core", openclaw_sha,
            variant=args.variant,
            skip_if_exists=not args.rebuild_base,
            plugins_dir=None,
        )
        eval_base_tag = build_eval_base(
            here, base_tag, openclaw_sha,
            variant=args.variant,
            skip_if_exists=not args.rebuild_base,
            minimal_prune=not args.no_minimal_prune,
        )
        pushed_tag = None
        if args.push_eval_base:
            pushed_tag = push_eval_base(eval_base_tag, args.registry)
        print()
        print("[build] DONE (eval-base only)")
        print(f"[build]   base_tag:      {base_tag}")
        print(f"[build]   eval_base_tag: {eval_base_tag}")
        if pushed_tag:
            print(f"[build]   pushed:        {pushed_tag}")
        if args.manifest_out:
            Path(args.manifest_out).write_text(json.dumps({
                "openclaw_sha": openclaw_sha,
                "variant": args.variant,
                "base_tag": base_tag,
                "eval_base_tag": eval_base_tag,
                "pushed_tag": pushed_tag,
            }, indent=2))
            print(f"[build]   manifest:      {args.manifest_out}")
        return

    # Path B — build only the plugin layer on top of an existing eval-base.
    if args.eval_base_image:
        layer_tag = build_eval_plugin_layer(
            here, args.eval_base_image, args.memory_plugin, openclaw_sha,
            plugin_rev,
            variant=args.variant,
            plugins_dir=here / "plugins",
            install_spec=args.install_spec,
            install_plugin_id=args.install_plugin_id,
            extra_install_specs=args._extra_pairs or None,
        )
        print()
        print("[build] DONE (plugin layer)")
        print(f"[build]   eval_base:  {args.eval_base_image}")
        print(f"[build]   layer_tag:  {layer_tag}")
        if args.manifest_out:
            Path(args.manifest_out).write_text(json.dumps({
                "openclaw_sha": openclaw_sha,
                "memory_plugin": args.memory_plugin,
                "plugin_rev": plugin_rev,
                "variant": args.variant,
                "eval_base_image": args.eval_base_image,
                "eval_tag": layer_tag,
            }, indent=2))
            print(f"[build]   manifest:   {args.manifest_out}")
        return

    # ---------------------------------------------------------------- full-build mode
    # In install mode the eval-layer Dockerfile runs `openclaw plugins install`,
    # so we don't stage source from openclaw-eval/plugins/ for the base. We
    # also don't need to OPENCLAW_EXTENSIONS-include the plugin at base
    # layer (it's installed at eval layer). Reuse the memory-core base image
    # tag — it's shared across all install-mode plugins.
    if args.install_spec:
        base_tag = build_base(
            openclaw_repo, "memory-core", openclaw_sha,
            variant=args.variant,
            skip_if_exists=not args.rebuild_base,
            plugins_dir=None,
        )
    else:
        base_tag = build_base(
            openclaw_repo, args.memory_plugin, openclaw_sha,
            variant=args.variant,
            skip_if_exists=not args.rebuild_base,
            plugins_dir=here / "plugins",
        )
    layer_tag = build_eval_layer(
        here, base_tag, args.memory_plugin, openclaw_sha, plugin_rev,
        variant=args.variant,
        plugins_dir=here / "plugins",
        install_spec=args.install_spec,
        install_plugin_id=args.install_plugin_id,
        extra_install_specs=args._extra_pairs or None,
        minimal_prune=not args.no_minimal_prune,
    )

    print()
    print(f"[build] DONE")
    print(f"[build]   base_tag:  {base_tag}")
    print(f"[build]   layer_tag: {layer_tag}")

    if args.manifest_out:
        manifest = {
            "openclaw_sha": openclaw_sha,
            "memory_plugin": args.memory_plugin,
            "plugin_rev": plugin_rev,
            "variant": args.variant,
            "base_tag": base_tag,
            "eval_tag": layer_tag,
        }
        Path(args.manifest_out).write_text(json.dumps(manifest, indent=2))
        print(f"[build]   manifest: {args.manifest_out}")


if __name__ == "__main__":
    main()
