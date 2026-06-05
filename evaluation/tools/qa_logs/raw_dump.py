"""Per-stage raw log dumper.

One directory per qid, one file per pipeline stage. Each file is the raw
text/log/json of its source — no markdown wrapping, no tables, no derived
analysis. The reader views with cat / less / grep.

File layout per qid:

    01_question.txt   raw question + golden + evidence dialogue text
    02_ingest.log     raw OV server log lines (Enqueued/upsert) for this conv,
                       filtered to this run's wall-clock window
    03_storage.txt    raw cat of every .md file under .ovdata for this conv —
                       ONLY if .ovdata mtime falls in this run's window
    04_recall.log     raw OV server log lines (vector retrieval), filtered by
                       [qid=...] tag AND this qa's wall-clock window
    05_rerank.log     raw OV server log lines (rerank + recall_trace + telemetry),
                       same scoping as 04
    06_prompt.txt     raw agent LLM user_message_text (verbatim from session jsonl)
    07_thinking.txt   raw agent LLM assistant_thinking
    08_answer.txt     raw agent LLM assistant_text
    09_judge.json     raw eval_results.json entry for this qid

When a stage's source data does not exist for this run (e.g. OV server log
was rotated past this run's time window, .ovdata namespace was re-ingested
by a later run, session jsonl missing), the file is a single
``# unavailable: <reason>`` line. That line is the only tool-generated text.

Wall-clock window scoping is critical because OV server log
(``.runlogs/ov-server.log``) and ``.ovdata`` are **globally shared live
files**, not per-run snapshots. Without window filtering, ``--run-name X``
would silently return live state instead of run X's state. The window
filter is the only way to be honest about data freshness.

Window sources:
  * per-qa (04, 05): session jsonl ``user_ts_ms`` + answer_results.json
    ``answer_latency_ms`` → ``[user_ts, user_ts + latency]``
  * per-run (02): the run's ``pipeline.log`` first and last timestamps
  * per-run-or-recent (03): any .md file under the conv's memories dir
    whose mtime falls within the per-run window — if no .md is in-window,
    the storage state is from a different run and we mark unavailable
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.dataset_loader import load_qa
from evaluation.tools.qa_logs.session_jsonl import find_user_message


_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
_PIPELINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ")


@dataclass
class WallClockWindow:
    """A `[start, end]` window plus a label describing where it came from."""
    start: Optional[datetime]
    end: Optional[datetime]
    source: str

    @property
    def is_known(self) -> bool:
        return self.start is not None and self.end is not None


# ── Public entrypoint ────────────────────────────────────────────────────────


def dump_qa_logs(
    args: CLIArgs,
    out_dir: Path,
    *,
    dataset_path: Path,
    ov_log: Path,
    ovdata_root: Path,
    session_jsonl: Optional[Path],
    answer_results_path: Optional[Path] = None,
) -> None:
    """Write 9 raw per-stage files into ``out_dir`` for ``args.qid``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    qa = load_qa(dataset_path, args.qid)
    run_dir = args.results_root / f"locomo-{args.system}-{args.run_name}"
    eval_path = run_dir / "eval_results.json"
    answer_record = _answer_record(answer_results_path, args.qid)

    qa_window = _qa_window(session_jsonl, args.qid, answer_record)
    run_window = _run_window(run_dir)
    # NOTE: previous versions fell back to run_window when qa_window was
    # unknown. That was unsafe — it expanded per-qa dumps to the full
    # multi-hour run, polluting 04/05 with every other qa's recalls. After
    # answer_stage started writing metadata.qa_start_unix_ms, _qa_window has
    # a robust fallback path; if both session_jsonl and qa_start_unix_ms are
    # absent we now leave qa_window unknown and let 04/05 emit "# unavailable".

    (out_dir / "01_question.txt").write_text(_question_text(qa))
    (out_dir / "02_ingest.log").write_text(
        _ingest_log(ov_log, qa.conv, run_window)
    )
    (out_dir / "03_storage.txt").write_text(
        _storage_files(ovdata_root, qa.conv, run_window)
    )
    (out_dir / "04_recall.log").write_text(
        _recall_log(ov_log, args.qid, qa_window)
    )
    (out_dir / "05_rerank.log").write_text(
        _rerank_log(ov_log, args.qid, qa_window)
    )

    prompt, thinking, answer = _session_jsonl_blobs(session_jsonl, args.qid)
    (out_dir / "06_prompt.txt").write_text(prompt)
    (out_dir / "07_thinking.txt").write_text(thinking)
    (out_dir / "08_answer.txt").write_text(answer)

    (out_dir / "09_judge.json").write_text(_judge_json(eval_path, args.qid))


