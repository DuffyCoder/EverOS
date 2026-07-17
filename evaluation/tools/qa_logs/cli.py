"""Argument parser for qa_logs.dump: reconstruct full-link log report for a single qa
or all wrong qa of a given run.

Examples:
  python -m evaluation.tools.qa_logs.dump           # all wrong qa, latest run
  python -m evaluation.tools.qa_logs.dump --qid locomo_7_qa_9
  python -m evaluation.tools.qa_logs.dump --run-name main-noproxy-c4
"""
import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Public system ID registered in evaluation/config/systems/index.yaml.
DEFAULT_SYSTEM = "openclaw-docker-openviking-session-bundle-noop"
DEFAULT_DATASET_PREFIX = "locomo"
_QID_CANONICAL_RE = re.compile(r"^(locomo_\d+)_qa(\d+)$")
_QID_INPUT_RE = re.compile(r"^(locomo_\d+)_qa_?(\d+)$")


def normalize_qid(qid: str) -> str:
    """Accept either user-friendly `locomo_7_qa_9` or canonical `locomo_7_qa9`;
    return canonical form used in dataset and eval_results.json."""
    m = _QID_INPUT_RE.match(qid)
    if not m:
        raise ValueError(
            f"bad qid: {qid!r} (expected locomo_<conv>_qa<idx> or locomo_<conv>_qa_<idx>)"
        )
    return f"{m.group(1)}_qa{m.group(2)}"


def resolve_latest_run(
    results_root: Path,
    dataset_prefix: str = DEFAULT_DATASET_PREFIX,
    system: str = DEFAULT_SYSTEM,
) -> str:
    """Pick latest-mtime run dir under results_root, return its run-name
    (strip `<dataset>-<system>-` prefix)."""
    if not results_root.exists():
        raise FileNotFoundError(f"results root not found: {results_root}")
    candidates = [
        p for p in results_root.iterdir()
        if p.is_dir() and p.name.startswith(f"{dataset_prefix}-{system}-")
    ]
    if not candidates:
        # Fallback: any directory under results
        candidates = [p for p in results_root.iterdir() if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"no run dirs under {results_root}")
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    prefix = f"{dataset_prefix}-{system}-"
    name = latest.name
    if name.startswith(prefix):
        return name[len(prefix):]
    # Fallback: strip leading "<dataset>-<anything>-" so any system prefix is removed
    import re as _re
    m = _re.match(rf"^{re.escape(dataset_prefix)}-[^-]+-(.+)$", name)
    return m.group(1) if m else name


@dataclass
class CLIArgs:
    qid: Optional[str]               # canonical (no underscore between qa and idx); None == all errors
    qid_mode: str                    # "single" or "all_errors"
    run_name: str
    system: str
    ov_log: Optional[str]
    ovdata: Optional[str]
    dataset: Optional[str]
    results_root: Path
    out: Optional[str]


def _default_results_root() -> Path:
    # Resolves relative to current working directory; main wrapper can override
    return Path("evaluation/results")


def parse_args(argv: Optional[list[str]] = None) -> CLIArgs:
    p = argparse.ArgumentParser(prog="qa_logs")
    p.add_argument("--qid", default=None,
                   help="qa id, e.g. locomo_7_qa_9. Omit to process all wrong qa of the run.")
    p.add_argument("--run-name", default=None,
                   help="run name suffix. Omit to pick latest by mtime under --results-root.")
    p.add_argument("--system", default=DEFAULT_SYSTEM)
    p.add_argument("--ov-log", default=None,
                   help="path to ov-server.log; defaults to .runlogs/ov-server.log")
    p.add_argument("--ovdata", default=None,
                   help="path to OpenViking-fork .ovdata root")
    p.add_argument("--dataset", default=None,
                   help="path to LoCoMo dataset json; auto-discovered if omitted")
    p.add_argument("--results-root", default=None,
                   help="evaluation/results parent; auto-discovered if omitted")
    p.add_argument("--out", default=None,
                   help="output dir; defaults to reports/qa_logs/<run-name>/<qid>/")
    ns = p.parse_args(argv)

    results_root = Path(ns.results_root) if ns.results_root else _default_results_root()

    qid_canonical = normalize_qid(ns.qid) if ns.qid else None
    qid_mode = "single" if qid_canonical else "all_errors"

    if ns.run_name:
        run_name = ns.run_name
    else:
        run_name = resolve_latest_run(results_root, system=ns.system)

    return CLIArgs(
        qid=qid_canonical, qid_mode=qid_mode,
        run_name=run_name,
        system=ns.system,
        ov_log=ns.ov_log, ovdata=ns.ovdata, dataset=ns.dataset,
        results_root=results_root, out=ns.out,
    )
