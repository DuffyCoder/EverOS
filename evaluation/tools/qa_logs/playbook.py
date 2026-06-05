"""OTLP trace dump → readable playbook (jsonl + pretty json).

Reads a JSONL file produced by an OTLP gRPC receiver (one
ExportTraceServiceRequest per line, with ``resource_spans`` shape) and emits
one ``record per trace`` as both JSONL (stream-friendly, one trace per line)
and pretty JSON (single file, browser/jless-friendly).

Each output trace is a chronologically-ordered nested tree:

  {
    trace_id: "0e64a5d6…",
    n_spans: 6,
    duration_ms: 93053.989,
    start_iso: "2026-06-04T20:29:52.249",
    root: {
      type: "span", name, kind, dur_ms, attrs?,
      timeline: [
        {type: "event", t_ms: ..., key?: ..., body: ...},
        {type: "span", name, kind, dur_ms, timeline: [...]},
        ...
      ]
    }
  }

Event payloads that parse as JSON are nested as real objects (so jq/jless can
walk into LLM prompts as structured data); non-JSON bodies remain strings.

Stand-alone CLI; NOT invoked by raw_dump or annotate.

    python -m evaluation.tools.qa_logs.playbook \\
        --trace-dump /Data/shutong.shan/.claude/jobs/.../trace_dump.jsonl \\
        --out-dir reports/qa_logs/<run-name>/playbook

Defaults:
* ``--out-dir`` defaults to ``<trace-dump-parent>/playbook/``
* Writes both ``playbook.jsonl`` and ``playbook.json``

The trace_dump.jsonl is produced by the OTLP receiver script in
``/Data/shutong.shan/.claude/jobs/<id>/tmp/otlp_receiver.py`` (out of tree —
host OV server's OTel exporter writes to ``localhost:4317`` where the
receiver listens). To produce one, configure OV's ``ov.local.conf`` with
``server.observability.traces.enabled = true, endpoint = localhost:4317``
and have the receiver running.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCAL_OFFSET = datetime.now(timezone.utc).astimezone().utcoffset()


def _b64hex(s: str) -> str:
    return base64.b64decode(s).hex() if s else ""


def _attr_val(v: dict):
    if not v:
        return None
    for k, val in v.items():
        if k.endswith("_value"):
            return val
    return None


def _flat_attrs(attrs):
    return {kv["key"]: _attr_val(kv.get("value", {})) for kv in attrs or []}


def _load_spans(dump_path: Path) -> list[dict]:
    out = []
    with dump_path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ss in rec["resource_spans"].get("scope_spans", []):
                for sp in ss.get("spans", []):
                    s = {
                        "name": sp["name"],
                        "kind": sp.get("kind", "").replace("SPAN_KIND_", "").lower(),
                        "trace_id": _b64hex(sp.get("trace_id", "")),
                        "span_id": _b64hex(sp.get("span_id", "")),
                        "parent_id": _b64hex(sp.get("parent_span_id", "")),
                        "start_ns": int(sp.get("start_time_unix_nano", 0)),
                        "end_ns": int(sp.get("end_time_unix_nano", 0)),
                        "attrs": _flat_attrs(sp.get("attributes", [])),
                        "events": [
                            {
                                "name": ev.get("name", ""),
                                "time_ns": int(ev.get("time_unix_nano", 0)),
                            }
                            for ev in sp.get("events", [])
                        ],
                    }
                    s["dur_ms"] = (s["end_ns"] - s["start_ns"]) / 1e6
                    out.append(s)
    return out


def _parse_event(ev: dict) -> dict:
    """Convert one event into a timeline item.

    Tries to extract structure: ``key=...`` → ``{key, body}`` where ``body``
    is the parsed JSON object if applicable, else raw string. Free-form
    ``Starting v2 memory extraction…`` events become ``{body: <full text>}``.
    """
    name = ev["name"]
    head = name[:80]
    if "=" not in head:
        return {"type": "event", "body": name}

    key, body = name.split("=", 1)
    if " " in key or len(key) > 50:
        return {"type": "event", "body": name}

    body_s = body.strip()

    if body_s and body_s[0] in "{[":
        try:
            return {"type": "event", "key": key, "body": json.loads(body_s)}
        except json.JSONDecodeError:
            pass

    if body_s.startswith("```json"):
        inner = body_s[len("```json"):].lstrip("\n").rstrip()
        if inner.endswith("```"):
            inner = inner[:-3].rstrip()
        try:
            return {"type": "event", "key": key, "body": json.loads(inner)}
        except json.JSONDecodeError:
            pass

    return {"type": "event", "key": key, "body": body}


def _build_span_tree(span: dict, by_parent: dict, t0_ns: int) -> dict:
    timeline = []
    for ev in span["events"]:
        item = _parse_event(ev)
        item["t_ms"] = (ev["time_ns"] - t0_ns) / 1e6
        item["_t_ns"] = ev["time_ns"]
        timeline.append(item)
    for child in by_parent.get(span["span_id"], []):
        sub = _build_span_tree(child, by_parent, t0_ns)
        sub["t_ms"] = (child["start_ns"] - t0_ns) / 1e6
        sub["_t_ns"] = child["start_ns"]
        timeline.append(sub)

    timeline.sort(key=lambda x: x.pop("_t_ns"))

    out = {
        "type": "span",
        "name": span["name"],
        "kind": span["kind"],
        "dur_ms": round(span["dur_ms"], 3),
    }
    if span["attrs"]:
        out["attrs"] = span["attrs"]
    out["timeline"] = timeline
    return out


def _fmt_iso(ns: int) -> str:
    sec = ns // 1_000_000_000
    rem_ns = ns % 1_000_000_000
    dt = datetime.fromtimestamp(sec, tz=timezone.utc) + (LOCAL_OFFSET or datetime.now(timezone.utc).utcoffset())
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{rem_ns // 1_000_000:03d}"


def build_playbook(spans: list[dict]) -> list[dict]:
    """Group spans by trace_id, return chronologically-sorted trace records."""
    by_trace: dict[str, list[dict]] = {}
    by_parent: dict[str, list[dict]] = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)
        if s["parent_id"]:
            by_parent.setdefault(s["parent_id"], []).append(s)
    for k in by_parent:
        by_parent[k].sort(key=lambda x: x["start_ns"])

    records = []
    for tid, sps in by_trace.items():
        roots = [s for s in sps if not s["parent_id"]]
        if not roots:
            continue
        root = roots[0]
        t0 = min(s["start_ns"] for s in sps)
        t1 = max(s["end_ns"] for s in sps)
        rec = {
            "trace_id": tid,
            "n_spans": len(sps),
            "duration_ms": round((t1 - t0) / 1e6, 3),
            "start_iso": _fmt_iso(t0),
            "root": _build_span_tree(root, by_parent, t0),
        }
        records.append(rec)

    records.sort(key=lambda r: -r["duration_ms"])
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert OTLP trace_dump.jsonl into a readable playbook.",
    )
    parser.add_argument(
        "--trace-dump",
        type=Path,
        required=True,
        help="Path to OTLP receiver's JSONL output (one export request per line).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help=(
            "Output directory. Defaults to <trace-dump-parent>/playbook/. "
            "Writes 'playbook.jsonl' and 'playbook.json' inside."
        ),
    )
    args = parser.parse_args()

    if not args.trace_dump.exists():
        print(f"error: trace dump not found: {args.trace_dump}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or (args.trace_dump.parent / "playbook")
    out_dir.mkdir(parents=True, exist_ok=True)

    out_jsonl = out_dir / "playbook.jsonl"
    out_json = out_dir / "playbook.json"

    spans = _load_spans(args.trace_dump)
    records = build_playbook(spans)

    with out_jsonl.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with out_json.open("w") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    n_finds = sum(1 for r in records if r["root"]["name"] == "POST /api/v1/search/find")
    print(
        f"[playbook] {len(records)} traces from {args.trace_dump.name} "
        f"({len(spans)} spans, {n_finds} POST /search/find)"
    )
    print(f"[playbook] wrote {out_jsonl} ({out_jsonl.stat().st_size} bytes)")
    print(f"[playbook] wrote {out_json}  ({out_json.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
