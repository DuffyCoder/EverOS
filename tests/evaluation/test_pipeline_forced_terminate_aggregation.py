"""Regression test for Pipeline._aggregate_forced_terminate_metrics.

The aggregator walks `events.jsonl` files in the run output directory
and parses each line via `json.loads`. A previous version used
`json.loads(...)` without importing the `json` module at the top of
pipeline.py, so any non-empty events file would crash with NameError
during diagnostics emit. This test exercises the file-based path so
the missing import is caught before reaching production runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.src.core.pipeline import Pipeline


def _make_pipeline(output_dir: Path) -> Pipeline:
    """Build a Pipeline with only output_dir set — bypasses the heavy
    constructor (which wants adapter/evaluator/llm_provider) and lets
    us drive _aggregate_forced_terminate_metrics directly."""
    pipe = Pipeline.__new__(Pipeline)
    pipe.output_dir = output_dir
    return pipe


def test_aggregator_parses_events_jsonl_without_crash(tmp_path: Path) -> None:
    """End-to-end: events.jsonl on disk → metrics dict with the right counts.

    Before the import fix, json.loads at line 808 raised NameError on the
    very first event line, so this whole code path crashed.
    """
    events_dir = tmp_path / "artifacts" / "openclaw" / "run-1" / "conv0"
    events_dir.mkdir(parents=True)
    events_file = events_dir / "events.jsonl"
    lines = [
        {"event": "agent_run_complete", "forced_terminate": False},
        {"event": "agent_run_complete", "forced_terminate": True},
        {"event": "agent_run_complete", "forced_terminate": False},
        {"event": "ingest_started"},  # noise
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in lines) + "\n")

    pipe = _make_pipeline(tmp_path)
    metrics = pipe._aggregate_forced_terminate_metrics()

    assert metrics["agent_run_count"] == 3
    assert metrics["forced_terminate_count"] == 1


def test_aggregator_returns_zero_when_no_events_file(tmp_path: Path) -> None:
    """Non-openclaw runs (in-memory adapters) have no events files. Must
    return the empty shape, not crash."""
    pipe = _make_pipeline(tmp_path)
    metrics = pipe._aggregate_forced_terminate_metrics()
    assert metrics["agent_run_count"] == 0
    assert metrics["forced_terminate_count"] == 0


def test_aggregator_skips_corrupt_jsonl_lines(tmp_path: Path) -> None:
    """Best-effort parse: corrupt JSONL lines are silently skipped so a
    truncated events file (interrupted run) doesn't kill diagnostics."""
    events_dir = tmp_path / "artifacts" / "openclaw" / "run-1" / "conv0"
    events_dir.mkdir(parents=True)
    events_file = events_dir / "events.jsonl"
    events_file.write_text(
        "\n".join([
            json.dumps({"event": "agent_run_complete", "forced_terminate": True}),
            "{not valid json",   # corrupt mid-line
            json.dumps({"event": "agent_run_complete", "forced_terminate": False}),
        ]) + "\n"
    )

    pipe = _make_pipeline(tmp_path)
    metrics = pipe._aggregate_forced_terminate_metrics()
    # Only the two well-formed events count.
    assert metrics["agent_run_count"] == 2
    assert metrics["forced_terminate_count"] == 1


def test_aggregator_walks_multiple_conv_dirs(tmp_path: Path) -> None:
    """Multiple per-conv events.jsonl files must all be aggregated."""
    base = tmp_path / "artifacts" / "openclaw" / "run-1"
    for i in range(3):
        d = base / f"conv{i}"
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text(
            json.dumps({"event": "agent_run_complete", "forced_terminate": i == 0})
            + "\n"
        )
    pipe = _make_pipeline(tmp_path)
    metrics = pipe._aggregate_forced_terminate_metrics()
    assert metrics["agent_run_count"] == 3
    assert metrics["forced_terminate_count"] == 1
