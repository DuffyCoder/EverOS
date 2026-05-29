"""
evaluation.tools.qa_logs
========================

Public API for the qa_logs raw-dump pipeline.

  dump_qa_logs(args, out_dir)       → writes per-qid raw-dump directory
  dump_qa_logs_all_errors(args)     → iterates every wrong qid
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.dataset_loader import load_qa
from evaluation.tools.qa_logs.eval_results import list_wrong_qids
from evaluation.tools.qa_logs.raw_dump import dump_qa_logs as _dump_qa_logs_impl


# ── Internal helpers ──────────────────────────────────────────────────────────

_QID_IDX_RE = re.compile(r"_qa(\d+)$")


def _qid_idx(qid: str) -> int:
    """Map 'locomo_7_qa9' → 9."""
    m = _QID_IDX_RE.search(qid)
    if not m:
        raise ValueError(f"Cannot extract qa index from qid: {qid!r}")
    return int(m.group(1))


def _find_session_jsonl_for_qid(
    results_root: Path,
    system: str,
    run_name: str,
    conv: str,
    qid_idx: int,
) -> Optional[Path]:
    """Locate the per-QA session jsonl for this qid.

    openclaw archives one jsonl per QA at:
        <run>/artifacts/openclaw/run-*/conversations/<conv>/state/agents/main/sessions/qa<idx>.jsonl

    Returns the first match or None.
    """
    run_dir = results_root / f"locomo-{system}-{run_name}"
    # Per-QA file: qa<idx>.jsonl
    matches = sorted(run_dir.glob(
        f"artifacts/openclaw/run-*/conversations/{conv}/state/agents/main/sessions/qa{qid_idx}.jsonl"
    ))
    if matches:
        return matches[0]
    # Legacy / fallback: single session.jsonl per conv (plan's original assumption)
    matches = sorted(run_dir.glob(
        f"artifacts/openclaw/run-*/conversations/{conv}/session.jsonl"
    ))
    return matches[0] if matches else None


# Back-compat alias for tests written against the old name.
def _find_session_jsonl_for_conv(
    results_root: Path,
    system: str,
    run_name: str,
    conv: str,
) -> Optional[Path]:
    return _find_session_jsonl_for_qid(results_root, system, run_name, conv, 0)


def _autodiscover_dataset() -> Optional[Path]:
    """Try to find the LoCoMo dataset json in common locations."""
    candidates = [
        Path("evaluation/data/locomo/locomo10.json"),
        Path("evaluation/data/locomo10.json"),
        Path("evaluation/data/locomo10_test.json"),
        Path("evaluation/data/locomo_test.json"),
        Path("data/locomo10.json"),
        Path("data/locomo10_test.json"),
        Path("data/locomo_test.json"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _autodiscover_ovdata() -> Optional[Path]:
    """Find .ovdata root. Priority: OV_OVDATA_ROOT env -> sibling OpenViking-fork
    repo (search upward) -> None."""
    import os
    env = os.environ.get("OV_OVDATA_ROOT")
    if env and Path(env).exists():
        return Path(env)
    # Walk up the worktree looking for a sibling OpenViking-fork/.ovdata.
    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        candidate = parent.parent / "OpenViking-fork" / ".ovdata"
        if candidate.exists():
            return candidate
        # Also try common layouts
        for sibling in ("OpenViking-fork", "openviking", "OpenViking"):
            candidate2 = parent / sibling / ".ovdata"
            if candidate2.exists():
                return candidate2
    return None


# ── Public API ────────────────────────────────────────────────────────────────


def dump_qa_logs(args: CLIArgs, out_dir: Path) -> None:
    """Raw-dump entrypoint. Writes unprocessed source files into out_dir.

    Resolves dataset / ov_log / ovdata / session_jsonl / answer_results from args
    and delegates to raw_dump.dump_qa_logs.
    """
    if args.dataset:
        dataset_path = Path(args.dataset)
    else:
        dataset_path = _autodiscover_dataset()
        if dataset_path is None:
            raise FileNotFoundError(
                "Cannot find LoCoMo dataset json. "
                "Pass --dataset or place it at evaluation/data/locomo/locomo10.json"
            )

    ov_log = Path(args.ov_log) if args.ov_log else Path(".runlogs/ov-server.log")

    if args.ovdata:
        ovdata_root = Path(args.ovdata)
    else:
        ovdata_root = _autodiscover_ovdata()
        if ovdata_root is None:
            raise FileNotFoundError(
                "Cannot find .ovdata root. "
                "Pass --ovdata or set OV_OVDATA_ROOT environment variable."
            )

    qa = load_qa(dataset_path, args.qid)
    qid_idx = _qid_idx(args.qid)
    session_path = _find_session_jsonl_for_qid(
        args.results_root, args.system, args.run_name, qa.conv, qid_idx,
    )
    answer_results_path = (
        args.results_root
        / f"locomo-{args.system}-{args.run_name}"
        / "answer_results.json"
    )

    _dump_qa_logs_impl(
        args,
        out_dir,
        dataset_path=dataset_path,
        ov_log=ov_log,
        ovdata_root=ovdata_root,
        session_jsonl=session_path,
        answer_results_path=answer_results_path if answer_results_path.exists() else None,
    )


def dump_qa_logs_all_errors(args: CLIArgs) -> None:
    """Raw-dump version of all-errors mode: one directory per wrong qid."""
    import dataclasses

    eval_results_path = (
        args.results_root / f"locomo-{args.system}-{args.run_name}" / "eval_results.json"
    )
    wrong = list_wrong_qids(eval_results_path)
    print(f"[qa_logs] {len(wrong)} wrong qids in run={args.run_name}")
    base_out = Path(args.out or f"reports/qa_logs/{args.run_name}")

    for i, qid in enumerate(wrong, 1):
        try:
            sub_args = dataclasses.replace(args, qid=qid, qid_mode="single")
            dump_qa_logs(sub_args, base_out / qid)
        except Exception as e:
            print(f"[qa_logs] {qid} FAILED: {e}")
        if i % 50 == 0:
            print(f"[qa_logs] {i}/{len(wrong)} done")
