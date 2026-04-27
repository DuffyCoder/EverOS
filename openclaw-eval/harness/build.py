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
) -> str:
    """Step 2: layer eval-runtime on top of openclaw-base."""
    if plugins_dir is not None:
        stage_active_sidecar(eval_dir, plugins_dir, memory_plugin)
    tag = f"openclaw-eval:{openclaw_sha}-{memory_plugin}-{plugin_rev}-{variant}"
    cmd = [
        "docker", "build",
        "-f", str(eval_dir / "Dockerfile.eval"),
        "--build-arg", f"BASE_IMAGE={base_tag}",
        "--build-arg", f"MEMORY_PLUGIN={memory_plugin}",
        "--build-arg", f"OPENCLAW_COMMIT={openclaw_sha}",
        "--build-arg", f"PLUGIN_REV={plugin_rev}",
        "-t", tag,
        str(eval_dir),
    ]
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
    parser.add_argument("--manifest-out",
                        help="optional path to write build-manifest JSON")
    args = parser.parse_args()

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
    if args.memory_plugin not in ("memory-core", "noop"):
        plugin_dir = here / "plugins" / args.memory_plugin
        plugin_rev = plugin_content_hash(plugin_dir)

    print(f"[build] openclaw_sha={openclaw_sha}  plugin={args.memory_plugin}  rev={plugin_rev}")

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
