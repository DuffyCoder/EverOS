import pytest
from pathlib import Path
from evaluation.tools.qa_logs.session_jsonl import find_user_message

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_session_jsonl():
    return FIXTURES / "sample_session.jsonl"


# ── Core assertions from the plan (lines 1333-1338) ─────────────────────────

def test_find_user_message_for_qid_returns_text_and_timestamp(fixture_session_jsonl):
    msg = find_user_message(fixture_session_jsonl, qid_idx=9)
    assert "When did Deborah's father" in msg.text
    assert msg.unix_ts_ms == 1779869911196
    assert msg.injected_bullets and len(msg.injected_bullets) == 5


# ── Additional coverage ──────────────────────────────────────────────────────

def test_no_relevant_memories_block_gives_empty_bullets(fixture_session_jsonl):
    """qid_idx=0 has no <relevant-memories> block in its user message."""
    msg = find_user_message(fixture_session_jsonl, qid_idx=0)
    # The early QAs have a <relevant-memories> block but with a single bullet.
    # Use qid_idx=0 and check bullets is a list (possibly 1 item).
    # To test the empty case we need a message without the block entirely.
    # qid_idx=0..8 have exactly one bullet (the real block is present but short).
    # Test the edge: no block → [] by calling _parse_bullets directly.
    from evaluation.tools.qa_logs.session_jsonl import _parse_bullets
    assert _parse_bullets("This text has no relevant-memories tag.") == []


def test_qid_idx_out_of_range_raises_index_error(fixture_session_jsonl):
    with pytest.raises(IndexError):
        find_user_message(fixture_session_jsonl, qid_idx=99)


def test_bullets_text_contains_full_body_not_just_abstract_head(fixture_session_jsonl):
    """Each bullet's .text is the full body, not truncated to abstract_head."""
    msg = find_user_message(fixture_session_jsonl, qid_idx=9)
    for bullet in msg.injected_bullets:
        # abstract_head is first 80 chars; text must be >= abstract_head length
        assert len(bullet.text) >= len(bullet.abstract_head)
        assert bullet.text.startswith(bullet.abstract_head)
        assert bullet.chars == len(bullet.text)


def test_assistant_reply_captured_after_user_message(fixture_session_jsonl):
    """Assistant text and thinking are extracted from the reply following qa9."""
    msg = find_user_message(fixture_session_jsonl, qid_idx=9)
    assert msg.assistant_text is not None
    assert "1980" in msg.assistant_text
    assert msg.assistant_thinking is not None
    assert "1980" in msg.assistant_thinking
