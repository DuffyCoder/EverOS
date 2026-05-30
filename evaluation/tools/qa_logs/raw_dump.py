"""Raw log/dataset/jsonl excerpt dumper.

Instead of rendering a synthesized markdown report, this module writes one
directory per qid with 10-12 files, each containing unprocessed source content
(JSON records, log line excerpts, .md file contents). The user inspects these
directly with cat / grep / less — no second-order interpretation by qa_logs.

File layout per qid (under <out_dir>/<qid>/):

    00_summary.txt               1-pager: qid, question, golden, judge, generated_answer
    01_dataset_record.json       dataset.qa[qid_idx] JSON object
    02_evidence_turns.json       session turns that evidence dia_id references
    03_ovdata_golden.md          .ovdata candidate md files that match evidence phrase
    04_log_ingest.txt            ov-server.log lines mentioning candidate golden URI
    05_log_archive.txt           ov-server.log task_tracker / commit_session lines for this qid
    06_session_jsonl_record.json session jsonl: this qa's user + assistant records
    07_log_recall.txt            ov-server.log lines tagged with [qid=...] for recall stages
    08_eval_record.json          eval_results + answer_results record for this qid
    09_window.txt                window inference notes (source, start, end, n lines)
    10_recall_topk.txt           structured vector+rerank topk table (raw rows)
    11_picking_analysis.txt      reverse picked bullets → URIs + cross-ref recall scores

Scoping mechanism
-----------------
After feat/qid-tagging on the OpenViking-fork server side, every recall and
archive log row carries a ``[qid=<qid>]`` prefix. The dumper greps by that
tag — the per-qid time window is only kept as a *secondary* sanity bound, not
as an exclusive filter. For pre-tagging legacy logs the ``_LEGACY_LOG_COMPAT``
constant gates a conv-match fallback (which is the OLD broken behavior, kept
only for diagnosing historical runs).

No heuristics
-------------
``_infer_window`` returns exact ``[user_ts, user_ts + answer_latency_ms]``
bounds when both session jsonl and answer_results are available, or an
``unavailable_*`` spec otherwise. No ±N-second padding is applied anywhere.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.dataset_loader import EvidenceTurn, load_qa
from evaluation.tools.qa_logs.eval_results import load_eval_result
from evaluation.tools.qa_logs.session_jsonl import find_user_message

# ── Limits to keep dumps tractable ────────────────────────────────────────────

INGEST_LINE_CAP = 200
ARCHIVE_LINE_CAP = 200
RECALL_LINE_CAP = 2000
OVDATA_FILE_CAP = 5
OVDATA_FILE_BYTES_CAP = 32_000

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
_VECTOR_RE = re.compile(r"('vector':\s*)\[[\d.\-eE+,\s]+\]")
_VECTOR_RE_JSON = re.compile(r'("vector":\s*)\[[\d.\-eE+,\s]+\]')

# vector_topk row: "  [N] URI: <uri>, score: X.XXXX, level: Y, ..."
_VEC_ROW_RE = re.compile(
    r"\[(\d+)\] URI:\s*(\S+),\s*score:\s*([\d.\-eE+]+),\s*level:\s*(\d+)"
)
# rerank "Added initial candidate" row: "[RecursiveSearch] Added initial candidate: <uri> (score: X.XXXX)"
_RERANK_ROW_RE = re.compile(
    r"Added initial candidate:\s*(\S+)\s*\(score:\s*([\d.\-eE+]+)\)"
)
# Telemetry summary returned count
_RETURNED_RE = re.compile(r"'returned':\s*(\d+)")

# qid tag injected by OpenViking-fork plain-debug logger after feat/qid-tagging
_QID_RE = re.compile(r"\[qid=([^\]]+)\]")
# Set True ONLY when inspecting pre-qid-tagging legacy logs. Falls back to
# conv-substring matching, which leaks rows from other concurrent QAs of the
# same conv — diagnostic value only.
_LEGACY_LOG_COMPAT = False


def _strip_vector_arrays(line: str) -> str:
    """Replace huge embedding arrays inline with '<truncated N floats>' marker.

    Keeps the surrounding fields (uri, level, owner_user_id, meta, ...) untouched
    so the line still reads as raw log output.
    """
    def _sub(m: re.Match) -> str:
        body = m.group(0)
        try:
            inner = body[body.index("[") + 1: body.rindex("]")]
            n = inner.count(",") + 1 if inner.strip() else 0
        except ValueError:
            n = 0
        return f"{m.group(1)}[<truncated {n} floats>]"

    line = _VECTOR_RE.sub(_sub, line)
    line = _VECTOR_RE_JSON.sub(_sub, line)
    return line


def _match_qid(line: str, qid: str, conv: str) -> bool:
    """Return True if line carries [qid={qid}] tag.

    In legacy compat mode (pre-qid-tagging logs), fall back to conv-substring
    match — this is the OLD broken behavior preserved only for legacy run
    diagnostics. The fallback leaks rows from concurrent QAs of the same conv.
    """
    if f"[qid={qid}]" in line:
        return True
    if _LEGACY_LOG_COMPAT and conv in line:
        return True
    return False


@dataclass
class WindowSpec:
    """Result of window inference.

    ``source`` is a free-form label describing how the bounds were derived.
    Recognized values:
      * ``session_jsonl_exact`` — exact ``[user_ts, user_ts + answer_latency_ms]``.
      * ``unavailable_no_session_jsonl`` — no per-QA session jsonl artifact.
      * ``unavailable_session_parse_error:<exc>`` — jsonl present but unreadable.
      * ``unavailable_no_user_ts`` — turn record had no usable timestamp.
      * ``unavailable_no_latency_in_answer_results`` — user_ts known but no
        ``answer_latency_ms`` in the answer_results record.

    When the source is any ``unavailable_*`` variant, ``start`` and ``end`` are
    ``None`` and the line scanners skip the window-bounded check; the
    ``[qid=...]`` tag is then the sole scoping mechanism.
    """
    source: str
    start: Optional[datetime]
    end: Optional[datetime]
    user_ts_ms: Optional[int] = None
    answer_latency_ms: Optional[float] = None


# ── Public entrypoint ─────────────────────────────────────────────────────────

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
    """Write 10-12 raw files into out_dir for the given qid."""
    out_dir.mkdir(parents=True, exist_ok=True)

    qa = load_qa(dataset_path, args.qid)
    eval_path = args.results_root / f"locomo-{args.system}-{args.run_name}" / "eval_results.json"
    eval_result = load_eval_result(eval_path, args.qid)

    raw_dataset = _raw_dataset_record(dataset_path, qa.conv, args.qid)
    answer_record = _answer_record(answer_results_path, args.qid)

    window = _infer_window(args, qa.conv, session_jsonl, answer_record)

    # 00 summary
    _write_summary(out_dir / "00_summary.txt", args.qid, qa, eval_result, window)
    # 01 dataset record
    (out_dir / "01_dataset_record.json").write_text(
        json.dumps(raw_dataset, ensure_ascii=False, indent=2) + "\n"
    )
    # 02 evidence turns
    (out_dir / "02_evidence_turns.json").write_text(
        json.dumps(
            [_evidence_turn_to_dict(t) for t in qa.evidence_turns],
            ensure_ascii=False, indent=2,
        ) + "\n"
    )
    # 03 ovdata golden md files
    _dump_ovdata_candidates(out_dir / "03_ovdata_golden.md", ovdata_root, qa.conv, qa.evidence_turns)
    # 04 ingest log lines
    _dump_ingest_lines(out_dir / "04_log_ingest.txt", ov_log, qa.conv, qa.evidence_turns)
    # 05 archive log lines (qid-tag scoped)
    _dump_archive_lines(out_dir / "05_log_archive.txt", ov_log, qa.conv, args.qid, window)
    # 06 session jsonl record
    _dump_session_record(out_dir / "06_session_jsonl_record.json", session_jsonl, args.qid)
    # 07 recall log lines (qid-tag scoped)
    _dump_recall_lines(out_dir / "07_log_recall.txt", ov_log, qa.conv, args.qid, window)
    # 08 eval record
    eval_record_raw = _eval_record_raw(eval_path, args.qid)
    (out_dir / "08_eval_record.json").write_text(
        json.dumps(
            {
                "eval_results.json": eval_record_raw,
                "answer_results.json": answer_record,
            },
            ensure_ascii=False, indent=2,
        ) + "\n"
    )
    # 09 window notes
    (out_dir / "09_window.txt").write_text(_render_window(window) + "\n")
    # 10 recall topk structured (vector top N + rerank candidates with URI/score/level)
    _dump_recall_topk(out_dir / "10_recall_topk.txt", ov_log, qa.conv, window)
    # 11 picking analysis (reverse picked bullets to URIs + cross-ref with vector/rerank scores)
    _dump_picking_analysis(
        out_dir / "11_picking_analysis.txt",
        session_jsonl,
        ovdata_root,
        qa.conv,
        out_dir / "10_recall_topk.txt",
        args.qid,
    )
    # 12-15 openclaw conv artifacts (events / session manifest / internal log / metrics)
    oc_conv_dir = _find_openclaw_conv_dir(args.results_root, args.system, args.run_name, qa.conv)
    _dump_openclaw_events(out_dir / "12_openclaw_events.json", oc_conv_dir, args.qid)
    _dump_session_manifest_evidence(out_dir / "13_session_manifest_evidence.json", oc_conv_dir, qa.evidence_turns)
    _dump_openclaw_internal_log(out_dir / "14_openclaw_internal.log", oc_conv_dir)
    _dump_openclaw_metrics(out_dir / "15_openclaw_metrics.json", oc_conv_dir)


# ── Section helpers ───────────────────────────────────────────────────────────

def _write_summary(path: Path, qid: str, qa, eval_result, window: WindowSpec) -> None:
    judgments = eval_result.judgments or {}
    lines = [
        f"qid: {qid}",
        f"conv: {qa.conv}",
        f"category: {qa.category}",
        f"question: {qa.question}",
        f"golden_answer: {qa.golden}",
        f"judge.is_correct: {eval_result.correct}",
        f"judge.llm_judgments: {judgments}",
        f"generated_answer: {eval_result.generated}",
        f"window_source: {window.source}  ({window.start} -> {window.end})",
    ]
    path.write_text("\n".join(lines) + "\n")


def _raw_dataset_record(dataset_path: Path, conv: str, qid: str) -> dict:
    """Return the raw qa object plus sample_id metadata for traceability."""
    data = json.loads(Path(dataset_path).read_text())
    conv_pos = int(conv.split("_", 1)[1])
    qid_idx = int(qid.rsplit("_qa", 1)[1])
    sample = data[conv_pos] if 0 <= conv_pos < len(data) else None
    if sample is None:
        return {}
    return {
        "dataset_index": conv_pos,
        "sample_id_in_dataset": sample.get("sample_id"),
        "conv_id_eval_side": conv,
        "qid_idx": qid_idx,
        "qa": sample["qa"][qid_idx],
    }


def _evidence_turn_to_dict(t: EvidenceTurn) -> dict:
    return {
        "session_idx": t.session_idx,
        "dia_id": t.dia_id,
        "speaker": t.speaker,
        "text": t.text,
        "session_date_time": t.timestamp,
    }


def _dump_ovdata_candidates(
    path: Path, ovdata_root: Path, conv: str, evidence_turns: list[EvidenceTurn],
) -> None:
    """Find candidate golden md files in ovdata and copy contents verbatim."""
    if not ovdata_root.exists():
        path.write_text(f"# ovdata root not found: {ovdata_root}\n")
        return
    mem_root = ovdata_root / "viking" / "default" / "user" / conv / "memories"
    if not mem_root.exists():
        # Try alternative ovdata layouts
        for cand in ovdata_root.rglob(f"user/{conv}/memories"):
            mem_root = cand
            break
    if not mem_root.exists():
        path.write_text(f"# memories dir not found under {ovdata_root}\n")
        return

    phrases = _evidence_phrases(evidence_turns)
    hits: list[tuple[Path, str]] = []
    for md in mem_root.rglob("*.md"):
        try:
            content = md.read_text(errors="replace")
        except OSError:
            continue
        if any(p.lower() in content.lower() for p in phrases):
            hits.append((md, content))
            if len(hits) >= OVDATA_FILE_CAP:
                break

    if not hits:
        path.write_text(f"# no .md under {mem_root} matches evidence phrases: {phrases!r}\n")
        return

    body: list[str] = []
    for md_path, content in hits:
        uri = _md_to_uri(md_path, mem_root, conv)
        body.append(f"# === {uri} === ({md_path})")
        snippet = content if len(content) <= OVDATA_FILE_BYTES_CAP else content[:OVDATA_FILE_BYTES_CAP] + "\n# ... (truncated)"
        body.append(snippet)
        body.append("")
    path.write_text("\n".join(body) + "\n")


def _evidence_phrases(evidence_turns: list[EvidenceTurn]) -> list[str]:
    phrases: list[str] = []
    for t in evidence_turns:
        text = (t.text or "").strip()
        if text:
            phrases.append(text[:40])
    return phrases or ["__no_evidence__"]


def _md_to_uri(md_path: Path, mem_root: Path, conv: str) -> str:
    rel = md_path.relative_to(mem_root)
    return f"viking://user/{conv}/memories/{rel.as_posix()}"


def _dump_ingest_lines(
    path: Path, ov_log: Path, conv: str, evidence_turns: list[EvidenceTurn],
) -> None:
    """Grep ov-server.log for lines naming the conv + key evidence phrase or 'Enqueued'."""
    if not ov_log.exists():
        path.write_text(f"# ov_log not found: {ov_log}\n")
        return
    phrases = _evidence_phrases(evidence_turns)
    needles = [conv, *phrases]
    needles_lower = [n.lower() for n in needles]
    hits: list[str] = []
    with ov_log.open(errors="replace") as f:
        for line in f:
            ll = line.lower()
            if conv.lower() not in ll:
                continue
            if "enqueued" not in ll and "upsert" not in ll:
                continue
            if not any(n in ll for n in needles_lower):
                continue
            hits.append(_strip_vector_arrays(line.rstrip("\n")))
            if len(hits) >= INGEST_LINE_CAP:
                break
    if not hits:
        path.write_text(f"# no ingest lines found for {conv} matching {phrases!r}\n")
        return
    path.write_text("\n".join(hits) + "\n")


def _dump_archive_lines(
    path: Path, ov_log: Path, conv: str, qid: str, window: WindowSpec,
) -> None:
    """task_tracker + commit_session lines attributed to this qid.

    Primary filter is the ``[qid={qid}]`` tag stamped by the OV server's
    plain-debug logger after feat/qid-tagging. Window is kept only as a
    secondary sanity bound — it does NOT widen row inclusion. For legacy
    pre-tagging logs, enable ``_LEGACY_LOG_COMPAT`` at module top to fall
    back to conv-substring matching (which is the OLD leaky behavior, kept
    only for historical diagnostics).
    """
    legacy_active = _LEGACY_LOG_COMPAT
    header = [
        f"# qid filter: [qid={qid}]",
        f"# window (sanity, not exclusive): {window.start} -> {window.end}",
        "# loggers tracked: task_tracker, session.session [TRACER]",
    ]
    if legacy_active:
        header.append(
            "# WARN: _LEGACY_LOG_COMPAT=True — falling back to conv match for "
            "rows that pre-date qid tagging; lines without [qid=] are "
            "conv-leaked and NOT exclusive to this qid."
        )

    lines_out: list[str] = []
    if not ov_log.exists():
        header.append(f"# ov_log not found: {ov_log}")
    else:
        try:
            with ov_log.open("r", errors="replace") as f:
                for line in f:
                    # Sanity window check: when bounds are known, skip lines
                    # clearly outside. When window is unavailable, skip the
                    # timestamp check entirely so qid filter is sole scoping.
                    if window.start is not None and window.end is not None:
                        ts = _parse_ts(line)
                        if ts is None:
                            continue
                        if not (window.start <= ts <= window.end):
                            continue
                    # Logger include: task_tracker activity or TRACER commit traces
                    if (
                        "task_tracker" not in line
                        and "TaskTracker" not in line
                        and "[TRACER]" not in line
                    ):
                        continue
                    # qid-grep primary filter
                    if not _match_qid(line, qid, conv):
                        continue
                    lines_out.append(line.rstrip("\n"))
                    if len(lines_out) >= ARCHIVE_LINE_CAP:
                        break
        except OSError as e:
            header.append(f"# ov_log open error: {e}")

    body = "\n".join(header + [""] + lines_out)
    path.write_text(body + "\n")


def _dump_session_record(path: Path, session_jsonl: Optional[Path], qid: str) -> None:
    if not session_jsonl or not session_jsonl.exists():
        path.write_text(json.dumps({"note": "no session jsonl present for this run"}, indent=2) + "\n")
        return
    qid_idx = int(qid.rsplit("_qa", 1)[1])
    # Per-QA files (qa<N>.jsonl) have just one user message at index 0.
    # Legacy single-jsonl files use the real qid_idx.
    msg_idx = 0 if session_jsonl.name == f"qa{qid_idx}.jsonl" else qid_idx
    try:
        turn = find_user_message(session_jsonl, msg_idx)
    except (IndexError, KeyError) as e:
        path.write_text(json.dumps({"error": str(e)}, indent=2) + "\n")
        return
    record = {
        "user_message_text": turn.text,
        "user_message_unix_ts_ms": turn.unix_ts_ms,
        "injected_bullets": [
            {"chars": b.chars, "abstract_head": b.abstract_head, "text": b.text}
            for b in turn.injected_bullets
        ],
        "assistant_text": turn.assistant_text,
        "assistant_thinking": turn.assistant_thinking,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")


_RECALL_LOGGERS = (
    "hierarchical_retriever",
    "viking_vector_index_backend",   # vector store query backend
    "openai_rerank",                 # rerank model
    "openai_embedders",              # query embed
    "telemetry.execution",           # Telemetry summary lines
)


def _dump_recall_lines(
    path: Path, ov_log: Path, conv: str, qid: str, window: WindowSpec,
) -> None:
    """Stream every recall-stage log row attributed to this qid via [qid=...] tag.

    Window is kept ONLY as a secondary sanity-check filter (bounds the linear
    scan; does NOT widen the row inclusion). For new runs every selected line
    carries ``[qid={qid}]``; for legacy logs (pre-feat/qid-tagging) the
    ``_LEGACY_LOG_COMPAT`` fallback can be enabled to pass-through conv-matched
    lines — the file will then carry an explicit deprecation header.

    Caps at RECALL_LINE_CAP lines with a truncation marker.
    """
    legacy_active = _LEGACY_LOG_COMPAT
    header = [
        f"# qid filter: [qid={qid}]",
        f"# window (sanity, not exclusive): {window.start} -> {window.end}",
        f"# loggers tracked: {', '.join(_RECALL_LOGGERS)}",
        f"# line cap: {RECALL_LINE_CAP}",
    ]
    if legacy_active:
        header.append(
            "# WARN: _LEGACY_LOG_COMPAT=True — falling back to conv match for "
            "rows that pre-date qid tagging; lines without [qid=] are "
            "conv-leaked and NOT exclusive to this qid."
        )

    lines_out: list[str] = []
    truncated = False
    if not ov_log.exists():
        header.append(f"# ov_log not found: {ov_log}")
    else:
        try:
            with ov_log.open("r", errors="replace") as f:
                for line in f:
                    # Sanity window check (skipped when bounds unavailable)
                    if window.start is not None and window.end is not None:
                        ts = _parse_ts(line)
                        if ts is None:
                            continue
                        if not (window.start <= ts <= window.end):
                            continue
                    # Logger include filter
                    if not any(needle in line for needle in _RECALL_LOGGERS):
                        continue
                    # qid-grep primary filter
                    if not _match_qid(line, qid, conv):
                        continue
                    lines_out.append(_strip_vector_arrays(line.rstrip("\n")))
                    if len(lines_out) >= RECALL_LINE_CAP:
                        truncated = True
                        break
        except OSError as e:
            header.append(f"# ov_log open error: {e}")

    body = "\n".join(header + [""] + lines_out)
    if truncated:
        body += f"\n# ... truncated at {RECALL_LINE_CAP} lines (raise RECALL_LINE_CAP to see more)"
    path.write_text(body + "\n")


def _dump_recall_topk(path: Path, ov_log: Path, conv: str, window: WindowSpec) -> None:
    """Extract structured vector_topk + rerank_topk tables from the recall window.

    Walks the same window as 07_log_recall, but parses each vector / rerank /
    telemetry line into rows and prints two aligned tables. Quick way to see
    'did golden show up in vector top? in rerank top? what score?' without
    scanning 900 raw log lines.

    Note: 10_recall_topk continues to use conv-substring matching plus the
    sanity window because the vector/rerank rows are emitted as multi-line
    blocks by recursive search, and the [qid=...] tag sits only on the
    enclosing header line — parsing structured rows still needs the full
    block. This file is a *visualization* of 07_log_recall, not an
    additional source of truth.
    """
    if not ov_log.exists():
        path.write_text(f"# ov_log not found: {ov_log}\n")
        return

    vector_rows: list[tuple[int, str, float, int]] = []
    rerank_rows: list[tuple[str, float]] = []
    telemetry_returns: list[int] = []

    with ov_log.open(errors="replace") as f:
        for line in f:
            if window.start is not None and window.end is not None:
                ts = _parse_ts(line)
                if ts is None:
                    continue
                if not (window.start <= ts <= window.end):
                    continue
            if conv not in line and "Telemetry summary" not in line:
                continue
            mv = _VEC_ROW_RE.search(line)
            if mv:
                vector_rows.append((int(mv.group(1)), mv.group(2), float(mv.group(3)), int(mv.group(4))))
                continue
            mr = _RERANK_ROW_RE.search(line)
            if mr:
                rerank_rows.append((mr.group(1), float(mr.group(2))))
                continue
            if "Telemetry summary" in line:
                mt = _RETURNED_RE.search(line)
                if mt:
                    telemetry_returns.append(int(mt.group(1)))

    out: list[str] = []
    out.append(f"# window: {window.start} -> {window.end}  ({window.source})")
    out.append(f"# conv filter: {conv}")
    out.append("")
    out.append("=== TELEMETRY ===")
    if telemetry_returns:
        out.append(f"search.find returned counts in window: {telemetry_returns}")
    else:
        out.append("no Telemetry summary found in window")
    out.append("")
    out.append(f"=== VECTOR TOPK  ({len(vector_rows)} rows) ===")
    out.append(f"{'rank':>4}  {'score':>7}  {'L':>1}  uri")
    for rank, uri, score, level in vector_rows:
        out.append(f"{rank:>4}  {score:>7.4f}  {level:>1}  {uri}")
    out.append("")
    out.append(f"=== RERANK CANDIDATES  ({len(rerank_rows)} rows) ===")
    out.append("# rerank scores from openai_rerank model; rank reflects emit order")
    out.append(f"{'rk':>3}  {'score':>7}  uri")
    # Sort rerank rows by score descending for analysis
    rerank_sorted = sorted(enumerate(rerank_rows), key=lambda x: -x[1][1])
    for new_rank, (emit_idx, (uri, score)) in enumerate(rerank_sorted, 1):
        out.append(f"{new_rank:>3}  {score:>7.4f}  {uri}")
    path.write_text("\n".join(out) + "\n")


def _dump_picking_analysis(
    path: Path,
    session_jsonl: Optional[Path],
    ovdata_root: Path,
    conv: str,
    recall_topk_path: Path,
    qid: str,
) -> None:
    """Cross-reference injected bullets against vector/rerank scores.

    Reverse-lookup each picked bullet's abstract_head against .ovdata to get
    its URI, then look that URI up in 10_recall_topk to find vector rank,
    vector score, rerank score.
    """
    if not session_jsonl or not session_jsonl.exists():
        path.write_text(
            "# session jsonl missing — cannot reverse picked bullets to URIs\n"
        )
        return

    qid_idx = int(qid.rsplit("_qa", 1)[1])
    msg_idx = 0 if session_jsonl.name == f"qa{qid_idx}.jsonl" else qid_idx
    try:
        turn = find_user_message(session_jsonl, msg_idx)
    except (IndexError, KeyError) as e:
        path.write_text(f"# could not load session turn: {e}\n")
        return

    bullets = turn.injected_bullets
    if not bullets:
        path.write_text("# no injected bullets in this turn\n")
        return

    # Build URI -> .md content map for reverse lookup
    mem_root = ovdata_root / "viking" / "default" / "user" / conv / "memories"
    if not mem_root.exists():
        for cand in ovdata_root.rglob(f"user/{conv}/memories"):
            mem_root = cand
            break
    if not mem_root.exists():
        path.write_text(f"# memories dir not found under {ovdata_root}\n")
        return

    # Parse 10_recall_topk for URI -> (vec_rank, vec_score, level) and URI -> rerank_score
    vec_index: dict[str, tuple[int, float, int]] = {}
    rerank_index: dict[str, float] = {}
    if recall_topk_path.exists():
        section = None
        for line in recall_topk_path.read_text().splitlines():
            if line.startswith("=== VECTOR"):
                section = "vector"
                continue
            if line.startswith("=== RERANK"):
                section = "rerank"
                continue
            if line.startswith("===") or not line.strip() or line.startswith("#") or line.startswith("rank") or line.startswith(" rk"):
                continue
            parts = line.split()
            if section == "vector" and len(parts) >= 4:
                try:
                    vec_index[parts[3]] = (int(parts[0]), float(parts[1]), int(parts[2]))
                except (ValueError, IndexError):
                    pass
            elif section == "rerank" and len(parts) >= 3:
                try:
                    rerank_index[parts[2]] = float(parts[1])
                except (ValueError, IndexError):
                    pass

    # Reverse picked bullets to URIs
    picked: list[dict] = []
    md_cache: list[tuple[Path, str]] = []
    for md in mem_root.rglob("*.md"):
        try:
            md_cache.append((md, md.read_text(errors="replace")))
        except OSError:
            pass

    for i, b in enumerate(bullets):
        head = b.abstract_head[:50]
        matched_uri = None
        for md, content in md_cache:
            if head in content:
                rel = md.relative_to(mem_root)
                matched_uri = f"viking://user/{conv}/memories/{rel.as_posix()}"
                break
        picked.append({
            "i": i, "chars": b.chars, "uri": matched_uri,
            "vec": vec_index.get(matched_uri or "_"),
            "rerank": rerank_index.get(matched_uri or "_"),
            "head": head,
        })

    # Render
    out: list[str] = []
    total_chars = sum(b.chars for b in bullets)
    out.append("=== PICKED 5 BULLETS — REVERSE LOOKUP ===")
    out.append(f"injected total chars: {total_chars}  (plugin budget typical 4000)")
    out.append(f"budget usage: {100 * total_chars / 4000:.1f}%")
    out.append("")
    out.append(f"{'#':>2}  {'chars':>5}  {'vec_rk':>6}  {'vec_sc':>7}  {'rerank':>7}  uri")
    for p in picked:
        vec = p["vec"]
        vec_rk = f"{vec[0]}" if vec else "—"
        vec_sc = f"{vec[1]:.4f}" if vec else "—"
        rerank = f"{p['rerank']:.4f}" if p["rerank"] is not None else "—"
        uri_disp = p["uri"] or f"(unresolved: {p['head'][:30]}...)"
        out.append(f"{p['i']:>2}  {p['chars']:>5}  {vec_rk:>6}  {vec_sc:>7}  {rerank:>7}  {uri_disp}")

    # Show all rerank candidates with picked / not-picked annotation
    picked_uris = {p["uri"] for p in picked if p["uri"]}
    out.append("")
    out.append("=== ALL RERANK CANDIDATES vs PICKED (sorted by rerank score) ===")
    out.append("# this is the key view: rerank order top-down, picked marked")
    out.append(f"{'rk':>3}  {'rerank':>7}  picked  uri")
    for rk, (uri, rerank_score) in enumerate(
        sorted(rerank_index.items(), key=lambda x: -x[1]), 1
    ):
        mark = "  ✓  " if uri in picked_uris else "  ·  "
        out.append(f"{rk:>3}  {rerank_score:>7.4f}  {mark}  {uri}")
    path.write_text("\n".join(out) + "\n")



def _eval_record_raw(eval_results_path: Path, qid: str) -> dict | None:
    if not eval_results_path.exists():
        return None
    data = json.loads(eval_results_path.read_text())
    detailed = data.get("detailed_results")
    if isinstance(detailed, dict):
        for user, recs in detailed.items():
            for r in recs or []:
                if r.get("question_id") == qid:
                    return {"user": user, **r}
    for r in data.get("results") or []:
        if r.get("question_id") == qid:
            return r
    return None


def _answer_record(answer_results_path: Optional[Path], qid: str) -> dict | None:
    if not answer_results_path or not answer_results_path.exists():
        return None
    data = json.loads(answer_results_path.read_text())
    if isinstance(data, list):
        for r in data:
            if r.get("question_id") == qid:
                return r
    return None


# ── Window inference ─────────────────────────────────────────────────────────

def _infer_window(
    args: CLIArgs,
    conv: str,
    session_jsonl: Optional[Path],
    answer_record: Optional[dict],
) -> WindowSpec:
    """Compute exact ``[user_ts, user_ts + answer_latency_ms]`` bounds.

    No padding. No log-extent fallback. No heuristic widening. When session
    jsonl or ``answer_latency_ms`` is missing, return a spec with
    ``source='unavailable_*'`` and ``start=end=None`` — the caller writes
    09_window.txt accordingly and the line scanners do not apply
    window-bounded checks (qid filter is still primary).
    """
    if session_jsonl is None or not session_jsonl.exists():
        return WindowSpec(
            source="unavailable_no_session_jsonl",
            start=None, end=None,
            user_ts_ms=None, answer_latency_ms=None,
        )

    try:
        qid_idx = int(args.qid.rsplit("_qa", 1)[1])
    except (IndexError, ValueError) as e:
        return WindowSpec(
            source=f"unavailable_session_parse_error:{e}",
            start=None, end=None,
            user_ts_ms=None, answer_latency_ms=None,
        )

    msg_idx = 0 if session_jsonl.name == f"qa{qid_idx}.jsonl" else qid_idx
    try:
        turn = find_user_message(session_jsonl, msg_idx)
    except (IndexError, KeyError, ValueError) as e:
        return WindowSpec(
            source=f"unavailable_session_parse_error:{type(e).__name__}",
            start=None, end=None,
            user_ts_ms=None, answer_latency_ms=None,
        )

    user_ts_ms = turn.unix_ts_ms
    if user_ts_ms is None or user_ts_ms <= 0:
        return WindowSpec(
            source="unavailable_no_user_ts",
            start=None, end=None,
            user_ts_ms=None, answer_latency_ms=None,
        )

    latency_ms = None
    if answer_record:
        meta = answer_record.get("metadata") or {}
        latency_ms = meta.get("answer_latency_ms")
    if not isinstance(latency_ms, (int, float)) or latency_ms <= 0:
        return WindowSpec(
            source="unavailable_no_latency_in_answer_results",
            start=None, end=None,
            user_ts_ms=user_ts_ms, answer_latency_ms=None,
        )

    start_dt = datetime.fromtimestamp(user_ts_ms / 1000.0)
    end_dt = datetime.fromtimestamp((user_ts_ms + latency_ms) / 1000.0)
    return WindowSpec(
        source="session_jsonl_exact",
        start=start_dt, end=end_dt,
        user_ts_ms=user_ts_ms, answer_latency_ms=float(latency_ms),
    )


def _render_window(w: WindowSpec) -> str:
    """Render 09_window.txt contents.

    When bounds are known, includes the exact ISO timestamps and
    ``answer_latency_ms``. When unavailable, replaces the note with the
    explicit fact that the qid tag is the sole scoping mechanism.
    """
    user_ts_iso = (
        datetime.fromtimestamp(w.user_ts_ms / 1000.0).isoformat()
        if w.user_ts_ms else "unavailable"
    )
    latency_disp = (
        f"{w.answer_latency_ms}" if w.answer_latency_ms is not None else "unavailable"
    )
    start_disp = w.start.isoformat() if w.start else "unavailable"
    end_disp = w.end.isoformat() if w.end else "unavailable"

    if w.start is not None and w.end is not None:
        note = (
            "exact bounds [user_ts, user_ts + answer_latency_ms]; "
            "no padding applied"
        )
    else:
        note = (
            "window unavailable — line-level [qid=...] filter is the sole "
            "scoping mechanism"
        )

    lines = [
        f"source: {w.source}",
        f"user_ts: {user_ts_iso}",
        f"answer_latency_ms: {latency_disp}",
        f"window_start: {start_disp}",
        f"window_end: {end_disp}",
        f"note: {note}",
    ]
    return "\n".join(lines)


def _parse_ts(line: str) -> Optional[datetime]:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


# ── openclaw conv artifact helpers ────────────────────────────────────────────

def _find_openclaw_conv_dir(
    results_root: Path,
    system: str,
    run_name: str,
    conv: str,
) -> Optional[Path]:
    """Locate the per-conv openclaw artifact directory.

    Path:
        <results_root>/locomo-<system>-<run_name>/artifacts/openclaw/run-*/conversations/<conv>/
    """
    run_dir = results_root / f"locomo-{system}-{run_name}"
    matches = sorted(run_dir.glob(
        f"artifacts/openclaw/run-*/conversations/{conv}"
    ))
    return matches[0] if matches else None


def _dump_openclaw_events(
    path: Path, oc_conv_dir: Optional[Path], qid: str,
) -> None:
    """events.jsonl filtered to events mentioning the qid + all conv-wide events.

    Returns a JSON object with two keys:
      - per_qid: events whose 'question_id' == qid (per-qa errors, etc.)
      - conv_wide: events with no 'question_id' (ingest_mode_session_bundle,
        ov_session_opened, ov_sdk_ingest_complete, etc. — these are conv-level
        markers that contextualize the per-qa events.)
    """
    if not oc_conv_dir or not (oc_conv_dir / "events.jsonl").exists():
        path.write_text(
            json.dumps({"note": "events.jsonl not found", "oc_conv_dir": str(oc_conv_dir)}) + "\n"
        )
        return
    per_qid: list[dict] = []
    conv_wide: list[dict] = []
    try:
        with (oc_conv_dir / "events.jsonl").open(errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("question_id") == qid:
                    per_qid.append(e)
                elif "question_id" not in e:
                    conv_wide.append(e)
    except OSError as ex:
        path.write_text(json.dumps({"error": str(ex)}) + "\n")
        return
    path.write_text(json.dumps(
        {"per_qid": per_qid, "conv_wide": conv_wide},
        ensure_ascii=False, indent=2,
    ) + "\n")


def _dump_session_manifest_evidence(
    path: Path, oc_conv_dir: Optional[Path], evidence_turns: list[EvidenceTurn],
) -> None:
    """Map each evidence dia_id (e.g. D2:1) to its session_id from session_manifest.

    Lets the reader trace which openclaw session bundle covered a given
    evidence turn. The session_id is the openclaw-internal label (S1, S2, …),
    raw_session_key is the dataset's session_2 / session_20 / etc.
    """
    if not oc_conv_dir or not (oc_conv_dir / "session_manifest.json").exists():
        path.write_text(
            json.dumps({"note": "session_manifest.json not found"}) + "\n"
        )
        return
    try:
        manifest = json.loads((oc_conv_dir / "session_manifest.json").read_text())
    except (json.JSONDecodeError, OSError) as ex:
        path.write_text(json.dumps({"error": str(ex)}) + "\n")
        return
    sessions = manifest.get("sessions", [])
    # Build dia_id → session lookup
    dia_to_session: dict[str, dict] = {}
    for s in sessions:
        for dia_id in s.get("source_message_ids", []) or []:
            dia_to_session[dia_id] = {
                "session_id": s.get("session_id"),
                "raw_session_key": s.get("raw_session_key"),
                "session_message_count": len(s.get("source_message_ids", []) or []),
            }
    matched: list[dict] = []
    for t in evidence_turns:
        entry = dia_to_session.get(t.dia_id)
        matched.append({
            "dia_id": t.dia_id,
            "speaker": t.speaker,
            "text_head": (t.text or "")[:120],
            "session_match": entry,
        })
    path.write_text(json.dumps({
        "schema_version": manifest.get("schema_version"),
        "total_sessions": len(sessions),
        "total_messages": len(manifest.get("messages", [])),
        "evidence_to_session": matched,
    }, ensure_ascii=False, indent=2) + "\n")


def _dump_openclaw_internal_log(
    path: Path, oc_conv_dir: Optional[Path],
) -> None:
    """openclaw internal log entries flattened to (time, level, parent, message) tuples."""
    if not oc_conv_dir:
        path.write_text("# oc_conv_dir not found\n")
        return
    log_glob = list((oc_conv_dir / ".openclaw-container-tmp" / "openclaw").glob("openclaw-*.log"))
    if not log_glob:
        path.write_text("# no openclaw internal log under .openclaw-container-tmp/openclaw/\n")
        return
    out: list[str] = [f"# source: {log_glob[0]}"]
    try:
        with log_glob[0].open(errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    out.append(line[:300])
                    continue
                ts = e.get("time", "?")
                meta = e.get("_meta", {})
                level = meta.get("logLevelName", "?")
                parents = meta.get("parentNames", [])
                parent_label = ".".join(parents) if parents else "root"
                msg = e.get("1") or e.get("0") or ""
                # truncate massive JSON blobs in message
                if isinstance(msg, str):
                    msg_display = msg if len(msg) <= 500 else msg[:500] + "...[truncated]"
                else:
                    msg_display = json.dumps(msg, ensure_ascii=False)[:500]
                out.append(f"{ts} [{level}] [{parent_label}] {msg_display}")
    except OSError as ex:
        out.append(f"# read error: {ex}")
    path.write_text("\n".join(out) + "\n")


def _dump_openclaw_metrics(
    path: Path, oc_conv_dir: Optional[Path],
) -> None:
    """Bundle openclaw metrics/* + handle.json into a single JSON for quick inspection."""
    if not oc_conv_dir:
        path.write_text(json.dumps({"note": "oc_conv_dir not found"}) + "\n")
        return
    bundle: dict = {"conv_dir": str(oc_conv_dir)}
    # handle.json (run config)
    handle = oc_conv_dir / "handle.json"
    if handle.exists():
        try:
            bundle["handle"] = json.loads(handle.read_text())
        except (json.JSONDecodeError, OSError) as ex:
            bundle["handle_error"] = str(ex)
    # metrics/*.json
    metrics_dir = oc_conv_dir / "metrics"
    if metrics_dir.is_dir():
        bundle["metrics"] = {}
        for m in sorted(metrics_dir.glob("*.json")):
            try:
                bundle["metrics"][m.name] = json.loads(m.read_text())
            except (json.JSONDecodeError, OSError) as ex:
                bundle["metrics"][m.name] = {"_error": str(ex)}
    # state/agents/main/sessions/sessions.json (one-level)
    sess = oc_conv_dir / "state" / "agents" / "main" / "sessions" / "sessions.json"
    if sess.exists():
        try:
            sess_data = json.loads(sess.read_text())
            # Compact: only keys + sample
            bundle["agent_sessions"] = {
                "keys": list(sess_data.keys()),
                "count": len(sess_data),
            }
        except (json.JSONDecodeError, OSError) as ex:
            bundle["agent_sessions_error"] = str(ex)
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
