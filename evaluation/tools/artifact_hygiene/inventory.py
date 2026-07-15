"""Read-only inventory helpers for local evaluation artifacts."""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_DELETE_CANDIDATE_MARKERS = ("smoke", "debug", "retry", "calib", "test", "incomplete")
_ANALYSIS_REFERENCE_SCAN_BYTES = 256 * 1024
_ANALYSIS_REFERENCE_READ_CHUNK = 64 * 1024


def classify_result(
    name: str,
    *,
    referenced: bool,
    pinned: bool,
    successful: bool,
    is_latest_success: bool,
) -> dict[str, Any]:
    """Classify one result conservatively using explicit precedence."""

    if pinned:
        return {"decision": "keep", "reasons": ["pinned"]}
    if referenced:
        return {"decision": "keep", "reasons": ["referenced"]}
    if successful and is_latest_success:
        return {"decision": "keep", "reasons": ["latest_success"]}

    lowered_name = name.casefold()
    for marker in _DELETE_CANDIDATE_MARKERS:
        if marker in lowered_name:
            return {
                "decision": "delete_candidate",
                "reasons": [f"name_marker:{marker}"],
            }

    return {"decision": "review", "reasons": ["manual_review"]}


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _entry_type(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _tree_size(path: Path) -> int:
    """Return regular-file bytes below *path* without following symlinks."""

    try:
        path_stat = path.lstat()
    except OSError:
        return 0
    if stat.S_ISLNK(path_stat.st_mode):
        return 0
    if stat.S_ISREG(path_stat.st_mode):
        return path_stat.st_size
    if not stat.S_ISDIR(path_stat.st_mode):
        return 0

    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(entry_stat.st_mode):
                    total += entry_stat.st_size
                elif stat.S_ISDIR(entry_stat.st_mode):
                    total += _tree_size(Path(entry.path))
    except OSError:
        return total
    return total


def _metadata(path: Path, repo_root: Path) -> dict[str, Any]:
    path_stat = path.lstat()
    entry_type = _entry_type(path_stat.st_mode)
    size = _tree_size(path) if entry_type == "directory" else path_stat.st_size
    return {
        "path": path.relative_to(repo_root).as_posix(),
        "size": size,
        "mode": stat.S_IMODE(path_stat.st_mode),
        "mtime": path_stat.st_mtime,
        "type": entry_type,
    }


def _real_repo_directory(repo_root: Path, *parts: str) -> Path | None:
    """Resolve a repo-relative directory only through real directory components."""

    current = repo_root
    for part in parts:
        if part in {"", ".", ".."}:
            return None
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            return None
    return current


def _safe_keep_entry(raw_entry: str) -> str | None:
    entry = raw_entry.split("#", 1)[0].strip()
    if not entry:
        return None
    posix_path = PurePosixPath(entry)
    windows_path = PureWindowsPath(entry)
    if posix_path.is_absolute() or windows_path.drive or windows_path.root:
        return None
    if ".." in posix_path.parts or ".." in windows_path.parts:
        return None
    normalized = posix_path.as_posix()
    return None if normalized in {"", "."} else normalized


def _read_keep_entries(repo_root: Path) -> list[str]:
    archives_root = _real_repo_directory(repo_root, "evaluation", "archives")
    if archives_root is None:
        return []
    keep_path = archives_root / "KEEP"
    try:
        keep_stat = keep_path.lstat()
    except OSError:
        return []
    if not stat.S_ISREG(keep_stat.st_mode):
        return []
    try:
        lines = keep_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    entries = {_safe_keep_entry(line) for line in lines}
    return sorted(entry for entry in entries if entry is not None)


def _environment_backups(repo_root: Path) -> list[dict[str, Any]]:
    candidates: dict[str, Path] = {}
    try:
        root_entries = list(repo_root.iterdir())
    except OSError:
        return []

    for path in root_entries:
        if path.name.startswith(".env.bak"):
            candidates[path.relative_to(repo_root).as_posix()] = path
        try:
            path_stat = path.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(path_stat.st_mode):
            continue
        try:
            shallow_entries = path.iterdir()
            for shallow_path in shallow_entries:
                if shallow_path.name.startswith(".env.bak"):
                    candidates[shallow_path.relative_to(repo_root).as_posix()] = (
                        shallow_path
                    )
        except OSError:
            continue

    metadata: list[dict[str, Any]] = []
    for relative_path in sorted(candidates):
        try:
            metadata.append(_metadata(candidates[relative_path], repo_root))
        except OSError:
            continue
    return metadata


def _walk_files_without_symlinks(root: Path) -> list[Path]:
    files: list[Path] = []
    try:
        with os.scandir(root) as entries:
            sorted_entries = sorted(entries, key=lambda entry: entry.name)
    except OSError:
        return files
    for entry in sorted_entries:
        path = Path(entry.path)
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
            files.append(path)
        elif stat.S_ISDIR(entry_stat.st_mode):
            files.extend(_walk_files_without_symlinks(path))
    return files


def _read_markdown_reference_prefix(path: Path) -> tuple[str, dict[str, Any]]:
    """Read at most the configured Markdown prefix without following the file."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            return "", {"status": "error", "error": "not_regular_file"}

        chunks: list[bytes] = []
        remaining = _ANALYSIS_REFERENCE_SCAN_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(_ANALYSIS_REFERENCE_READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        truncated = len(payload) > _ANALYSIS_REFERENCE_SCAN_BYTES
        bounded_payload = payload[:_ANALYSIS_REFERENCE_SCAN_BYTES]
        scan = {
            "status": "truncated" if truncated else "complete",
            "bytes_scanned": len(bounded_payload),
            "byte_limit": _ANALYSIS_REFERENCE_SCAN_BYTES,
        }
        return bounded_payload.decode("utf-8", errors="replace"), scan
    except OSError as exc:
        return "", {"status": "error", "error": f"read_failed:{exc.errno}"}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _analysis_inventory(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    analysis_root = _real_repo_directory(repo_root, "docs", "evaluation", "analysis")
    if analysis_root is None:
        return [], []
    inventory: list[dict[str, Any]] = []
    reference_texts: list[str] = []
    for path in _walk_files_without_symlinks(analysis_root):
        try:
            path_stat = path.lstat()
            metadata = _metadata(path, repo_root)
            if stat.S_ISREG(path_stat.st_mode) and path.suffix.casefold() == ".md":
                reference_text, scan = _read_markdown_reference_prefix(path)
                if reference_text:
                    reference_texts.append(reference_text)
                metadata["reference_scan"] = scan
            elif stat.S_ISREG(path_stat.st_mode):
                metadata["reference_scan"] = {
                    "status": "not_scanned",
                    "reason": "non_markdown",
                }
            else:
                metadata["reference_scan"] = {
                    "status": "not_scanned",
                    "reason": "not_regular_file",
                }
            inventory.append(metadata)
        except OSError:
            continue
    inventory.sort(key=lambda entry: entry["path"])
    return inventory, reference_texts


def _top_level_inventory(
    root: Path, repo_root: Path, *, excluded_names: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    try:
        relative_root = root.relative_to(repo_root)
    except ValueError:
        return []
    safe_root = _real_repo_directory(repo_root, *relative_root.parts)
    if safe_root is None:
        return []
    try:
        paths = sorted(safe_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    inventory: list[dict[str, Any]] = []
    for path in paths:
        if path.name in excluded_names:
            continue
        try:
            inventory.append(_metadata(path, repo_root))
        except OSError:
            continue
    return inventory


def _is_successful_result(result_path: Path) -> bool:
    if result_path.is_symlink():
        return False
    try:
        result_stat = result_path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(result_stat.st_mode):
        return False
    try:
        entries = list(result_path.iterdir())
    except OSError:
        return False
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            continue
        lowered_name = entry.name.casefold()
        if lowered_name in {"_success", "success"}:
            return True
        if entry.suffix.casefold() == ".json" and lowered_name.startswith(
            ("metrics", "summary", "eval_results", "evaluation_results")
        ):
            return True
    return False


def _is_pinned_result(
    result_path: str, result_name: str, keep_entries: list[str]
) -> bool:
    aliases = {result_path, result_name, f"results/{result_name}"}
    return any(
        entry in aliases or entry.startswith(f"{result_path}/")
        for entry in keep_entries
    )


def _parse_worktrees(output: str) -> list[dict[str, Any]]:
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value if separator else ""
        elif key == "HEAD":
            current["head"] = value if separator else ""
        elif key == "branch":
            current["branch"] = value if separator else ""
        elif key in {"bare", "detached"}:
            current[key] = True
        elif key in {"locked", "prunable"}:
            current[key] = value if separator else True
    return sorted(worktrees, key=lambda entry: entry.get("path", ""))


def _registered_worktrees(repo_root: Path) -> tuple[list[dict[str, Any]], str | None]:
    command = ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        return [], f"git worktree unavailable: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        return [], f"git worktree failed with exit {completed.returncode}{suffix}"
    return _parse_worktrees(completed.stdout), None


def build_inventory(repo_root: Path | str) -> dict[str, Any]:
    """Build a read-only, JSON-friendly inventory below *repo_root*."""

    resolved_root = Path(repo_root).expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"repository root is not a directory: {resolved_root}")

    keep_entries = _read_keep_entries(resolved_root)
    analysis_files, reference_texts = _analysis_inventory(resolved_root)
    analysis_reference_scan_incomplete = any(
        entry.get("reference_scan", {}).get("status") in {"truncated", "error"}
        for entry in analysis_files
    )
    worktrees, worktree_error = _registered_worktrees(resolved_root)
    result_metadata = _top_level_inventory(
        resolved_root / "evaluation" / "results", resolved_root
    )

    success_paths = {
        entry["path"]
        for entry in result_metadata
        if _is_successful_result(resolved_root / entry["path"])
    }

    results: list[dict[str, Any]] = []
    for metadata in result_metadata:
        result_path = metadata["path"]
        result_name = PurePosixPath(result_path).name
        referenced = any(
            result_path in reference_text or result_name in reference_text
            for reference_text in reference_texts
        )
        pinned = _is_pinned_result(result_path, result_name, keep_entries)
        successful = result_path in success_paths
        # A "latest success" is meaningful only within a reliable
        # dataset/system/config group. Result paths do not currently expose
        # trustworthy grouping metadata, so inventory must not infer this from
        # one global modification-time ordering.
        is_latest_success = False
        classification = classify_result(
            result_name,
            referenced=referenced,
            pinned=pinned,
            successful=successful,
            is_latest_success=is_latest_success,
        )
        if (
            classification["decision"] == "delete_candidate"
            and analysis_reference_scan_incomplete
        ):
            classification = {
                "decision": "review",
                "reasons": ["analysis_reference_scan_incomplete"],
            }
        results.append(
            {
                **metadata,
                "name": result_name,
                "referenced": referenced,
                "pinned": pinned,
                "successful": successful,
                "is_latest_success": is_latest_success,
                **classification,
            }
        )

    return {
        "schema": "evaluation-artifact-inventory/v1",
        "generated_at": _utc_timestamp(),
        "repo_root": str(resolved_root),
        "registered_worktrees": worktrees,
        "worktree_error": worktree_error,
        "environment_backups": _environment_backups(resolved_root),
        "results": results,
        "archives": _top_level_inventory(
            resolved_root / "evaluation" / "archives",
            resolved_root,
            excluded_names=frozenset({"KEEP"}),
        ),
        "analysis_files": analysis_files,
        "keep_entries": keep_entries,
    }
