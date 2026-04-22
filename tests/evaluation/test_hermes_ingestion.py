"""Tests for evaluation.src.adapters.hermes.ingestion turn-pair iterator."""
from __future__ import annotations

from evaluation.src.core.data_models import Conversation, Message


def _msg(speaker_id: str, content: str) -> Message:
    return Message(speaker_id=speaker_id, speaker_name=speaker_id, content=content)


def test_two_speaker_even_pairs_in_order():
    from evaluation.src.adapters.hermes.ingestion import iter_turn_pairs

    conv = Conversation(
        conversation_id="c1",
        messages=[
            _msg("a", "hi"),
            _msg("b", "hey"),
            _msg("a", "how are you?"),
            _msg("b", "good"),
        ],
    )

    pairs = list(iter_turn_pairs(conv))
    assert pairs == [("hi", "hey"), ("how are you?", "good")]


def test_odd_trailing_turn_is_paired_with_empty_string():
    from evaluation.src.adapters.hermes.ingestion import iter_turn_pairs

    conv = Conversation(
        conversation_id="c1",
        messages=[
            _msg("a", "hi"),
            _msg("b", "hey"),
            _msg("a", "dangling"),
        ],
    )

    pairs = list(iter_turn_pairs(conv))
    assert pairs == [("hi", "hey"), ("dangling", "")]


def test_three_speaker_round_robin_warns(caplog):
    from evaluation.src.adapters.hermes.ingestion import iter_turn_pairs

    conv = Conversation(
        conversation_id="c1",
        messages=[
            _msg("a", "1"),
            _msg("b", "2"),
            _msg("c", "3"),
            _msg("a", "4"),
        ],
    )

    with caplog.at_level("WARNING"):
        pairs = list(iter_turn_pairs(conv))
    assert pairs == [("1", "2"), ("3", "4")]
    assert any("3 speakers" in r.message or "multi-speaker" in r.message.lower()
               for r in caplog.records)


def test_empty_conversation_yields_no_pairs():
    from evaluation.src.adapters.hermes.ingestion import iter_turn_pairs

    conv = Conversation(conversation_id="c1", messages=[])
    assert list(iter_turn_pairs(conv)) == []
