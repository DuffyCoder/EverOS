#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


RUNTIME_FILES = [
    "package.json",
    "openclaw.plugin.json",
    "index.ts",
    "client.ts",
    "context-engine.ts",
    "config.ts",
    "auto-recall.ts",
    "memory-ranking.ts",
    "process-manager.ts",
    "runtime-utils.ts",
    "session-transcript-repair.ts",
    "text-utils.ts",
    "tool-call-id.ts",
    "commands/setup.ts",
]


def default_source(repo_root: Path) -> Path:
    return repo_root.parent.parent / "OpenViking" / "examples" / "openclaw-plugin"


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Vendor the OpenViking OpenClaw plugin into openclaw-eval/plugins/openviking."
    )
    parser.add_argument(
        "--source",
        default=str(default_source(repo_root)),
        help="Source OpenViking plugin directory (default: ../OpenViking/examples/openclaw-plugin)",
    )
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"source plugin directory not found: {source}")

    target = repo_root / "plugins" / "openviking"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for rel in RUNTIME_FILES:
        src = source / rel
        if not src.exists():
            raise SystemExit(f"required plugin file missing: {src}")
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    manifest = {
        "source": str(source),
        "copied_files": copied,
    }
    (target / ".vendor-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[vendor-openviking] copied {len(copied)} files -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
