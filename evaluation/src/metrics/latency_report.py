"""
Latency report semantics and human-readable formatting.

Layer-1 ``latency_views`` stays harness-measured at adapter boundaries.
This module annotates those numbers so reports do not mis-read them,
especially for openclaw-docker ``agent_local`` (search skipped,
retrieval inside answer) and docker add (container boot inside batch add).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def resolve_latency_semantics(adapter: Any) -> dict:
    """Derive interpretation hints from adapter config (no timing changes)."""
    cfg: dict = getattr(adapter, "config", None) or {}
    openclaw: dict = cfg.get("openclaw") or {}
    answer_mode = str(openclaw.get("answer_mode") or "shared_llm")

    adapter_kind = str(cfg.get("adapter") or "").strip()
    if not adapter_kind:
        cls = type(adapter).__name__.lower()
        if "dockerizedopenclaw" in cls or "openclaw_docker" in cls:
            adapter_kind = "openclaw-docker"
        elif "openclaw" in cls:
            adapter_kind = "openclaw"
        elif "evermemos" in cls:
            adapter_kind = "evermemos"

    docker = adapter_kind == "openclaw-docker" or hasattr(
        adapter, "_docker_handles"
    )
    search_integrated = answer_mode == "agent_local"

    if docker:
        add_scope = "stage1_batch_incl_docker_prepare"
        add_note = (
            "Single batched Stage-1 wall clock: orphan cleanup, per-conversation "
            "docker run, startup verify (~2.5s each), in-container config patch, "
            "then parallel ingest (bounded by openclaw.add_max_concurrent_convs). "
            "NOT per-conversation ingest and NOT amortized per QA."
        )
        ingest_note = (
            "Per-conversation ingest only (add_summary.add_latency_ms): "
            "session_bundle / OV SDK path inside the container, excludes "
            "container cold start and docker exec patches."
        )
    else:
        add_scope = "stage1_batch"
        add_note = (
            "Single batched Stage-1 wall clock for adapter.add() over all "
            "conversations (parallelism inside the adapter). NOT per-conversation "
            "ingest unless add_latency_ms_stats is present below."
        )
        ingest_note = (
            "Per-conversation ingest (add_summary.add_latency_ms) when the "
            "adapter writes add_summary.json under metrics_dir."
        )

    if search_integrated:
        search_note = (
            "N/A for memory recall — answer_mode=agent_local; pipeline search() "
            "returns skipped and recall runs inside agent_run / context-engine "
            "assemble. Harness search wall_ms is ~0 and must not be compared "
            "to separated search stages (e.g. EverMemOS)."
        )
        e2e_note = (
            "search_wall_ms + answer_wall_ms; with integrated search this equals "
            "answer within rounding. Use answer (or this e2e) as the per-QA "
            "end-to-end metric."
        )
        primary = "answer"
    else:
        search_note = (
            "Pipeline search() boundary; comparable across adapters that "
            "populate retrieval in search stage."
        )
        e2e_note = "search_wall_ms + answer_wall_ms per question_id."
        primary = "e2e_query_ms"

    return {
        "answer_mode": answer_mode,
        "adapter_kind": adapter_kind or None,
        "docker_stage1": bool(docker),
        "search_integrated_in_answer": search_integrated,
        "add_canonical_scope": add_scope,
        "primary_query_metric": primary,
        "notes": {
            "add_canonical": add_note,
            "ingest_per_conv": ingest_note,
            "search": search_note,
            "e2e_query_ms": e2e_note,
        },
    }


def enrich_latency_views(
    latency_views: dict,
    diagnostics: Optional[dict],
    semantics: dict,
) -> dict:
    """Attach semantics + per-conv ingest stats to latency_views.json."""
    out = dict(latency_views)
    ingest_stats = None
    if diagnostics:
        ingest_stats = diagnostics.get("add_latency_ms_stats")
    out["_semantics"] = {
        **semantics,
        "ingest_per_conv_ms": ingest_stats,
    }
    return out


def _fmt_stats(v: Optional[dict]) -> str:
    if not v:
        return "n/a"
    parts: List[str] = []
    for key in ("n", "mean", "p50", "p95", "max"):
        if key not in v or v[key] is None:
            continue
        if key == "n":
            parts.append(f"{key}={v[key]}")
        else:
            parts.append(f"{key}={float(v[key]):.2f}")
    return "{" + ", ".join(parts) + "}" if parts else "n/a"


def _fmt_wall_views(wall_views: dict) -> List[str]:
    lines: List[str] = []
    for view_name, v in wall_views.items():
        if not v:
            lines.append(f"    {view_name}: n/a")
            continue
        parts = [
            f"n={v['n']}",
            f"mean={v['mean']:.2f}",
            f"p50={v['p50']:.2f}",
            f"p95={v['p95']:.2f}",
            f"max={v['max']:.2f}",
        ]
        lines.append(f"    {view_name}: {{{', '.join(parts)}}}")
    return lines


def format_latency_report_lines(
    latency: dict,
    diagnostics: Optional[dict],
    semantics: dict,
    retry_policy: str,
) -> List[str]:
    """Build report.txt latency section lines."""
    lines: List[str] = []
    notes = semantics.get("notes") or {}
    diag = diagnostics or {}

    lines.append(f"Latency (canonical, retry_policy={retry_policy}):")
    lines.append(f"  Semantics: answer_mode={semantics.get('answer_mode')}"
                 f", primary_query_metric={semantics.get('primary_query_metric')}")
    if semantics.get("search_integrated_in_answer"):
        lines.append(
            "  Note: search stage is integrated in answer (agent_local); "
            "see search row below."
        )
    lines.append("")

    # --- add ---
    add_stage = latency.get("add")
    if add_stage:
        n_calls = add_stage.get("n_calls")
        lines.append(
            f"  add / stage1 batch (n={n_calls}) — {semantics.get('add_canonical_scope')}:"
        )
        lines.append(f"    scope: {notes.get('add_canonical', '')}")
        lines.extend(_fmt_wall_views(add_stage.get("wall_ms") or {}))
        rel = add_stage.get("reliability")
        if rel:
            lines.append(
                f"    reliability: {{retry={rel['retry_rate']:.3f}, "
                f"fallback={rel['fallback_rate']:.3f}, "
                f"failed={rel['failed_rate']:.3f}}}"
            )
        ingest_stats = diag.get("add_latency_ms_stats")
        if ingest_stats:
            lines.append(
                f"  ingest per conversation (n={ingest_stats.get('n', '?')}) "
                f"[excludes container boot on docker]:"
            )
            lines.append(f"    scope: {notes.get('ingest_per_conv', '')}")
            lines.append(f"    realistic: {_fmt_stats(ingest_stats)}")
        lines.append("")

    # --- search ---
    search_stage = latency.get("search")
    if search_stage:
        n_calls = search_stage.get("n_calls")
        if semantics.get("search_integrated_in_answer"):
            lines.append(
                f"  search (n={n_calls}): N/A — integrated in answer (agent_local)"
            )
            lines.append(f"    {notes.get('search', '')}")
            wall = search_stage.get("wall_ms") or {}
            realistic = wall.get("realistic")
            if realistic:
                lines.append(
                    f"    harness placeholder (do not use): mean={realistic.get('mean', 0):.2f} ms"
                )
        else:
            lines.append(f"  search (n={n_calls}):")
            lines.extend(_fmt_wall_views(search_stage.get("wall_ms") or {}))
            rel = search_stage.get("reliability")
            if rel:
                lines.append(
                    f"    reliability: {{retry={rel['retry_rate']:.3f}, "
                    f"fallback={rel['fallback_rate']:.3f}, "
                    f"failed={rel['failed_rate']:.3f}}}"
                )
        lines.append("")

    # --- answer ---
    answer_stage = latency.get("answer")
    if answer_stage:
        n_calls = answer_stage.get("n_calls")
        primary = semantics.get("primary_query_metric") == "answer"
        label = "answer"
        if primary:
            label += " [primary per-QA metric when search integrated]"
        lines.append(f"  {label} (n={n_calls}):")
        lines.extend(_fmt_wall_views(answer_stage.get("wall_ms") or {}))
        rel = answer_stage.get("reliability")
        if rel:
            lines.append(
                f"    reliability: {{retry={rel['retry_rate']:.3f}, "
                f"fallback={rel['fallback_rate']:.3f}, "
                f"failed={rel['failed_rate']:.3f}}}"
            )
        lines.append("")

    # --- e2e ---
    e2e_stage = latency.get("e2e_query_ms")
    if e2e_stage:
        n_calls = e2e_stage.get("n_calls")
        if semantics.get("search_integrated_in_answer"):
            lines.append(
                f"  e2e_query_ms (n={n_calls}) [≈ answer when search integrated]:"
            )
        else:
            lines.append(f"  e2e_query_ms (n={n_calls}) [primary per-QA metric]:")
        lines.append(f"    scope: {notes.get('e2e_query_ms', '')}")
        lines.extend(_fmt_wall_views(e2e_stage.get("wall_ms") or {}))
        lines.append("")

    return lines
