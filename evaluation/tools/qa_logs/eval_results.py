import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class EvalResult:
    generated: str
    correct: bool
    latency_ms: int | None
    judgments: dict | None = None


def _iter_records(data: dict):
    """Yield each per-question record from either schema:
    - new: data["detailed_results"] is dict keyed by user, each value a list
    - legacy/fixture: data["results"] is a flat list
    """
    if isinstance(data, dict) and isinstance(data.get("detailed_results"), dict):
        for _user, recs in data["detailed_results"].items():
            yield from (recs or [])
        return
    yield from (data.get("results") or [])


def _record_is_correct(r: dict) -> bool:
    if "is_correct" in r:
        return bool(r["is_correct"])
    return bool(r.get("judge", {}).get("correct", False))


def load_eval_result(path: Path, qid: str) -> EvalResult:
    data = json.loads(Path(path).read_text())
    for r in _iter_records(data):
        if r.get("question_id") == qid:
            return EvalResult(
                generated=r.get("generated_answer") or r.get("answer", ""),
                correct=_record_is_correct(r),
                latency_ms=r.get("latency_ms"),
                judgments=r.get("llm_judgments"),
            )
    raise KeyError(qid)


def list_wrong_qids(path: Path) -> list[str]:
    """For default --qid=all_errors mode: return every wrong qid in this run."""
    data = json.loads(Path(path).read_text())
    return [r["question_id"] for r in _iter_records(data) if not _record_is_correct(r)]
