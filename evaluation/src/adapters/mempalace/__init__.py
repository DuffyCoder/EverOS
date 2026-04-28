"""Minimal internal MemPalace package for evaluation.

This package intentionally vendors only the pieces required by
`evaluation.src.adapters.mempalace_adapter`:

- `convo_miner.mine_convos`
- `searcher.search_memories`

The goal is to keep evaluation self-contained while preserving the
runtime behavior of the MemPalace paths used by the adapter.
"""

from .convo_miner import mine_convos
from .convo_closet_miner import mine_convos_with_closets
from .searcher import search_memories

__all__ = ["mine_convos", "mine_convos_with_closets", "search_memories"]
