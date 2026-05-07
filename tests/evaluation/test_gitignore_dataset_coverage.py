"""Regression test for .gitignore coverage of operator-fetched datasets.

Background: `evaluation/data/longmemeval/longmemeval_s_locomo_style.json`
is a 265MB generated dataset that exceeds GitHub's 100MB per-file limit.
Without an ignore rule, an unwary `git add evaluation/data/...` would
stage it and the subsequent push would be rejected.

This test exercises `git check-ignore` to confirm the rule is in place
and is precise: longmemeval json/jsonl is ignored, but the small bundled
`locomo10.json` and `.gitkeep` placeholders remain trackable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

_PATHS = [
    # Expected ignored:
    "evaluation/data/longmemeval/longmemeval_s_locomo_style.json",
    "evaluation/data/longmemeval/longmemeval_s_cleaned.json",
    "evaluation/data/longmemeval/some_future_export.json",
    "evaluation/data/longmemeval/another.jsonl",
    # Expected NOT ignored:
    "evaluation/data/longmemeval/.gitkeep",
    "evaluation/data/locomo/locomo10.json",
]


@pytest.fixture(scope="module")
def ignored_map() -> dict[str, bool]:
    """One `git check-ignore` invocation across all paths instead of N
    subprocess starts. `--non-matching --verbose` outputs both ignored
    and non-ignored entries, so we can build a complete map in a single
    call. A non-matching line is prefixed with '::' (no source rule)."""
    if shutil.which("git") is None:
        pytest.skip("git CLI not available")
    result = subprocess.run(
        [
            "git", "-C", str(REPO_ROOT),
            "check-ignore", "--verbose", "--non-matching", "--",
            *_PATHS,
        ],
        capture_output=True,
        text=True,
    )
    # Exit 0: at least one path matched. Exit 1: none matched. Both fine.
    if result.returncode not in (0, 1):
        pytest.fail(f"git check-ignore errored: {result.stderr}")
    out: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        # Format with --verbose: "<source>:<line>:<pattern>\t<path>"
        # When --non-matching, ignored=False rows have empty source: "::\t<path>"
        if "\t" not in line:
            continue
        prefix, path = line.rsplit("\t", 1)
        out[path] = not prefix.startswith("::")
    return out


@pytest.mark.parametrize(
    "path,expected_ignored",
    [
        ("evaluation/data/longmemeval/longmemeval_s_locomo_style.json", True),
        ("evaluation/data/longmemeval/longmemeval_s_cleaned.json", True),
        ("evaluation/data/longmemeval/some_future_export.json", True),
        ("evaluation/data/longmemeval/another.jsonl", True),
        ("evaluation/data/longmemeval/.gitkeep", False),
        ("evaluation/data/locomo/locomo10.json", False),
    ],
    ids=lambda p: p if isinstance(p, str) else None,
)
def test_gitignore_coverage(
    ignored_map: dict[str, bool], path: str, expected_ignored: bool
) -> None:
    """Verify each path's tracked/ignored status matches expectation.

    Cases assert: the 265MB dataset and its symlink are ignored;
    arbitrary future longmemeval json/jsonl artifacts are also ignored;
    `.gitkeep` placeholder stays trackable; the bundled locomo dataset
    is unaffected by the longmemeval rule.
    """
    assert path in ignored_map, f"{path} missing from check-ignore output"
    assert ignored_map[path] is expected_ignored
