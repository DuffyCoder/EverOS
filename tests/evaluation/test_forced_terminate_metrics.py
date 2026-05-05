"""R-S3-4 — forced_terminate metric aggregation tests.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_forced_terminate_metrics.py -v
"""
from __future__ import annotations

import pytest

from evaluation.src.metrics.forced_terminate_metrics import (
    build_forced_terminate_metrics,
)


class TestForcedTerminateMetrics:
    def test_empty_events_returns_zeros(self):
        out = build_forced_terminate_metrics([])
        assert out["agent_run_count"] == 0
        assert out["forced_terminate_count"] == 0
        assert out["forced_terminate_rate"] == 0.0
        assert out["kill_overhead_ms_estimate"] == 0.0

    def test_no_agent_run_events_returns_zeros(self):
        events = [
            {"event": "session_ingested"},
            {"event": "prebootstrap_complete"},
            {"event": "index_complete"},
        ]
        out = build_forced_terminate_metrics(events)
        assert out["agent_run_count"] == 0

    def test_all_clean_runs(self):
        events = [
            {"event": "agent_run_complete", "forced_terminate": False},
            {"event": "agent_run_complete", "forced_terminate": False},
            {"event": "agent_run_complete", "forced_terminate": False},
        ]
        out = build_forced_terminate_metrics(events)
        assert out["agent_run_count"] == 3
        assert out["forced_terminate_count"] == 0
        assert out["forced_terminate_rate"] == 0.0
        assert out["kill_overhead_ms_estimate"] == 0.0

    def test_all_forced_terminated(self):
        events = [
            {"event": "agent_run_complete", "forced_terminate": True},
            {"event": "agent_run_complete", "forced_terminate": True},
            {"event": "agent_run_complete", "forced_terminate": True},
            {"event": "agent_run_complete", "forced_terminate": True},
        ]
        out = build_forced_terminate_metrics(events)
        assert out["agent_run_count"] == 4
        assert out["forced_terminate_count"] == 4
        assert out["forced_terminate_rate"] == 1.0
        assert out["kill_overhead_ms_estimate"] == pytest.approx(12000.0)

    def test_mixed(self):
        events = [
            {"event": "agent_run_complete", "forced_terminate": True},
            {"event": "agent_run_complete", "forced_terminate": False},
            {"event": "session_ingested"},
            {"event": "agent_run_complete", "forced_terminate": True},
            {"event": "agent_run_failed", "error": "x"},
            {"event": "agent_run_complete", "forced_terminate": False},
        ]
        out = build_forced_terminate_metrics(events)
        assert out["agent_run_count"] == 4
        assert out["forced_terminate_count"] == 2
        assert out["forced_terminate_rate"] == 0.5
        assert out["kill_overhead_ms_estimate"] == pytest.approx(6000.0)

    def test_missing_forced_terminate_treated_as_false(self):
        """Older events.jsonl from before R-S3-4 don't have the field —
        treat as False so legacy artifacts still aggregate cleanly."""
        events = [
            {"event": "agent_run_complete"},
            {"event": "agent_run_complete", "forced_terminate": True},
        ]
        out = build_forced_terminate_metrics(events)
        assert out["agent_run_count"] == 2
        assert out["forced_terminate_count"] == 1
        assert out["forced_terminate_rate"] == 0.5

    def test_truthy_values_treated_strictly(self):
        """Only `True` counts. Strings, ints, etc. are not forced_terminate."""
        events = [
            {"event": "agent_run_complete", "forced_terminate": "true"},  # str, not bool
            {"event": "agent_run_complete", "forced_terminate": 1},  # int, not bool
            {"event": "agent_run_complete", "forced_terminate": True},
        ]
        out = build_forced_terminate_metrics(events)
        assert out["forced_terminate_count"] == 1

    def test_handles_non_mapping_entries(self):
        events = [None, "skip-this", {"event": "agent_run_complete", "forced_terminate": True}, 42]
        out = build_forced_terminate_metrics(events)
        assert out["agent_run_count"] == 1
        assert out["forced_terminate_count"] == 1
