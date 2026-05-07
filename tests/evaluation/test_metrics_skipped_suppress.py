"""
v0.7 D3 unit tests for retrieval_metrics + content_overlap suppress
of ``retrieval_metadata.skipped == True`` samples.

Path B (answer_mode=agent_local) returns SearchResult(skipped=True),
metrics must NOT score these as 0 — that would falsely indicate
retrieval failure. Instead aggregate only over scored samples and
report n_skipped separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from evaluation.src.core.data_models import SearchResult
from evaluation.src.metrics.retrieval_metrics import evaluate_retrieval_metrics
from evaluation.src.metrics.content_overlap import evaluate_content_overlap


@dataclass
class FakeQA:
    question_id: str
    question: str
    answer: str
    evidence: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _skipped_sr(qid: str, conv: str = "c1") -> SearchResult:
    return SearchResult(
        query="q",
        conversation_id=conv,
        results=[],
        retrieval_metadata={
            "system": "openclaw",
            "skipped": True,
            "reason": "agent_local_owns_retrieval",
            "question_id": qid,
        },
    )


def _real_sr(qid: str, content: str, conv: str = "c1") -> SearchResult:
    return SearchResult(
        query="q",
        conversation_id=conv,
        results=[{"content": content, "score": 0.9, "metadata": {
            "source_sessions": ["S0"],
        }}],
        retrieval_metadata={"question_id": qid},
    )


# --- retrieval_metrics ----------------------------------------------

def test_retrieval_metrics_all_skipped_returns_not_applicable():
    qa_pairs = [FakeQA(question_id=f"q{i}", question="?", answer="x",
                       evidence=["S0"]) for i in range(3)]
    search_results = [_skipped_sr(qa.question_id) for qa in qa_pairs]

    result = evaluate_retrieval_metrics(qa_pairs, search_results, k=5)

    assert result["status"] == "not_applicable"
    assert result["reason"] == "all_queries_skipped"
    assert result["n_scored"] == 0
    assert result["n_skipped"] == 3
    # Critical: no zero-mean misleading aggregates
    assert "evidence_recall_at_k_mean" not in result
    assert "mrr_mean" not in result


def test_retrieval_metrics_mixed_skipped_and_scored():
    """Some samples real, some skipped: aggregate only over real ones."""
    qa_pairs = [
        FakeQA(question_id="q0", question="?", answer="alice",
               evidence=["S0"]),
        FakeQA(question_id="q1", question="?", answer="bob",
               evidence=["S0"]),
        FakeQA(question_id="q2", question="?", answer="carol",
               evidence=["S0"]),
    ]
    search_results = [
        _real_sr("q0", "alice was here", conv="c1"),
        _skipped_sr("q1"),  # skipped
        _real_sr("q2", "carol attended", conv="c1"),
    ]

    result = evaluate_retrieval_metrics(qa_pairs, search_results, k=5)

    # Only 2 scored, 1 skipped
    assert result["n_scored"] == 2
    assert result["n_skipped"] == 1
    assert "q1" in result["skipped_question_ids"]
    # Aggregates exist (not N/A) since we have scored samples
    assert "evidence_recall_at_k_mean" in result
    # Should not have included q1 in per_question
    qids = {p["question_id"] for p in result["per_question"]}
    assert qids == {"q0", "q2"}


def test_retrieval_metrics_no_skipped_unchanged():
    """No skipped samples: behaves exactly as v0.6 (no regression)."""
    qa_pairs = [
        FakeQA(question_id="q0", question="?", answer="alice",
               evidence=["S0"]),
        FakeQA(question_id="q1", question="?", answer="bob",
               evidence=["S0"]),
    ]
    search_results = [
        _real_sr("q0", "alice", conv="c1"),
        _real_sr("q1", "bob", conv="c1"),
    ]

    result = evaluate_retrieval_metrics(qa_pairs, search_results, k=5)

    assert "evidence_recall_at_k_mean" in result
    assert result["n_scored"] == 2
    assert result["n_skipped"] == 0


# --- content_overlap ------------------------------------------------

def test_content_overlap_all_skipped_returns_not_applicable():
    qa_pairs = [FakeQA(question_id=f"q{i}", question="?", answer="x")
                for i in range(3)]
    search_results = [_skipped_sr(qa.question_id) for qa in qa_pairs]

    result = evaluate_content_overlap(qa_pairs, search_results, k=5)

    assert result["status"] == "not_applicable"
    assert result["reason"] == "all_queries_skipped"
    assert result["n_scored"] == 0
    assert result["n_skipped"] == 3
    assert "content_overlap_at_k_mean" not in result


def test_content_overlap_mixed_skipped_and_scored():
    qa_pairs = [
        FakeQA(question_id="q0", question="?", answer="alice was here"),
        FakeQA(question_id="q1", question="?", answer="bob attended"),
    ]
    search_results = [
        _real_sr("q0", "alice was indeed here yesterday"),
        _skipped_sr("q1"),
    ]

    result = evaluate_content_overlap(qa_pairs, search_results, k=5)

    assert result["n_scored"] == 1
    assert result["n_skipped"] == 1
    assert "content_overlap_at_k_mean" in result  # has real samples
    assert len(result["per_question"]) == 1


def test_content_overlap_no_skipped_unchanged():
    qa_pairs = [FakeQA(question_id="q0", question="?", answer="alice was here")]
    search_results = [_real_sr("q0", "alice")]

    result = evaluate_content_overlap(qa_pairs, search_results, k=5)

    assert result["n_scored"] == 1
    assert result["n_skipped"] == 0
    assert "content_overlap_at_k_mean" in result
