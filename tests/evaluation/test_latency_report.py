"""Tests for latency report semantics and formatting."""
from __future__ import annotations

from evaluation.src.metrics.latency_report import (
    enrich_latency_views,
    format_latency_report_lines,
    resolve_latency_semantics,
)


class _StubAdapter:
    def __init__(self, config: dict):
        self.config = config


def test_resolve_semantics_agent_local_docker():
    adapter = _StubAdapter({
        "adapter": "openclaw-docker",
        "openclaw": {"answer_mode": "agent_local"},
    })
    sem = resolve_latency_semantics(adapter)
    assert sem["search_integrated_in_answer"] is True
    assert sem["primary_query_metric"] == "answer"
    assert sem["add_canonical_scope"] == "stage1_batch_incl_docker_prepare"
    assert sem["docker_stage1"] is True


def test_resolve_semantics_shared_evermemos():
    adapter = _StubAdapter({
        "adapter": "evermemos",
        "openclaw": {"answer_mode": "shared"},
    })
    sem = resolve_latency_semantics(adapter)
    assert sem["search_integrated_in_answer"] is False
    assert sem["primary_query_metric"] == "e2e_query_ms"


def test_format_search_integrated_shows_na():
    semantics = resolve_latency_semantics(_StubAdapter({
        "adapter": "openclaw-docker",
        "openclaw": {"answer_mode": "agent_local"},
    }))
    latency = {
        "add": {
            "n_calls": 1,
            "wall_ms": {
                "realistic": {"n": 1, "mean": 100.0, "p50": 100.0, "p95": 100.0, "max": 100.0},
            },
        },
        "search": {
            "n_calls": 2,
            "wall_ms": {
                "realistic": {"n": 2, "mean": 0.1, "p50": 0.1, "p95": 0.1, "max": 0.1},
            },
        },
        "answer": {
            "n_calls": 2,
            "wall_ms": {
                "realistic": {"n": 2, "mean": 8000.0, "p50": 8000.0, "p95": 9000.0, "max": 9000.0},
            },
        },
        "e2e_query_ms": {
            "n_calls": 2,
            "wall_ms": {
                "realistic": {"n": 2, "mean": 8000.1, "p50": 8000.1, "p95": 9000.1, "max": 9000.1},
            },
        },
    }
    diagnostics = {
        "add_latency_ms_stats": {
            "n": 2, "mean": 36000.0, "p50": 35000.0, "p95": 40000.0, "max": 40000.0,
        },
    }
    lines = format_latency_report_lines(
        latency, diagnostics, semantics, "realistic",
    )
    text = "\n".join(lines)
    assert "ingest per conversation" in text
    assert "excludes container boot" in text
    assert "N/A — integrated in answer" in text
    assert "primary per-QA metric when search integrated" in text
    assert "stage1_batch_incl_docker_prepare" in text


def test_enrich_latency_views_adds_semantics():
    views = {"answer": {"n_calls": 1}}
    sem = {"answer_mode": "agent_local"}
    diag = {"add_latency_ms_stats": {"n": 1, "mean": 1.0, "p50": 1.0, "p95": 1.0, "max": 1.0}}
    out = enrich_latency_views(views, diag, sem)
    assert out["answer"] == views["answer"]
    assert out["_semantics"]["answer_mode"] == "agent_local"
    assert out["_semantics"]["ingest_per_conv_ms"]["mean"] == 1.0
