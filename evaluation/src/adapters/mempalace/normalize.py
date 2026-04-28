"""Minimal transcript normalization used by the evaluation MemPalace package.

The evaluation adapter writes plain-text transcript files that already match
MemPalace's expected `> user` exchange format, so the only behavior needed
here is the same fast-path the upstream normalizer would take: read the file
and return its content unchanged.
"""

import os


def normalize(filepath: str) -> str:
    try:
        file_size = os.path.getsize(filepath)
    except OSError as e:
        raise IOError(f"Could not read {filepath}: {e}")
    if file_size > 500 * 1024 * 1024:
        raise IOError(f"File too large ({file_size // (1024 * 1024)} MB): {filepath}")
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        raise IOError(f"Could not read {filepath}: {e}")
