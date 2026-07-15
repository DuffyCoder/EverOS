"""Materialize archived OpenClaw session transcripts as ``qa<idx>.jsonl``.

OpenClaw archive_session renames an active transcript from
``<session-id>.jsonl`` to ``<session-id>.jsonl.<epoch>`` after each QA.
This tool copies those archived transcripts into deterministic per-QA
filenames so other qa_logs tools can discover them.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional


def _archive_sort_key(path: Path) -> tuple[int, str]:
    try:
        suffix_epoch = int(path.name.rsplit(".", 1)[1])
    except (IndexError, ValueError):
        suffix_epoch = 0
    return suffix_epoch, path.name


def _find_archived_jsonl(sessions_dir: Path) -> list[Path]:
    return sorted(
        (
            p
            for p in sessions_dir.glob("*.jsonl.*")
            if p.is_file() and p.name.rsplit(".", 1)[-1].isdigit()
        ),
        key=_archive_sort_key,
    )


def materialize_qa_jsonl(
    sessions_dir: Path,
    *,
    out_dir: Optional[Path] = None,
    start_idx: int = 0,
    force: bool = False,
) -> list[Path]:
    """Copy ``*.jsonl.<epoch>`` archives to ``qa<idx>.jsonl``.

    Archives are ordered by the numeric epoch suffix. The source files are
    never modified.
    """
    sessions_dir = sessions_dir.resolve()
    if not sessions_dir.is_dir():
        raise NotADirectoryError(sessions_dir)
    if start_idx < 0:
        raise ValueError("start_idx must be >= 0")

    target_dir = (out_dir or sessions_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    archives = _find_archived_jsonl(sessions_dir)
    written: list[Path] = []
    for offset, src in enumerate(archives):
        dst = target_dir / f"qa{start_idx + offset}.jsonl"
        if dst.exists() and not force:
            raise FileExistsError(f"{dst} already exists; pass --force to overwrite")
        shutil.copyfile(src, dst)
        written.append(dst)
    return written


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Copy archived OpenClaw <session-id>.jsonl.<epoch> files into "
            "qa<idx>.jsonl files ordered by archive epoch."
        )
    )
    p.add_argument(
        "--sessions-dir",
        type=Path,
        required=True,
        help="Directory containing archived session jsonl files.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to --sessions-dir.",
    )
    p.add_argument(
        "--start-idx",
        type=int,
        default=0,
        help="First QA index to write. Defaults to 0.",
    )
    p.add_argument(
        "--force", action="store_true", help="Overwrite existing qa<idx>.jsonl files."
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    try:
        written = materialize_qa_jsonl(
            args.sessions_dir,
            out_dir=args.out_dir,
            start_idx=args.start_idx,
            force=args.force,
        )
    except Exception as e:
        print(f"[materialize_qa_jsonl] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    print(
        f"[materialize_qa_jsonl] wrote {len(written)} qa jsonl files "
        f"under {(args.out_dir or args.sessions_dir).resolve()}"
    )
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
