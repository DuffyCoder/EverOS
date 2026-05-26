"""
Core data models.

Define standard data formats for the evaluation framework to ensure interoperability
between different systems and datasets.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime


# Canonical Stage-3 answer sentinels. The answer stage writes one of these
# literal strings into ``AnswerResult.answer`` when answer generation fails
# (timeout / retries exhausted), and the LLM judge treats an answer that is
# EXACTLY one of these as judge-unavailable (excluded from the accuracy
# denominator) rather than judging it. Matching the exact set — not a broad
# ``startswith("Error:")`` — avoids dropping a legitimate model answer that
# merely happens to begin with "Error:". Defined here (a leaf module) so both
# the answer stage and the evaluator share one source of truth and can't drift.
ANSWER_SENTINEL_FAILED = "Error: Failed to generate answer"
ANSWER_SENTINEL_DEADLINE = "Error: deadline exceeded before retry"
ANSWER_SENTINEL_TIMEOUT = "Error: Answer generation timeout after retries"
ANSWER_ERROR_SENTINELS = frozenset({
    ANSWER_SENTINEL_FAILED,
    ANSWER_SENTINEL_DEADLINE,
    ANSWER_SENTINEL_TIMEOUT,
})


@dataclass
class Message:
    """Standard message format."""
    speaker_id: str
    speaker_name: str
    content: str
    timestamp: Optional[datetime] = None  # Optional, some datasets (e.g., PersonaMem) lack timestamps
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    """Standard conversation format."""
    conversation_id: str
    messages: List[Message]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QAPair:
    """
    Standard QA pair format.
    
    Note: category field is unified as string type to be compatible with different datasets.
    """
    question_id: str
    question: str
    answer: str
    category: Optional[str] = None  # Unified as string type
    evidence: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Dataset:
    """Standard dataset format."""
    dataset_name: str
    conversations: List[Conversation]
    qa_pairs: List[QAPair]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    """Standard search result format."""
    query: str
    conversation_id: str
    results: List[Dict[str, Any]]  # [{"content": str, "score": float, "metadata": dict}]
    retrieval_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnswerResult:
    """Standard answer result format."""
    question_id: str
    question: str
    answer: str
    golden_answer: str
    category: Optional[int] = None
    conversation_id: str = ""
    formatted_context: str = ""  # Actual context used
    search_results: List[Dict[str, Any]] = field(default_factory=list) 
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    """Standard evaluation result format."""
    total_questions: int
    correct: int
    accuracy: float
    detailed_results: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

