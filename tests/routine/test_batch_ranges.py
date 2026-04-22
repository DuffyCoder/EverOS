"""
Regression tests for the auto-bench routine's batch-range math.

The run-bench skill (.claude/skills/run-bench-with-docker-stack/SKILL.md)
splits LoCoMo into batches using:

    for START in $(seq 0 $BATCH_SIZE $((NCONVS - 1))); do
        END=$(( START + BATCH_SIZE ))
        [ $END -gt $NCONVS ] && END=$NCONVS

with `--to-conv $END` where END is EXCLUSIVE (matches evaluation/cli.py:107).

These tests lock in three properties the Codex adversarial review caught
silently broken in the first draft:

1. `--to-conv` is exclusive — ranges built with `END = START + BATCH_SIZE`
   (not `- 1`) give full `{0..NCONVS-1}` coverage.
2. Batch slicing is contiguous — no gap, no overlap.
3. Edge cases (BATCH_SIZE >= NCONVS, BATCH_SIZE not dividing NCONVS)
   still cover every conversation exactly once.

If this file fails, the skill's batch loop was edited in a way that
reintroduces a silent coverage drop.
"""
from __future__ import annotations

import pytest


def simulate_seq(start: int, step: int, stop_inclusive: int) -> list[int]:
    """Reproduce POSIX `seq $start $step $stop_inclusive` output."""
    if step <= 0:
        raise ValueError("step must be positive")
    vals = []
    i = start
    while i <= stop_inclusive:
        vals.append(i)
        i += step
    return vals


def simulate_batch_ranges(nconvs: int, batch_size: int) -> list[tuple[int, int]]:
    """Reproduce the skill's shell loop, returning [(START, END_EXCLUSIVE), ...]."""
    ranges = []
    for start in simulate_seq(0, batch_size, nconvs - 1):
        end = start + batch_size
        if end > nconvs:
            end = nconvs
        ranges.append((start, end))
    return ranges


@pytest.mark.parametrize(
    "nconvs,batch_size",
    [
        (10, 1),
        (10, 2),
        (10, 3),
        (10, 4),
        (10, 5),
        (10, 7),
        (10, 10),
        (10, 11),
        (10, 20),
        (1, 1),
        (1, 5),
    ],
)
def test_batch_ranges_cover_all_conversations_exactly_once(nconvs: int, batch_size: int) -> None:
    ranges = simulate_batch_ranges(nconvs, batch_size)
    covered: list[int] = []
    for lo, hi_ex in ranges:
        covered.extend(range(lo, hi_ex))
    assert sorted(covered) == list(range(nconvs)), (
        f"coverage gap or overlap for nconvs={nconvs} batch_size={batch_size}: "
        f"ranges={ranges} covered={covered}"
    )


@pytest.mark.parametrize(
    "nconvs,batch_size",
    [(10, 2), (10, 3), (10, 5), (10, 7), (10, 10)],
)
def test_batch_ranges_are_contiguous_no_gap(nconvs: int, batch_size: int) -> None:
    ranges = simulate_batch_ranges(nconvs, batch_size)
    assert ranges[0][0] == 0, f"first batch must start at 0, got {ranges[0]}"
    for (lo_a, hi_a), (lo_b, hi_b) in zip(ranges, ranges[1:]):
        assert hi_a == lo_b, f"gap between {(lo_a, hi_a)} and {(lo_b, hi_b)}"
    assert ranges[-1][1] == nconvs, f"last batch must end at {nconvs}, got {ranges[-1]}"


def test_skill_uses_exclusive_to_conv() -> None:
    """
    Guardrail for Codex finding #3 from round 1: passing `--to-conv END_INCLUSIVE`
    with END_INCLUSIVE = START + BATCH_SIZE - 1 silently drops one conversation
    per batch. If the skill is ever edited back to that form, this test breaks.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    skill_path = repo_root / ".claude/skills/run-bench-with-docker-stack/SKILL.md"
    content = skill_path.read_text()

    # The fixed form passes END as exclusive (no `- 1`). The bug form had
    # `END=$(( START + BATCH_SIZE - 1 ))` on its own line.
    assert "END=$(( START + BATCH_SIZE - 1 ))" not in content, (
        "Skill regressed to inclusive --to-conv math. Each batch will drop one "
        "conversation. See tests/routine/test_batch_ranges.py docstring for context."
    )
    # Positive form must still be present so the doc stays operational.
    assert "END=$(( START + BATCH_SIZE ))" in content, (
        "Expected exclusive form `END=$(( START + BATCH_SIZE ))` missing from skill."
    )
