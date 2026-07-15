import json
import re
from dataclasses import dataclass
from pathlib import Path

@dataclass
class EvidenceTurn:
    session_idx: int
    dia_id: str
    speaker: str
    text: str
    timestamp: str | None

@dataclass
class QAFields:
    conv: str
    qid: str
    question: str
    golden: str
    category: int
    evidence_turns: list[EvidenceTurn]

_QID_RE = re.compile(r"^(?P<conv>locomo_\d+)_qa(?P<idx>\d+)$")

def load_qa(dataset_path: Path, qid: str) -> QAFields:
    m = _QID_RE.match(qid)
    if not m:
        raise ValueError(f"bad qid: {qid}")
    conv, idx = m.group("conv"), int(m.group("idx"))
    conv_pos = int(conv.split("_", 1)[1])
    data = json.loads(Path(dataset_path).read_text())
    # eval framework derives conv_id by position: data[i] -> f"{dataset}_{i}".
    # Fall back to sample_id match for test fixtures that set sample_id=conv.
    if 0 <= conv_pos < len(data) and (
        data[conv_pos].get("sample_id") in (conv, None)
        or not any(s.get("sample_id") == conv for s in data)
    ):
        sample = data[conv_pos]
    else:
        sample = next(s for s in data if s.get("sample_id") == conv)
    qa = sample["qa"][idx]
    evid_turns = _resolve_evidence(sample.get("conversation", {}), qa.get("evidence", []))
    return QAFields(conv=conv, qid=qid,
                    question=qa["question"], golden=str(qa.get("answer", "")),
                    category=int(qa.get("category", -1)),
                    evidence_turns=evid_turns)

def _resolve_evidence(conv_dict, evidence_ids):
    turns = []
    for ev in evidence_ids:
        sess_part, dia_part = ev.split(":", 1) if ":" in ev else (ev, "")
        sess_idx = int(sess_part.lstrip("D"))
        session_turns = conv_dict.get(f"session_{sess_idx}", [])
        session_ts = conv_dict.get(f"session_{sess_idx}_date_time")
        for t in session_turns:
            if t.get("dia_id", "").endswith(f":{dia_part}") or t.get("dia_id") == ev:
                turns.append(EvidenceTurn(
                    session_idx=sess_idx, dia_id=t.get("dia_id", ev),
                    speaker=t.get("speaker", ""), text=t.get("text", ""),
                    timestamp=session_ts,
                ))
    return turns
