"""Regression test for openclaw_eval_bridge.mjs payload selection.

OpenClaw's ``agent --local --json`` emits ``parsed.payloads`` as an
ordered array of ALL assistant text emissions (preamble → tool → final
answer). Bridge previously selected ``payloads[0]`` which is the
**preamble**, not the final answer — so for any multi-emission session
the eval framework graded the preamble instead of the substantive
reply. Confirmed in the no-override run: 6+ unanimous-WRONG cases would
flip CORRECT once the bridge selects the last text-bearing payload
(qa37/44/68/74/78/81 in locomo_1).

Upstream proof:
- ``/Data3/shutong.shan/openclaw/repo/src/agents/pi-embedded-subscribe.ts:239``
  appends every assistant text in chronological order to
  ``assistantTexts``.
- ``buildEmbeddedRunPayloads`` (pi-embedded-runner/run/payloads.ts:271-302)
  pushes each text into ``replyItems`` preserving order.
- ``normalizeOutboundPayloadsForJson`` (infra/outbound/payloads.ts:124-140)
  is pass-through.

This test is a **source-level regression guard** because node is not
required to be present in the unit test env. It greps both bridge
mjs files (container/ + evaluation/scripts/) for the payload selection
expression and rejects ``[0]``-style indexing on the assistant reply.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_BRIDGES = [
    _ROOT / "openclaw-eval" / "container" / "openclaw_eval_bridge.mjs",
    _ROOT / "evaluation" / "scripts" / "openclaw_eval_bridge.mjs",
]

# Matches the historical buggy expression. Both forms are rejected:
#   parsed.payloads?.[0]?.text
#   parsed.payloads[0].text
_BUGGY = re.compile(
    r"parsed\.payloads\s*\??\s*\.?\[\s*0\s*\]\s*\??\s*\.\s*text"
)


@pytest.mark.parametrize("bridge_path", _BRIDGES, ids=lambda p: p.relative_to(_ROOT).as_posix())
def test_bridge_does_not_select_first_payload_for_reply(bridge_path: Path):
    """``parsed.payloads[0].text`` would return the preamble.

    Use ``parsed.payloads.at(-1)?.text`` (or ``findLast``) so multi-
    emission sessions surface the final answer to the judge.
    """
    src = bridge_path.read_text(encoding="utf-8")
    # Ignore the stub-mode array literal which legitimately has only one
    # element and isn't an indexing read of a model response.
    src_without_stub_block = re.sub(
        r"payloads:\s*\[\{[^}]+\}\]",
        "",
        src,
    )
    assert not _BUGGY.search(src_without_stub_block), (
        f"{bridge_path.relative_to(_ROOT)} still selects payloads[0] for "
        f"reply. payloads is an ordered list of ALL assistant emissions "
        f"(preamble → tool → final answer); index 0 is the preamble. Use "
        f"`parsed.payloads?.at(-1)?.text` or a `findLast` that filters "
        f"non-text/warning entries."
    )


@pytest.mark.parametrize("bridge_path", _BRIDGES, ids=lambda p: p.relative_to(_ROOT).as_posix())
def test_bridge_selects_last_text_payload_for_reply(bridge_path: Path):
    """Positive assertion: the reply assignment uses last-element semantics.

    Accepts ``at(-1)`` / ``findLast`` / ``[payloads.length - 1]`` / etc.
    """
    src = bridge_path.read_text(encoding="utf-8")
    # Find the line that assigns reply. We expect one of these forms.
    patterns = [
        r"\.at\s*\(\s*-\s*1\s*\)",            # parsed.payloads?.at(-1)?.text
        r"\.findLast\s*\(",                    # parsed.payloads.findLast(...)
        r"payloads\.length\s*-\s*1",          # parsed.payloads[payloads.length - 1]
    ]
    assert any(re.search(p, src) for p in patterns), (
        f"{bridge_path.relative_to(_ROOT)} reply selection expression "
        f"does not match any of the accepted last-element patterns: "
        f"{patterns}. Implement the fix at the `const reply = ...` line."
    )
