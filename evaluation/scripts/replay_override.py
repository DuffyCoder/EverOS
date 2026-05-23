"""Offline replay: simulate alternative override strategies against captured archive sessions.

For each QA in a smoke3 archive:
  - Locate the per-QA session jsonl (under conversations/<conv>/state/agents/main/sessions/)
  - Extract ordered asst emissions (text content of each role=assistant message, w/ has_tool flag)
  - For each strategy, compute what `answer` it would have produced given (bridge_reply, emissions)
  - Score: substring(gold, simulated_answer.lower()) as cheap heuristic; mark "differs" vs captured

bridge_reply is reconstructed as emissions[0]["text"] — for V2 single-emission this equals captured;
for V1 multi-emission, captured may equal emissions[-1] (if prod override fired) or emissions[0] (if it didn't).

Usage:
  python evaluation/scripts/replay_override.py <archive_root>
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


# ---------- archive loading ----------

def load_archive(archive_root: Path) -> dict:
    results_dirs = list((archive_root / "results").iterdir())
    assert len(results_dirs) == 1, f"expected 1 results dir, got {results_dirs}"
    rdir = results_dirs[0]
    answers = json.load(open(rdir / "answer_results.json"))
    sess_dirs: Dict[str, Path] = {}
    run_dirs = list((rdir / "artifacts/openclaw").glob("run-*"))
    for run_dir in run_dirs:
        for conv_dir in (run_dir / "conversations").iterdir():
            sp = conv_dir / "state/agents/main/sessions"
            if sp.exists():
                sess_dirs[conv_dir.name] = sp
    return {"answers": answers, "sess_dirs": sess_dirs, "results_dir": rdir}


def index_sessions_by_question(sess_dir: Path) -> Dict[str, Path]:
    """Scan all session jsonl files in sess_dir, return {last_user_text: path}.

    Multiple files may share the same user text if a question was re-run; in that case
    the most recently modified file wins (matches adapter latest-mtime behavior).
    """
    out: Dict[str, Tuple[float, Path]] = {}
    for f in sess_dir.iterdir():
        if not f.is_file():
            continue
        try:
            last_user_text: Optional[str] = None
            for line in open(f):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "message":
                    continue
                m = d.get("message") or {}
                if m.get("role") != "user":
                    continue
                content = m.get("content")
                text = ""
                if isinstance(content, list):
                    for c in content:
                        if c.get("type") == "text":
                            text += c.get("text", "")
                elif isinstance(content, str):
                    text = content
                if text.strip():
                    last_user_text = text.strip()
            if last_user_text is None:
                continue
            mtime = f.stat().st_mtime
            prev = out.get(last_user_text)
            if prev is None or mtime > prev[0]:
                out[last_user_text] = (mtime, f)
        except Exception:
            continue
    return {k: v[1] for k, v in out.items()}


def extract_asst_emissions(jsonl_path: Path) -> List[dict]:
    """Return list of {text, has_tool, has_thinking} for assistant messages, in order."""
    emissions = []
    for line in open(jsonl_path):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "message":
            continue
        m = d.get("message") or {}
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        text = ""
        has_tool = False
        has_thinking = False
        if isinstance(content, list):
            for c in content:
                t = c.get("type")
                if t == "text":
                    text += c.get("text", "")
                elif t in ("toolCall", "tool_use", "tool_call"):
                    has_tool = True
                elif t == "thinking":
                    has_thinking = True
        elif isinstance(content, str):
            text = content
        emissions.append({
            "text": text.strip(),
            "has_tool": has_tool,
            "has_thinking": has_thinking,
        })
    return emissions


# ---------- strategies ----------

# A strategy gets (bridge_reply, emissions) and returns final answer text.

def strat_no_override(bridge_reply: str, emissions: List[dict]) -> str:
    return bridge_reply


def strat_endswith_colon(bridge_reply: str, emissions: List[dict]) -> str:
    """Old 2f0be86: override only if bridge_reply ends with ':'."""
    if not bridge_reply.endswith(":"):
        return bridge_reply
    if len(emissions) < 2:
        return bridge_reply
    last = emissions[-1]["text"].strip()
    if last and last != bridge_reply.strip():
        return last
    return bridge_reply


def strat_emission_count(bridge_reply: str, emissions: List[dict]) -> str:
    """Current d88c570: override if ≥2 emissions and last differs from bridge_reply."""
    if (len(emissions) >= 2
            and emissions[0]["text"].strip() == bridge_reply.strip()
            and emissions[-1]["text"].strip()
            and emissions[-1]["text"].strip() != bridge_reply.strip()):
        return emissions[-1]["text"].strip()
    return bridge_reply


PREP_PATTERNS = [
    re.compile(r"^Let'?s\b", re.IGNORECASE),
    re.compile(r"^Let me\b", re.IGNORECASE),
    re.compile(r"^I'?ll\b", re.IGNORECASE),
    re.compile(r"^I will\b", re.IGNORECASE),
    re.compile(r"^Searching\b", re.IGNORECASE),
    re.compile(r"^Looking\b", re.IGNORECASE),
    re.compile(r"^Checking\b", re.IGNORECASE),
    re.compile(r"^First,?\s+(?:let|I'?ll)", re.IGNORECASE),
]


def is_prep_text(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 300:
        return False
    return any(p.match(s) for p in PREP_PATTERNS)


def strat_content_prep_text(bridge_reply: str, emissions: List[dict]) -> str:
    """Detect prep-text pattern in bridge_reply, then override from emissions[-1]."""
    if not is_prep_text(bridge_reply):
        return bridge_reply
    if len(emissions) < 2:
        return bridge_reply
    last = emissions[-1]["text"].strip()
    if last and last != bridge_reply.strip():
        return last
    return bridge_reply


def strat_last_no_tool(bridge_reply: str, emissions: List[dict]) -> str:
    """Always take the latest asst without tool call (true final). Falls back to bridge_reply."""
    for e in reversed(emissions):
        if not e["has_tool"] and e["text"]:
            return e["text"]
    return bridge_reply


def strat_content_or_short(bridge_reply: str, emissions: List[dict]) -> str:
    """Content-prep-text OR very short bridge_reply (likely truncated) → take last no-tool."""
    if is_prep_text(bridge_reply) or (len(bridge_reply.strip()) < 80 and len(emissions) >= 2):
        for e in reversed(emissions):
            if not e["has_tool"] and e["text"] and e["text"] != bridge_reply.strip():
                return e["text"]
    return bridge_reply


STRATEGIES: Dict[str, Callable[[str, List[dict]], str]] = {
    "no_override": strat_no_override,
    "endswith_colon": strat_endswith_colon,
    "emission_count": strat_emission_count,
    "content_prep_text": strat_content_prep_text,
    "last_no_tool": strat_last_no_tool,
    "content_or_short": strat_content_or_short,
}


# ---------- scoring (cheap heuristic) ----------

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().strip())


def gold_substring_score(simulated: str, gold) -> int:
    """1 if normalized(gold) is a substring of normalized(simulated), else 0.

    `gold` may be a list (LoCoMo multi-gold). Pass if ANY gold matches.
    Pure heuristic — LoCoMo judge is paraphrase-aware, so this UNDER-estimates acc.
    But the relative ranking across strategies should be stable.
    """
    if isinstance(gold, list):
        return 1 if any(gold_substring_score(simulated, g) for g in gold) else 0
    sim_n = normalize(simulated)
    gold_n = normalize(str(gold))
    if not gold_n:
        return 0
    return 1 if gold_n in sim_n else 0


# ---------- main replay ----------

def replay(archive_root: Path) -> dict:
    data = load_archive(archive_root)

    # Pre-index sessions per conv by last user message
    conv_index: Dict[str, Dict[str, Path]] = {}
    for conv, sd in data["sess_dirs"].items():
        print(f"[index] {conv}: scanning {len(list(sd.iterdir()))} files...", file=sys.stderr)
        conv_index[conv] = index_sessions_by_question(sd)
        print(f"[index] {conv}: {len(conv_index[conv])} unique user texts", file=sys.stderr)

    per_qa = []
    no_session = 0
    for qa in data["answers"]:
        qid = qa["question_id"]
        conv = qa["conversation_id"]
        q = qa["question"].strip()
        captured = qa["answer"]
        gold = qa["golden_answer"]
        cat = qa.get("category", "?")

        idx = conv_index.get(conv, {})
        # Exact match first, then endswith fallback
        sess_file = idx.get(q)
        if sess_file is None:
            for k, v in idx.items():
                if k.endswith(q) or q.endswith(k):
                    sess_file = v
                    break

        if sess_file is None:
            no_session += 1
            per_qa.append({
                "qid": qid, "cat": cat, "conv": conv, "q": q, "gold": gold,
                "captured": captured, "bridge_reply": None, "emissions": [],
                "no_session": True,
            })
            continue

        emissions = extract_asst_emissions(sess_file)
        bridge_reply = emissions[0]["text"] if emissions else captured

        per_qa.append({
            "qid": qid, "cat": cat, "conv": conv, "q": q, "gold": gold,
            "captured": captured, "bridge_reply": bridge_reply,
            "n_emissions": len(emissions),
            "first_emission": emissions[0]["text"] if emissions else None,
            "last_emission": emissions[-1]["text"] if emissions else None,
            "last_has_tool": emissions[-1]["has_tool"] if emissions else None,
            "session_file": str(sess_file.name),
            "emissions": emissions,
        })

    print(f"[load] {len(per_qa)} QAs, {no_session} without matching session file", file=sys.stderr)

    # Apply strategies
    strategy_results: Dict[str, dict] = {}
    for name, strat in STRATEGIES.items():
        all_scores: List[Tuple[str, int]] = []  # (cat, score)
        cat_correct = defaultdict(int)
        cat_total = defaultdict(int)
        diff_from_captured = 0
        examples_diff = []
        for r in per_qa:
            if r.get("no_session"):
                # Can't simulate; fall back to captured
                sim = r["captured"]
            else:
                sim = strat(r["bridge_reply"], r["emissions"])
            score = gold_substring_score(sim, r["gold"])
            cat_correct[r["cat"]] += score
            cat_total[r["cat"]] += 1
            all_scores.append((r["cat"], score))
            if not r.get("no_session") and sim != r["captured"]:
                diff_from_captured += 1
                if len(examples_diff) < 10:
                    examples_diff.append({
                        "qid": r["qid"], "cat": r["cat"], "gold": r["gold"],
                        "captured": r["captured"][:200],
                        "simulated": sim[:200],
                    })
        total = sum(cat_total.values())
        correct = sum(cat_correct.values())
        cat_breakdown = {c: {"correct": cat_correct[c], "n": cat_total[c],
                             "acc": cat_correct[c] / cat_total[c] if cat_total[c] else 0}
                         for c in sorted(cat_total)}
        strategy_results[name] = {
            "acc_overall": correct / total if total else 0,
            "correct": correct, "total": total,
            "cat": cat_breakdown,
            "differs_from_captured": diff_from_captured,
            "examples_diff": examples_diff,
        }

    return {"per_qa": per_qa, "strategies": strategy_results, "no_session": no_session}


# ---------- report ----------

def print_report(out: dict):
    sr = out["strategies"]
    names = list(sr.keys())
    cats = sorted({c for r in sr.values() for c in r["cat"]})
    print()
    print(f"{'strategy':<22} {'overall':>9} " + " ".join(f"cat{c:<3}" for c in cats) + f"  {'diff':>5}")
    print("-" * (22 + 10 + len(cats) * 8 + 8))
    for n in names:
        r = sr[n]
        cells = []
        for c in cats:
            cb = r["cat"].get(c)
            cells.append(f"{cb['acc']*100:5.2f}%" if cb else "  -  ")
        print(f"{n:<22} {r['acc_overall']*100:8.2f}% " + " ".join(f"{x:>7}" for x in cells)
              + f"  {r['differs_from_captured']:>5}")
    print()
    print("(scoring: substring(gold, normalized(simulated)) — UNDER-estimates LoCoMo judge acc;")
    print(" relative ranking across strategies is the actionable signal.)")
    print()
    print(f"unmatched-session QAs: {out['no_session']}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("archive_root", type=Path,
                   help="evaluation/archives/<run-name-tstamp>")
    p.add_argument("--out", type=Path, default=None,
                   help="optional full json dump")
    p.add_argument("--show-diffs", action="store_true",
                   help="show example diffs per strategy")
    args = p.parse_args()

    out = replay(args.archive_root.resolve())
    print_report(out)

    if args.show_diffs:
        for name, r in out["strategies"].items():
            if r["differs_from_captured"] == 0:
                continue
            print(f"\n=== {name}: example diffs ===")
            for ex in r["examples_diff"]:
                print(f"  [{ex['qid']} cat{ex['cat']}] gold={ex['gold']!r}")
                print(f"    captured : {ex['captured']!r}")
                print(f"    simulated: {ex['simulated']!r}")

    if args.out:
        # Strip emissions from per_qa to keep dump small
        slim = {
            "strategies": out["strategies"],
            "per_qa": [
                {k: v for k, v in r.items() if k != "emissions"}
                for r in out["per_qa"]
            ],
        }
        json.dump(slim, open(args.out, "w"), indent=2, default=str)
        print(f"\nfull dump: {args.out}")


if __name__ == "__main__":
    main()
