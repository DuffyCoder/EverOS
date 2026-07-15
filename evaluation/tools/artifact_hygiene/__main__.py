"""Command-line interface for safe evaluation artifact hygiene."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .archive import create_archive
from .inventory import build_inventory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.tools.artifact_hygiene",
        description="Read-only inventory and explicitly gated compact archives.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory", help="write a read-only artifact inventory"
    )
    inventory_parser.add_argument("--repo-root", type=Path, required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)

    archive_parser = subparsers.add_parser(
        "archive", help="plan or explicitly create a compact result archive"
    )
    archive_parser.add_argument("--result", type=Path, required=True)
    archive_parser.add_argument("--archive-root", type=Path, required=True)
    archive_parser.add_argument(
        "--execute",
        action="store_true",
        help="create the archive; without this flag the command only prints a plan",
    )
    archive_parser.add_argument(
        "--expected-plan-digest",
        help="fail before writing unless the fresh plan matches this reviewed digest",
    )
    return parser


def _run_inventory(args: argparse.Namespace) -> int:
    inventory = build_inventory(args.repo_root)
    output = args.output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


def _run_archive(args: argparse.Namespace) -> int:
    result = create_archive(
        args.result,
        args.archive_root,
        execute=args.execute,
        expected_plan_digest=args.expected_plan_digest,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            return _run_inventory(args)
        if args.command == "archive":
            return _run_archive(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