# ── Window resolution ────────────────────────────────────────────────────────


def _qa_window(
    session_jsonl: Optional[Path], qid: str, answer_record: Optional[dict],
) -> WallClockWindow:
    """qa window from session_jsonl user_ts (preferred) or answer_results.json (fallback).

    Two anchoring paths, in order of precision:

    1. **session_jsonl user_ts**: per-qa ``qa<idx>.jsonl`` (or single per-conv
       ``session.jsonl``) records the user-turn ``timestamp`` field in unix
       epoch ms. Best when the adapter writes per-qa jsonl.

    2. **answer_results.json qa_start_unix_ms**: session-bundle adapters write
       only ``locomo_0__bootstrap.jsonl`` (no per-qa file), so user_ts is
       unavailable. answer_stage records ``metadata.qa_start_unix_ms`` (wall-
       clock captured under conv_lock + semaphore) for these cases.

    Both paths compute ``end = start + answer_latency_ms``.
    Bounds are converted to local-host tz via ``datetime.fromtimestamp`` so
    they align with OV server log timestamps (Python ``logging`` defaults).
    """
    latency_ms = ((answer_record or {}).get("metadata") or {}).get("answer_latency_ms")

    # Path 1: session_jsonl user_ts + latency
    if session_jsonl and session_jsonl.exists():
        try:
            qid_idx = int(qid.rsplit("_qa", 1)[1])
        except (IndexError, ValueError) as e:
            return WallClockWindow(None, None, f"unavailable: bad qid {qid!r}: {e}")
        msg_idx = 0 if session_jsonl.name == f"qa{qid_idx}.jsonl" else qid_idx
        try:
            turn = find_user_message(session_jsonl, msg_idx)
        except (IndexError, KeyError):
            turn = None
        if (turn and turn.unix_ts_ms and turn.unix_ts_ms > 0
                and isinstance(latency_ms, (int, float)) and latency_ms > 0):
            start = datetime.fromtimestamp(turn.unix_ts_ms / 1000.0)
            end = datetime.fromtimestamp((turn.unix_ts_ms + latency_ms) / 1000.0)
            return WallClockWindow(start, end, f"session_jsonl user_ts + {latency_ms}ms")

    # Path 2: answer_results.json qa_start_unix_ms + latency
    start_ms = ((answer_record or {}).get("metadata") or {}).get("qa_start_unix_ms")
    if (isinstance(start_ms, int) and start_ms > 0
            and isinstance(latency_ms, (int, float)) and latency_ms > 0):
        start = datetime.fromtimestamp(start_ms / 1000.0)
        end = datetime.fromtimestamp((start_ms + latency_ms) / 1000.0)
        return WallClockWindow(
            start, end,
            f"answer_results qa_start_unix_ms + {latency_ms}ms",
        )

    return WallClockWindow(
        None, None,
        "unavailable: no session_jsonl user_ts and no answer_results.qa_start_unix_ms",
    )


