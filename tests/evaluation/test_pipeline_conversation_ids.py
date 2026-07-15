from __future__ import annotations

import logging

from evaluation.src.core.data_models import Conversation, Dataset, QAPair
from evaluation.src.core.pipeline import Pipeline


def _pipeline() -> Pipeline:
    pipe = Pipeline.__new__(Pipeline)
    pipe.logger = logging.getLogger(__name__)
    return pipe


def _dataset() -> Dataset:
    conversations = [
        Conversation(conversation_id=f"locomo_{idx}", messages=[])
        for idx in range(10)
    ]
    qa_pairs = [
        QAPair(
            question_id=f"locomo_{idx}_qa{qa_idx}",
            question="?",
            answer="x",
            metadata={"conversation_id": f"locomo_{idx}"},
        )
        for idx in range(10)
        for qa_idx in range(2)
    ]
    return Dataset(
        dataset_name="locomo",
        conversations=conversations,
        qa_pairs=qa_pairs,
        metadata={"source": "unit"},
    )


def test_apply_conversation_ids_selects_non_contiguous_convs_in_requested_order():
    selected = _pipeline()._apply_conversation_ids(
        _dataset(),
        ["locomo_4", "locomo_3", "locomo_8"],
    )

    assert [conv.conversation_id for conv in selected.conversations] == [
        "locomo_4",
        "locomo_3",
        "locomo_8",
    ]
    assert [qa.question_id for qa in selected.qa_pairs] == [
        "locomo_4_qa0",
        "locomo_4_qa1",
        "locomo_3_qa0",
        "locomo_3_qa1",
        "locomo_8_qa0",
        "locomo_8_qa1",
    ]
    assert selected.metadata["conversation_ids"] == [
        "locomo_4",
        "locomo_3",
        "locomo_8",
    ]
    assert selected.metadata["missing_conversation_ids"] == []


def test_apply_conversation_ids_accepts_numeric_suffixes_and_records_missing():
    selected = _pipeline()._apply_conversation_ids(_dataset(), ["8", "11"])

    assert [conv.conversation_id for conv in selected.conversations] == ["locomo_8"]
    assert [qa.question_id for qa in selected.qa_pairs] == [
        "locomo_8_qa0",
        "locomo_8_qa1",
    ]
    assert selected.metadata["conversation_ids"] == ["locomo_8", "locomo_11"]
    assert selected.metadata["missing_conversation_ids"] == ["locomo_11"]
