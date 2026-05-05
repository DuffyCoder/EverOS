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
    if install_spec:
        # install-mode rev replaces source-content rev
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
    args = parser.parse_args()

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
        # Memory plugin slot must match installed id (entrypoint reads
        # MEMORY_PLUGIN_ID for slot wiring; mismatch = silent slot bind
        # to a non-existent plugin).
        if args.memory_plugin != plugin_id:
            print(
                f"[build] ERROR: --memory-plugin '{args.memory_plugin}' must "
                f"match installed plugin id '{plugin_id}' when --install-spec "
                f"is set. Re-run with --memory-plugin {plugin_id}.",
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