def _run_window(run_dir: Path) -> WallClockWindow:
    """Per-run window anchored on artifacts dir mtimes (unix epoch).

    Sampled mtimes (all unix epoch, tz-safe across host moves):
      * ``run-YYYYMMDDTHHMMSS/`` artifact dir mtime — captures run start
      * every per-qa session jsonl mtime — captures per-qa progress
        (some images don't write these — see "fallback" below)
      * every per-conv ``events.jsonl`` mtime — written incrementally
      * ``eval_results.json`` and ``answer_results.json`` mtimes —
        written at run end, so they pin the upper bound when session
        jsonl per-qa files are absent
      * ``pipeline.log`` mtime — written throughout the run

    Min-max gives a sound run window in the current host's local tz
    (matching OV server log timestamps).
    """
    art_root = run_dir / "artifacts" / "openclaw"
    mtimes: list[float] = []
    if art_root.exists():
        for run_artifact_dir in art_root.glob("run-*"):
            try:
                mtimes.append(run_artifact_dir.stat().st_mtime)
            except OSError:
                pass
            for p in run_artifact_dir.rglob("state/agents/main/sessions/*.jsonl"):
                try:
                    mtimes.append(p.stat().st_mtime)
                except OSError:
                    pass
            for p in run_artifact_dir.rglob("events.jsonl"):
                try:
                    mtimes.append(p.stat().st_mtime)
                except OSError:
                    pass
    # End-of-run signals — these are reliably written even when per-qa
    # session jsonl artifacts are not (e.g. session bundle bridges that
    # produce only locomo_0__bootstrap.jsonl).
    for name in ("eval_results.json", "answer_results.json", "pipeline.log"):
        p = run_dir / name
        if p.exists():
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                pass
    if not mtimes:
        return WallClockWindow(None, None,
                               f"unavailable: no run artifacts under {run_dir}")
    start = datetime.fromtimestamp(min(mtimes))
    end = datetime.fromtimestamp(max(mtimes))
    return WallClockWindow(start, end, "run artifact + result file mtimes (unix epoch)")


def _line_ts(line: str) -> Optional[datetime]:
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def _in_window(ts: Optional[datetime], window: WallClockWindow) -> bool:
    if not window.is_known:
        return True  # window unknown — don't reject
    if ts is None:
        return False
    return window.start <= ts <= window.end


# ── 01 question ──────────────────────────────────────────────────────────────


def _question_text(qa) -> str:
    parts = [
        f"QUESTION: {qa.question}",
        "",
        f"GOLDEN: {qa.golden}",
        "",
        "EVIDENCE:",
    ]
    if not qa.evidence_turns:
        parts.append("(no evidence turns listed in dataset.qa.evidence)")
        return "\n".join(parts) + "\n"
    for t in qa.evidence_turns:
        parts.append(
            f"[{t.dia_id}] {t.speaker} @ session_{t.session_idx} ({t.timestamp or '?'})"
        )
        parts.append(t.text or "")
        parts.append("")
    return "\n".join(parts)


# ── 02 ingest ────────────────────────────────────────────────────────────────


def _ingest_log(ov_log: Path, conv: str, run_window: WallClockWindow) -> str:
    """OV server log lines matching this conv AND inside this run's window.

    Conv filter (Enqueued|upsert + conv substring) is the same as before;
    the new constraint is the wall-clock window — without it, ingest from
    a completely different run leaks into the output and misleads the
    reader. If 0 lines match, the file shows an unavailable stub naming
    the window we expected to see.
    """
    if not ov_log.exists():
        return f"# unavailable: ov_log not found at {ov_log}\n"
    if not run_window.is_known:
        return (
            f"# unavailable: cannot scope ingest lines without a run window "
            f"({run_window.source})\n"
        )
    out: list[str] = []
    conv_l = conv.lower()
    for line in ov_log.open(errors="replace"):
        ll = line.lower()
        if conv_l not in ll:
            continue
        if "enqueued" not in ll and "upsert" not in ll:
            continue
        ts = _line_ts(line)
        if not _in_window(ts, run_window):
            continue
        out.append(line.rstrip("\n"))
    if not out:
        return (
            f"# unavailable: no ingest lines in {ov_log} matching "
            f"conv={conv} within run window "
            f"[{run_window.start} → {run_window.end}] ({run_window.source}). "
            f"OV server log is a live file that rotates per process restart; "
            f"this run's ingest traffic was overwritten by a later run.\n"
        )
    return "\n".join(out) + "\n"


