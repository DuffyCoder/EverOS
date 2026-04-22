"""Turn-pair iteration for hermes memory provider ingest.

Hermes plugin providers expose ``sync_turn(user_content, assistant_content)``
as the unit of ingest. LoCoMo conversations are lists of messages with a
speaker_id; we convert them into consecutive (user, assistant) string pairs
in the order they appeared, without interpreting speaker semantics
(providers treat both strings opaquely).
"""
from __future__ import annotations

import logging
from typing import Iterator, Tuple

from evaluation.src.core.data_models import Conversation

logger = logging.getLogger(__name__)


def iter_turn_pairs(conversation: Conversation) -> Iterator[Tuple[str, str]]:
    """Yield consecutive ``(user_content, assistant_content)`` pairs.

    Pairing rules:
      - 0 messages → no pairs.
      - Even count → pair (msg_0, msg_1), (msg_2, msg_3), ...
      - Odd count → last pair is ``(msg_last, "")`` so the tail isn't dropped.
      - >=3 distinct speakers → pairs are still emitted in message order but
        a warning is logged; pair semantics are degraded but hermes plugins
        treat the strings opaquely so this is safe.
    """
    messages = conversation.messages
    speakers = {m.speaker_id for m in messages}
    if len(speakers) >= 3:
        logger.warning(
            "hermes ingest: conversation %s has %d speakers; falling back to "
            "round-robin pairing",
            conversation.conversation_id,
            len(speakers),
        )

    i = 0
    while i < len(messages):
        user = messages[i].content
        assistant = messages[i + 1].content if i + 1 < len(messages) else ""
        yield user, assistant
        i += 2
