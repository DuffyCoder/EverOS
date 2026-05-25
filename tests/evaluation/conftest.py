"""Shared pytest fixtures for the evaluation test suite.

`_restore_environ` (autouse) snapshots ``os.environ`` before each test and
restores it after. Some tests mutate process env (directly or via an imported
module's import-time side effects); without this, the leaked state — e.g. a
clobbered ``PATH`` — made ``test_openclaw_runtime`` fail with ``spawn node
ENOENT`` only in the full-suite run (it passed in isolation). Snapshotting per
test keeps every test starting from the same env as session start.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _restore_environ():
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
