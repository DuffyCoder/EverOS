"""Validate repository compatibility mirrors declared in the YAML manifest."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "evaluation" / "config" / "repository_mirrors.yaml"
REQUIRED_FIELDS = {"id", "canonical", "mirrors", "strategy", "reason"}


def _resolve_repository_file(repo_root: Path, raw_path: Any, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"{label} must be a non-empty repository-relative path")

    posix_path = PurePosixPath(raw_path)
    windows_path = PureWindowsPath(raw_path)
    if posix_path.is_absolute():
        raise ValueError(f"{label} must not be an absolute path: {raw_path}")
    if windows_path.drive or windows_path.root:
        raise ValueError(f"{label} crosses a Windows path boundary: {raw_path}")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{label} must not escape the repository: {raw_path}")

    repository_file = repo_root.joinpath(*posix_path.parts)
    if not repository_file.is_file():
        raise ValueError(f"{label} does not exist: {raw_path}")
    return repository_file


def _validate_manifest(manifest_path: Path, repo_root: Path) -> list[dict[str, Any]]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError("repository mirror manifest must be a top-level list")

    seen_ids: set[str] = set()
    seen_groups: set[tuple[Path, tuple[Path, ...]]] = set()
    for index, entry in enumerate(manifest):
        label = f"entry {index}"
        if not isinstance(entry, dict) or set(entry) != REQUIRED_FIELDS:
            raise ValueError(f"{label} must contain exactly {sorted(REQUIRED_FIELDS)}")

        mirror_id = entry["id"]
        if not isinstance(mirror_id, str) or not mirror_id.strip():
            raise ValueError(f"{label} id must be a non-empty string")
        if mirror_id in seen_ids:
            raise ValueError(f"duplicate mirror id: {mirror_id}")
        seen_ids.add(mirror_id)

        if entry["strategy"] != "exact":
            raise ValueError(f"{mirror_id} uses unsupported strategy")
        reason = entry["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{mirror_id} reason must be non-empty")

        canonical = _resolve_repository_file(
            repo_root, entry["canonical"], f"{mirror_id} canonical"
        )
        mirrors = entry["mirrors"]
        if not isinstance(mirrors, list) or not mirrors:
            raise ValueError(f"{mirror_id} mirrors must be a non-empty list")

        resolved_mirrors: list[Path] = []
        for mirror_index, raw_mirror in enumerate(mirrors):
            mirror = _resolve_repository_file(
                repo_root, raw_mirror, f"{mirror_id} mirror {mirror_index}"
            )
            if mirror == canonical:
                raise ValueError(f"{mirror_id} repeats its canonical path as a mirror")
            if mirror.read_bytes() != canonical.read_bytes():
                raise ValueError(
                    f"{mirror_id} mirror is not byte-identical: {raw_mirror}"
                )
            resolved_mirrors.append(mirror)

        group = (canonical, tuple(resolved_mirrors))
        if group in seen_groups:
            raise ValueError(f"duplicate mirror group: {entry['canonical']}")
        seen_groups.add(group)

    return manifest


def _valid_entry() -> dict[str, Any]:
    return {
        "id": "example",
        "canonical": "canonical.txt",
        "mirrors": ["mirror.txt"],
        "strategy": "exact",
        "reason": "Retain a legacy path.",
    }


def _write_fixture_manifest(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    (tmp_path / "canonical.txt").write_bytes(b"same bytes\n")
    (tmp_path / "mirror.txt").write_bytes(b"same bytes\n")
    manifest_path = tmp_path / "repository_mirrors.yaml"
    manifest_path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return manifest_path


def test_repository_manifest_declares_valid_known_mirrors() -> None:
    manifest = _validate_manifest(MANIFEST_PATH, REPO_ROOT)
    declared = [(entry["canonical"], tuple(entry["mirrors"])) for entry in manifest]
    assert declared == [
        ("evaluation/data/locomo/locomo10.json", ("data/locomo10.json",)),
        (
            "openclaw-eval/container/openclaw_eval_bridge_lib.mjs",
            ("evaluation/scripts/openclaw_eval_bridge_lib.mjs",),
        ),
        (
            "openclaw-eval/plugins/evermemos/src/prompt-builders.ts",
            ("openclaw-eval/plugins/mem0/src/prompt-builders.ts",),
        ),
    ]


def test_validator_rejects_absolute_path(tmp_path: Path) -> None:
    entry = _valid_entry()
    entry["canonical"] = str(tmp_path / "canonical.txt")
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="absolute path"):
        _validate_manifest(manifest_path, tmp_path)


@pytest.mark.parametrize(
    "raw_path",
    [
        pytest.param(r"C:\outside", id="drive-absolute"),
        pytest.param(r"\\server\share\outside", id="unc"),
        pytest.param(r"\outside", id="rooted-without-drive"),
        pytest.param(r"D:outside", id="drive-relative"),
    ],
)
def test_validator_rejects_windows_anchored_path(tmp_path: Path, raw_path: str) -> None:
    entry = _valid_entry()
    entry["canonical"] = raw_path
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="Windows path boundary"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_parent_escape(tmp_path: Path) -> None:
    entry = _valid_entry()
    entry["canonical"] = "../canonical.txt"
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="escape the repository"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_missing_file(tmp_path: Path) -> None:
    entry = _valid_entry()
    entry["mirrors"] = ["missing.txt"]
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="does not exist"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_duplicate_id(tmp_path: Path) -> None:
    entries = [_valid_entry(), _valid_entry()]
    manifest_path = _write_fixture_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="duplicate mirror id"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_duplicate_group_with_different_id(tmp_path: Path) -> None:
    duplicate = _valid_entry()
    duplicate["id"] = "different-id"
    manifest_path = _write_fixture_manifest(tmp_path, [_valid_entry(), duplicate])

    with pytest.raises(ValueError, match="duplicate mirror group"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_canonical_repeated_as_mirror(tmp_path: Path) -> None:
    entry = _valid_entry()
    entry["mirrors"] = ["canonical.txt"]
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="repeats its canonical path"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_unsupported_strategy(tmp_path: Path) -> None:
    entry = _valid_entry()
    entry["strategy"] = "copy"
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="unsupported strategy"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_byte_inequality(tmp_path: Path) -> None:
    manifest_path = _write_fixture_manifest(tmp_path, [_valid_entry()])
    (tmp_path / "mirror.txt").write_bytes(b"different bytes\n")

    with pytest.raises(ValueError, match="not byte-identical"):
        _validate_manifest(manifest_path, tmp_path)


def test_validator_rejects_empty_reason(tmp_path: Path) -> None:
    entry = _valid_entry()
    entry["reason"] = "   "
    manifest_path = _write_fixture_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="reason must be non-empty"):
        _validate_manifest(manifest_path, tmp_path)
