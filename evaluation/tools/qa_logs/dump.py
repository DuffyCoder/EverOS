"""CLI for the qa_logs raw-dump report tool.

This module contains the behavior that used to live behind
``python -m evaluation.tools.qa_logs``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from evaluation.tools.qa_logs import dump_qa_logs, dump_qa_logs_all_errors
from evaluation.tools.qa_logs.cli import parse_args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    base_out = Path(args.out or f"reports/qa_logs/{args.run_name}")
    if args.qid_mode == "all_errors":
        failed = dump_qa_logs_all_errors(args)
        print(f"[qa_logs] wrote per-qid directories under {base_out}/")
        return 1 if failed else 0

    out_dir = base_out / args.qid
    dump_qa_logs(args, out_dir)
    print(f"[qa_logs] wrote raw dump under {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
