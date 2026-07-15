import json
import re
from dataclasses import dataclass
from pathlib import Path

@dataclass
class InjectedBullet:
    abstract_head: str  # first 80 chars
    text: str
    chars: int

@dataclass
class AgentTurn:
    text: str
    unix_ts_ms: int
    injected_bullets: list[InjectedBullet]
    assistant_text: str | None
    assistant_thinking: str | None

_BULLET_RE = re.compile(r"^- \[\] ", re.MULTILINE)

def find_user_message(jsonl_path: Path, qid_idx: int) -> AgentTurn:
    """Locate qa N-th user turn in the session jsonl."""
    user_count = -1
    user_rec = None
    assistant_rec = None
    with jsonl_path.open() as f:
        recs = [json.loads(l) for l in f if l.strip()]
    for i, r in enumerate(recs):
        if r.get("type") == "message" and r.get("message",{}).get("role") == "user":
            user_count += 1
            if user_count == qid_idx:
                user_rec = r
                for j in range(i+1, len(recs)):
                    if recs[j].get("type") == "message" and recs[j].get("message",{}).get("role") == "assistant":
                        assistant_rec = recs[j]
                        break
                break
    if not user_rec:
        raise IndexError(qid_idx)
    text = user_rec["message"]["content"][0]["text"]
    bullets = _parse_bullets(text)
    ts_ms = user_rec["message"]["timestamp"]
    return AgentTurn(
        text=text, unix_ts_ms=ts_ms,
        injected_bullets=bullets,
        assistant_text=_extract_assistant_text(assistant_rec),
        assistant_thinking=_extract_thinking(assistant_rec),
    )

def _parse_bullets(text: str):
    if "<relevant-memories>" not in text:
        return []
    start = text.index("relevant:\n") + len("relevant:\n")
    end = text.index("\n</relevant-memories>")
    block = text[start:end]
    parts = _BULLET_RE.split(block)
    bullets = []
    for p in parts:
        p = p.strip()
        if not p: continue
        body = p
        bullets.append(InjectedBullet(
            abstract_head=body[:80], text=body, chars=len(body),
        ))
    return bullets

def _extract_assistant_text(rec):
    if not rec: return None
    for c in rec["message"]["content"]:
        if c.get("type") == "text":
            return c.get("text")
    return None

def _extract_thinking(rec):
    if not rec: return None
    for c in rec["message"]["content"]:
        if c.get("type") == "thinking":
            return c.get("thinking")
    return None
