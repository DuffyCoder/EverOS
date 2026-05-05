"""R-S3-4 — aggregate `forced_terminate` ratio from openclaw adapter events.

The bridge SIGKILLs an agent subprocess when its child group (hypermem
indexer / context-engine background timer) keeps stderr open past the
agent's natural stop. The reply is already parsed from stderr at that
point, so the eval treats the call as ok=true with `forced_terminate=true`
in the response. Latency for that call includes the bridge's 2s SIGTERM
grace + 1s SIGKILL fallback baked in.

This metric helps interpret latency stats: a run with high forced_terminate
ratio has its tail latencies inflated by the kill-timer overhead.

Usage:
    metrics = build_forced_terminate_metrics(events_iter)
    # {
    #     "agent_run_count": 50,
    #     "forced_terminate_count": 47,
    #     "forced_terminate_rate": 0.94,
    #     "kill_overhead_ms_estimate": 3000.0,  # 47 calls × 3s grace
    # }
"""
from __future__ import annotations

from typing import Iterable, Mapping


_KILL_GRACE_MS = 3000.0  # 2s SIGTERM + 1s SIGKILL fallback per bridge.runLauncher


def build_forced_terminate_metrics(events: Iterable[Mapping]) -> dict:
    """Aggregate forced_terminate rate across agent_run_complete events.

    Args:
        events: iterable of event dicts (e.g. parsed JSONL lines from
            sandbox events.jsonl).

    Returns:
        Dict with agent_run_count, forced_terminate_count,
        forced_terminate_rate, and kill_overhead_ms_estimate.
        Empty result when no agent_run_complete events present.
    """
    total = 0
    forced = 0
    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        if ev.get("event") != "agent_run_complete":
            continue
        total += 1
        if ev.get("forced_terminate") is True:
            forced += 1

    if total == 0:
        return {
            "agent_run_count": 0,
            "forced_terminate_count": 0,
            "forced_terminate_rate": 0.0,
            "kill_overhead_ms_estimate": 0.0,
        }

    return {
        "agent_run_count": total,
        "forced_terminate_count": forced,
        "forced_terminate_rate": forced / total,
        "kill_overhead_ms_estimate": forced * _KILL_GRACE_MS,
    }
