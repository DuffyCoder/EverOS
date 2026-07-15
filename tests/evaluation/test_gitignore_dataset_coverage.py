"""Regression test for repository-local .gitignore policy.

Background: `evaluation/data/longmemeval/longmemeval_s_locomo_style.json`
is a 265MB generated dataset that exceeds GitHub's 100MB per-file limit.
Without an ignore rule, an unwary `git add evaluation/data/...` would
stage it and the subsequent push would be rejected.

This test exercises `git check-ignore` to confirm the rules are in place
and precise: generated datasets, local reports, logs, and agent scratch
are ignored while bundled data, policy templates, and shared editor
configuration remain trackable.
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
    ".env.bak.1778308789",
    ".claire/worktrees/example/output.py",
    ".runlogs/eval.log",
    ".runlogs/session.json",
    "docs/evaluation/analysis/local-report.md",
    ".vscode/local.code-workspace",
    ".claude/private/session.json",
    # Expected NOT ignored:
    "evaluation/data/longmemeval/.gitkeep",
    "evaluation/data/locomo/locomo10.json",
    "docs/evaluation/analysis/README.md",
    "docs/evaluation/analysis/TEMPLATE.md",
    ".vscode/launch.json",
    ".vscode/settings.json",
    ".claude/setup.sh",
    ".claude/rules/example.md",
    ".claude/skills/example/SKILL.md",
]


@pytest.fixture(scope="module")
def ignored_map() -> dict[str, bool]:
    """One `git check-ignore` invocation across all paths instead of N
    subprocess starts. `--non-matching --verbose` outputs both ignored
    and non-ignored entries, so we can build a complete map in a single
    call. `--no-index` also checks policy for already-tracked paths. A
    non-matching line is prefixed with '::' (no source rule)."""
    if shutil.which("git") is None:
        pytest.skip("git CLI not available")
    result = subprocess.run(
        [
            "git", "-C", str(REPO_ROOT),
            "check-ignore", "--verbose", "--non-matching", "--no-index", "--",
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
        pattern = prefix.rsplit(":", 1)[-1]
        out[path] = not prefix.startswith("::") and not pattern.startswith("!")
    return out


@pytest.mark.parametrize(
    "path,expected_ignored",
    [
        ("evaluation/data/longmemeval/longmemeval_s_locomo_style.json", True),
        ("evaluation/data/longmemeval/longmemeval_s_cleaned.json", True),
        ("evaluation/data/longmemeval/some_future_export.json", True),
        ("evaluation/data/longmemeval/another.jsonl", True),
        (".env.bak.1778308789", True),
        (".claire/worktrees/example/output.py", True),
        (".runlogs/eval.log", True),
        (".runlogs/session.json", True),
        ("docs/evaluation/analysis/local-report.md", True),
        (".vscode/local.code-workspace", True),
        (".claude/private/session.json", True),
        ("evaluation/data/longmemeval/.gitkeep", False),
        ("evaluation/data/locomo/locomo10.json", False),
        ("docs/evaluation/analysis/README.md", False),
        ("docs/evaluation/analysis/TEMPLATE.md", False),
        (".vscode/launch.json", False),
        (".vscode/settings.json", False),
        (".claude/setup.sh", False),
        (".claude/rules/example.md", False),
        (".claude/skills/example/SKILL.md", False),
    ],
    ids=lambda p: p if isinstance(p, str) else None,
)
def test_gitignore_coverage(
    ignored_map: dict[str, bool], path: str, expected_ignored: bool
) -> None:
    """Verify each path's tracked/ignored status matches expectation.

    Cases assert generated and private artifacts are ignored while
    intentional repository-owned files stay trackable.
    """
    assert path in ignored_map, f"{path} missing from check-ignore output"
    assert ignored_map[path] is expected_ignored
