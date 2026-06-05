"""Post-process annotator for OV server log (v2 — Stage 3 anchored).

Computes per-qa wall-clock windows by anchoring on Stage 3 (Answer)
boundaries from ``pipeline.log`` instead of session-jsonl's bootstrap
turn (which v1 incorrectly treated as qa0's start). Within Stage 3, OV
server log's ``POST /api/v1/search/find`` request timestamps are
clustered (gap-threshold) and assigned in order to qa identifiers from
``answer_results.json``. Each qa's window naturally extends to the next
qa's first /find — so afterTurn / commit tails belong to the right qa,
not labeled as "ingest".

Labels written to ``ov-server.annotated.log``:

* ``[qid=ingest]`` — before Stage 3 start (Stage 1 Add + server startup
  + any pre-Stage-3 housekeeping)
* ``[qid=<qid>]`` — within qa N's window
  (``[cluster_N.start, cluster_{N+1}.start)``; last qa ends at Stage 3 end)
* ``[qid=eval]`` — after Stage 3 end (Stage 4 Evaluate + container idle
  + Stage 5 if present)

This is a stand-alone tool, NOT invoked by the eval pipeline or
``raw_dump``. Run manually after eval completes::

    python -m evaluation.tools.qa_logs.annotate \\
        --run-dir evaluation/results/<run> \\
        --ov-log .runlogs/ov-server.log

Prerequisites:

* Eval was run with strict per-qa serial config
  (``openclaw-docker-openviking-session-bundle-noop-serial.yaml`` or
  equivalent ``search.num_workers=1`` + ``answer.max_concurrent=1``).
* OV server log starts at or before Stage 1 Add (typical case: OV was
  restarted right before eval; a single ``> ov-server.log`` at restart
  truncates the file).
* pipeline.log is naive-UTC; ov-server.log is naive-local. The tool
  converts pipeline.log Stage 3 boundaries to local using the host's
  current UTC offset.
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
_STAGE_START_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Starting Stage (\d+):"
)
_STAGE_END_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Stage (\d+) completed"
)


# Local offset from UTC; used to translate pipeline.log naive-UTC ts to
# the naive-local datetime that ov-server.log writes. Computed once at
# import. Assumes the host's current TZ matches when the log was written.
_LOCAL_OFFSET = datetime.now().astimezone().utcoffset() or timedelta(0)


@dataclass(frozen=True)
class QaWindow:
    qid: str
    start: datetime
    end: datetime
    source: str

    def to_dict(self) -> dict:
        return {
            "qid": self.qid,
            "start_iso": self.start.isoformat(timespec="milliseconds"),
            "end_iso": self.end.isoformat(timespec="milliseconds"),
            "source": self.source,
        }


def _parse_line_ts(line: str) -> Optional[datetime]:
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def _parse_pipeline_stage_times(
    pipeline_log: Path,
) -> dict[str, tuple[Optional[datetime], Optional[datetime]]]:
    """Parse Stage N start/end timestamps from pipeline.log.

    Pipeline.log writes naive-UTC timestamps; we translate to local naive
    datetime so the result is directly comparable to ov-server.log's
    naive-local timestamps. Returns mapping ``"<n>"`` → ``(start, end)``.
    """
    starts: dict[str, datetime] = {}
    ends: dict[str, datetime] = {}
    if not pipeline_log.exists():
        return {}
    with pipeline_log.open(errors="replace") as f:
        for line in f:
            m = _STAGE_START_RE.match(line)
            if m:
                ts_utc = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
                starts[m.group(2)] = ts_utc + _LOCAL_OFFSET
                continue
            m = _STAGE_END_RE.match(line)
            if m:
                ts_utc = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
                ends[m.group(2)] = ts_utc + _LOCAL_OFFSET
    return {k: (starts.get(k), ends.get(k)) for k in set(starts) | set(ends)}


def _load_answer_records(answer_results: Path) -> list[dict]:
    """Return answer records as a list of dicts, in file order."""
    if not answer_results.exists():
        return []
    try:
        data = json.loads(answer_results.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        records = data.get("results")
        if not isinstance(records, list):
            records = []
    elif isinstance(data, list):
        records = data
    else:
        records = []
    return [r for r in records if isinstance(r, dict)]


_QID_RE = re.compile(r"^(.+?)_qa(\d+)$")


def _canonical_qa_order(records: list[dict]) -> list[str]:
    """Sort qids canonically by (conv_id, qa_idx).

    Under strict-serial eval this matches the wall-clock execution order
    (search/answer stages iterate convs in dict-iteration order, which
    matches insertion = file order; within conv qa idx ascends).
    """
    qids: list[tuple[str, int, str]] = []
    for rec in records:
        qid = rec.get("question_id") or rec.get("qid")
        if not isinstance(qid, str):
            continue
        m = _QID_RE.match(qid)
        if m:
            qids.append((m.group(1), int(m.group(2)), qid))
    qids.sort()
    return [q[2] for q in qids]


def _cluster_find_start_timestamps(
    ov_log: Path,
    stage3_start: datetime,
    stage3_end: datetime,
    gap_threshold_sec: float = 5.0,
) -> list[datetime]:
    """Return the first-find timestamp of each /search/find cluster within
    ``[stage3_start, stage3_end]``. Two consecutive finds farther apart
    than ``gap_threshold_sec`` start a new cluster.
    """
    timestamps: list[datetime] = []
    with ov_log.open(errors="replace") as f:
        for line in f:
            if "POST /api/v1/search/find" not in line:
                continue
            ts = _parse_line_ts(line)
            if ts is None or ts < stage3_start or ts > stage3_end:
                continue
            timestamps.append(ts)
    if not timestamps:
        return []
    timestamps.sort()
    starts = [timestamps[0]]
    last_ts = timestamps[0]
    for ts in timestamps[1:]:
        if (ts - last_ts).total_seconds() > gap_threshold_sec:
            starts.append(ts)
        last_ts = ts
    return starts


def collect_qa_windows(
    run_dir: Path, ov_log: Path
) -> tuple[list[QaWindow], Optional[datetime], Optional[datetime], list[str]]:
    """Compute qa windows from pipeline.log + ov-server.log.

    Returns ``(windows, stage3_start, stage3_end, diagnostics)``.
    Each qa N's window = ``[cluster_N.start, cluster_{N+1}.start)``;
    the last qa's end = Stage 3 end.
    """
    diagnostics: list[str] = []

    pipeline_log = run_dir / "pipeline.log"
    stages = _parse_pipeline_stage_times(pipeline_log)
    stage3_start, stage3_end = stages.get("3", (None, None))
    if not stage3_start or not stage3_end:
        diagnostics.append(
            f"pipeline.log missing or incomplete Stage 3 markers "
            f"(start={stage3_start}, end={stage3_end}); cannot anchor qa windows."
        )
        return [], stage3_start, stage3_end, diagnostics

    records = _load_answer_records(run_dir / "answer_results.json")
    qa_order = _canonical_qa_order(records)
    if not qa_order:
        diagnostics.append("answer_results.json has no usable question_id records.")
        return [], stage3_start, stage3_end, diagnostics

    cluster_starts = _cluster_find_start_timestamps(
        ov_log, stage3_start, stage3_end
    )
    if not cluster_starts:
        diagnostics.append(
            "no POST /api/v1/search/find timestamps within Stage 3 window; "
            "no qa windows derived."
        )
        return [], stage3_start, stage3_end, diagnostics

    if len(cluster_starts) != len(qa_order):
        diagnostics.append(
            f"cluster count ({len(cluster_starts)}) != qa count ({len(qa_order)}) "
            f"— zipping in order, leftover {'qas' if len(qa_order) > len(cluster_starts) else 'clusters'} ignored."
        )

    windows: list[QaWindow] = []
    n = min(len(cluster_starts), len(qa_order))
    for i in range(n):
        start = cluster_starts[i]
        end = cluster_starts[i + 1] if i + 1 < n else stage3_end
        windows.append(
            QaWindow(
                qid=qa_order[i],
                start=start,
                end=end,
                source=(
                    f"find_cluster[{i}] within Stage 3 → "
                    f"{'next_cluster' if i + 1 < n else 'stage3_end'}"
                ),
            )
        )
    return windows, stage3_start, stage3_end, diagnostics


def annotate(
    ov_log: Path, run_dir: Path, out_path: Path, windows_json_path: Path
) -> dict:
    """Annotate ``ov_log`` line-by-line and write to ``out_path``.

    Three label categories:

    * ``[qid=ingest]`` — lines whose timestamp is before Stage 3 start
      (or whenever Stage 3 boundaries are not available)
    * ``[qid=<qid>]`` — within qa N's wall-clock window
    * ``[qid=eval]`` — after Stage 3 end

    Lines without a parseable timestamp inherit the previous line's
    prefix (traceback continuations etc.).

    Returns a stats dict with ``total_lines``, ``ingest``, ``eval``,
    ``qa_total``, ``per_qa`` (mapping qid → line count).
    """
    windows, stage3_start, stage3_end, diags = collect_qa_windows(run_dir, ov_log)
    for d in diags:
        print(f"[annotate] {d}", file=sys.stderr)

    metadata = {
        "stage3_start_iso": stage3_start.isoformat(timespec="milliseconds")
        if stage3_start
        else None,
        "stage3_end_iso": stage3_end.isoformat(timespec="milliseconds")
        if stage3_end
        else None,
        "windows": [w.to_dict() for w in windows],
        "diagnostics": diags,
    }
    windows_json_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False)
    )

    if not ov_log.exists():
        raise FileNotFoundError(f"ov-server log not found: {ov_log}")

    starts = [w.start for w in windows]
    counts: dict = {
        "total_lines": 0,
        "ingest": 0,
        "eval": 0,
        "qa_total": 0,
        "per_qa": {},
    }
    last_prefix = "[qid=ingest] "

    with ov_log.open(errors="replace") as f_in, out_path.open("w") as f_out:
        for line in f_in:
            counts["total_lines"] += 1
            ts = _parse_line_ts(line)
            if ts is None:
                f_out.write(f"{last_prefix}{line}")
                _bump_counter(counts, last_prefix)
                continue

            if stage3_start is None or ts < stage3_start:
                prefix = "[qid=ingest] "
            elif stage3_end is not None and ts > stage3_end:
                prefix = "[qid=eval] "
            else:
                idx = bisect.bisect_right(starts, ts) - 1
                if idx >= 0 and ts < windows[idx].end:
                    prefix = f"[qid={windows[idx].qid}] "
                else:
                    # Within Stage 3 but before first cluster or in a gap
                    # — bucket as ingest (rare; happens if Stage 3 starts
                    # before any qa actually fires its first /find).
                    prefix = "[qid=ingest] "
            last_prefix = prefix
            f_out.write(f"{prefix}{line}")
            _bump_counter(counts, prefix)

    return counts


def _bump_counter(counts: dict, prefix: str) -> None:
    if prefix == "[qid=ingest] ":
        counts["ingest"] += 1
    elif prefix == "[qid=eval] ":
        counts["eval"] += 1
    else:
        counts["qa_total"] += 1
        # prefix is "[qid=<qid>] " — slice out the qid.
        qid = prefix[len("[qid="):-2]
        counts["per_qa"][qid] = counts["per_qa"].get(qid, 0) + 1


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Annotate OV server log with per-qa [qid=...] prefixes, "
            "anchored on pipeline.log Stage 3 + /search/find clusters."
        ),
    )
    p.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Eval run directory (contains pipeline.log + answer_results.json).",
    )
    p.add_argument(
        "--ov-log",
        type=Path,
        required=True,
        help="OV server log to annotate (e.g. .runlogs/ov-server.log).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output annotated log path (default: under "
            "<run-dir>/artifacts/openclaw/<latest-run>/ov-server.annotated.log)."
        ),
    )
    p.add_argument(
        "--windows-json",
        type=Path,
        default=None,
        help=(
            "Output windows JSON path "
            "(default: alongside the annotated log as ov-server.windows.json)."
        ),
    )
    return p


def _resolve_output_dir(run_dir: Path) -> Path:
    art_root = run_dir / "artifacts" / "openclaw"
    if not art_root.exists():
        return run_dir
    run_subdirs = sorted(
        art_root.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return run_subdirs[0] if run_subdirs else run_dir


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    run_dir = args.run_dir.resolve()
    ov_log = args.ov_log.resolve()
    if not run_dir.is_dir():
        print(f"[annotate] ERROR: run-dir not found: {run_dir}", file=sys.stderr)
        return 2
    output_dir = _resolve_output_dir(run_dir)
    out_path = args.out or (output_dir / "ov-server.annotated.log")
    windows_json_path = args.windows_json or (
        output_dir / "ov-server.windows.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts = annotate(ov_log, run_dir, out_path, windows_json_path)
    print(
        f"[annotate] wrote {out_path} ({counts['total_lines']} lines: "
        f"{counts['qa_total']} qa, {counts['ingest']} ingest, "
        f"{counts['eval']} eval)"
    )
    print(f"[annotate] wrote {windows_json_path}")
    if counts.get("per_qa"):
        print("[annotate] per-qa line counts:")
        for qid, n in sorted(counts["per_qa"].items()):
            print(f"  {qid}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