# ── 03 storage ───────────────────────────────────────────────────────────────


def _storage_files(
    ovdata_root: Path, conv: str, run_window: WallClockWindow,
) -> str:
    """Cat .md files only when their mtime indicates they belong to this run.

    .ovdata is a shared namespace that gets re-ingested by later runs. If
    every .md file under the conv's memories dir was written outside this
    run's wall-clock window, the current state is some other run's data,
    not this run's — mark unavailable.
    """
    if not ovdata_root.exists():
        return f"# unavailable: ovdata root not found: {ovdata_root}\n"
    mem_root = ovdata_root / "viking" / "default" / "user" / conv / "memories"
    if not mem_root.exists():
        for cand in ovdata_root.rglob(f"user/{conv}/memories"):
            mem_root = cand
            break
    if not mem_root.exists():
        return f"# unavailable: memories dir not found under {ovdata_root} for {conv}\n"
    files = sorted(mem_root.rglob("*.md"))
    if not files:
        return f"# unavailable: no .md files under {mem_root}\n"
    if run_window.is_known:
        in_window = [
            md for md in files
            if _in_window(datetime.fromtimestamp(md.stat().st_mtime), run_window)
        ]
        if not in_window:
            # All current files are from a different run — be honest about it.
            sample_mtime = datetime.fromtimestamp(files[0].stat().st_mtime)
            return (
                f"# unavailable: {len(files)} .md files exist under {mem_root} "
                f"but ALL mtimes are outside this run's window "
                f"[{run_window.start} → {run_window.end}] ({run_window.source}). "
                f"Sample mtime: {sample_mtime}. "
                f".ovdata is a shared namespace re-ingested per run; this run's "
                f"storage state was overwritten by a later ingest.\n"
            )
        files = in_window  # keep all in-window files
    parts: list[str] = []
    for md in files:
        rel = md.relative_to(mem_root)
        uri = f"viking://user/{conv}/memories/{rel.as_posix()}"
        try:
            content = md.read_text(errors="replace")
        except OSError as e:
            content = f"[read error: {e}]"
        parts.append(f"==={uri}===")
        parts.append(content.rstrip("\n"))
        parts.append("")
    return "\n".join(parts) + "\n"


# ── 04 recall ────────────────────────────────────────────────────────────────


def _recall_log(ov_log: Path, qid: str, qa_window: WallClockWindow) -> str:
    """Vector retrieval lines for this qa, scoped by wall-clock window.

    Uses pure time-window filtering (no ``[qid=...]`` tag — server source
    is clean; qid attribution is done post-process via
    ``evaluation/tools/qa_logs/annotate.py`` when needed). Strict-serial
    eval (``openclaw-docker-openviking-session-bundle-noop-serial.yaml``)
    guarantees the qa window has no overlap with other qa, so every line
    in the window belongs to this qa.
    """
    del qid  # qid no longer used for filtering; window does the work
    if not ov_log.exists():
        return f"# unavailable: ov_log not found at {ov_log}\n"
    if not qa_window.is_known:
        return (
            f"# unavailable: cannot scope recall without a qa window "
            f"({qa_window.source})\n"
        )
    out: list[str] = []
    for line in ov_log.open(errors="replace"):
        ts = _line_ts(line)
        if not _in_window(ts, qa_window):
            continue
        if "hierarchical_retriever" in line and "[RecursiveSearch]" not in line:
            out.append(line.rstrip("\n"))
        elif "viking_vector_index_backend" in line:
            out.append(line.rstrip("\n"))
        elif "openai_embedders" in line:
            out.append(line.rstrip("\n"))
    if not out:
        return (
            f"# unavailable: no vector-retrieval lines in {ov_log} "
            f"within qa window [{qa_window.start} → {qa_window.end}] "
            f"({qa_window.source}). Either the eval was not strict-serial "
            f"(non-overlapping windows required), or the OV server log "
            f"was rotated past this qa's time.\n"
        )
    return "\n".join(out) + "\n"


