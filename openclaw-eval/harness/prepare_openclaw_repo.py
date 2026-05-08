#!/usr/bin/env python3
"""Materialize a pinned OpenClaw checkout for docker image builds.

This script keeps a local checkout under EverOS/.cache so collaborators can:

1. fetch a specific commit from a private/open local git remote
2. build images with:
   uv run python openclaw-eval/harness/build.py --memory-plugin memory-core

The build script defaults to the same checkout directory, so callers no longer
need to pass --openclaw-repo every time.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


def load_project_env() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env_file = repo_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def default_checkout_dir() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    candidate = os.environ.get("OPENCLAW_REPO_PATH") or (repo_root / ".cache" / "openclaw-src")
    return str(Path(candidate).resolve())


def run(cmd: list[str], cwd: Path | None = None, *, capture: bool = False) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SystemExit(
            f"[prepare-openclaw] command failed (exit {result.returncode}): "
            f"{' '.join(cmd)}\n{stderr}"
        )
    return (result.stdout or "").strip()


def ensure_checkout(target_dir: Path, git_url: str) -> None:
    if target_dir.exists():
        if not (target_dir / ".git").exists():
            raise SystemExit(
                f"[prepare-openclaw] target exists but is not a git repo: {target_dir}"
            )
        run(["git", "remote", "set-url", "origin", git_url], cwd=target_dir)
        return

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--origin", "origin", git_url, str(target_dir)])


def checkout_ref(target_dir: Path, git_ref: str) -> str:
    run(["git", "fetch", "--tags", "--prune", "origin"], cwd=target_dir)
    fetched = False
    fetch_attempt = subprocess.run(
        ["git", "fetch", "origin", git_ref],
        cwd=target_dir,
        capture_output=True,
        text=True,
    )
    if fetch_attempt.returncode == 0:
        fetched = True
    if fetched:
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=target_dir)
    else:
        run(["git", "checkout", "--detach", git_ref], cwd=target_dir)
    run(["git", "submodule", "update", "--init", "--recursive"], cwd=target_dir)
    return run(["git", "rev-parse", "--short=7", "HEAD"], cwd=target_dir, capture=True)


def main() -> None:
    load_project_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--git-url",
        default=os.environ.get("OPENCLAW_GIT_URL"),
        help="Private/public OpenClaw git URL. Defaults to OPENCLAW_GIT_URL.",
    )
    parser.add_argument(
        "--git-ref",
        default=os.environ.get("OPENCLAW_GIT_REF"),
        help="Commit/tag/branch to checkout. Defaults to OPENCLAW_GIT_REF.",
    )
    parser.add_argument(
        "--checkout-dir",
        default=default_checkout_dir(),
        help=(
            "Local checkout dir. Defaults to OPENCLAW_REPO_PATH or "
            "EverOS/.cache/openclaw-src."
        ),
    )
    args = parser.parse_args()

    if shutil.which("git") is None:
        raise SystemExit("[prepare-openclaw] git not found in PATH")
    if not args.git_url:
        raise SystemExit("[prepare-openclaw] missing --git-url (or OPENCLAW_GIT_URL)")
    if not args.git_ref:
        raise SystemExit("[prepare-openclaw] missing --git-ref (or OPENCLAW_GIT_REF)")

    target_dir = Path(args.checkout_dir).resolve()
    ensure_checkout(target_dir, args.git_url)
    resolved_sha = checkout_ref(target_dir, args.git_ref)

    print("[prepare-openclaw] ready")
    print(f"[prepare-openclaw]   repo_path: {target_dir}")
    print(f"[prepare-openclaw]   git_url:   {args.git_url}")
    print(f"[prepare-openclaw]   git_ref:   {args.git_ref}")
    print(f"[prepare-openclaw]   commit:    {resolved_sha}")
    print(
        "[prepare-openclaw] next: uv run python openclaw-eval/harness/build.py "
        "--memory-plugin memory-core"
    )


if __name__ == "__main__":
    main()