# ── 05 rerank ────────────────────────────────────────────────────────────────


def _rerank_log(ov_log: Path, qid: str, qa_window: WallClockWindow) -> str:
    """Rerank phase lines for this qa, scoped by wall-clock window."""
    del qid  # qid no longer used for filtering; window does the work
    if not ov_log.exists():
        return f"# unavailable: ov_log not found at {ov_log}\n"
    if not qa_window.is_known:
        return (
            f"# unavailable: cannot scope rerank without a qa window "
            f"({qa_window.source})\n"
        )
    out: list[str] = []
    for line in ov_log.open(errors="replace"):
        ts = _line_ts(line)
        if not _in_window(ts, qa_window):
            continue
        if "hierarchical_retriever" in line and "[RecursiveSearch]" in line:
            out.append(line.rstrip("\n"))
        elif "openai_rerank" in line:
            out.append(line.rstrip("\n"))
        elif "telemetry.execution" in line:
            out.append(line.rstrip("\n"))
    if not out:
        return (
            f"# unavailable: no rerank lines in {ov_log} within "
            f"qa window [{qa_window.start} → {qa_window.end}] "
            f"({qa_window.source}).\n"
        )
    return "\n".join(out) + "\n"


# ── 06 / 07 / 08 session jsonl blobs ─────────────────────────────────────────


def _session_jsonl_blobs(
    session_jsonl: Optional[Path], qid: str
) -> tuple[str, str, str]:
    if not session_jsonl or not session_jsonl.exists():
        msg = f"# unavailable: session jsonl not found ({session_jsonl})\n"
        return msg, msg, msg
    qid_idx = int(qid.rsplit("_qa", 1)[1])
    msg_idx = 0 if session_jsonl.name == f"qa{qid_idx}.jsonl" else qid_idx
    try:
        turn = find_user_message(session_jsonl, msg_idx)
    except (IndexError, KeyError) as e:
        msg = f"# unavailable: could not locate qa turn in {session_jsonl}: {e}\n"
        return msg, msg, msg
    prompt = turn.text if turn.text is not None else (
        "# unavailable: session jsonl has no user message text for this turn\n"
    )
    thinking = turn.assistant_thinking if turn.assistant_thinking is not None else (
        "# unavailable: assistant emitted no thinking block\n"
    )
    answer = turn.assistant_text if turn.assistant_text is not None else (
        "# unavailable: assistant emitted no text response\n"
    )
    return _with_trailing_nl(prompt), _with_trailing_nl(thinking), _with_trailing_nl(answer)


def _with_trailing_nl(s: str) -> str:
    return s if s.endswith("\n") else s + "\n"


# ── 09 judge ─────────────────────────────────────────────────────────────────


def _judge_json(eval_results_path: Path, qid: str) -> str:
    if not eval_results_path.exists():
        return f"# unavailable: eval_results.json not found at {eval_results_path}\n"
    try:
        data = json.loads(eval_results_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return f"# unavailable: {type(e).__name__} reading eval_results.json: {e}\n"
    detailed = data.get("detailed_results")
    if isinstance(detailed, dict):
        for user, recs in detailed.items():
            for r in recs or []:
                if r.get("question_id") == qid:
                    record = {"user": user, **r}
                    return json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    for r in data.get("results") or []:
        if r.get("question_id") == qid:
            return json.dumps(r, ensure_ascii=False, indent=2) + "\n"
    return f"# unavailable: no entry for qid={qid} in {eval_results_path}\n"


def _answer_record(
    answer_results_path: Optional[Path], qid: str
) -> Optional[dict]:
    if not answer_results_path or not answer_results_path.exists():
        return None
    data = json.loads(answer_results_path.read_text())
    if isinstance(data, list):
        for r in data:
            if r.get("question_id") == qid:
                return r
    return None
