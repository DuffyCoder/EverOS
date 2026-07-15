"""Plan and explicitly execute compact, non-destructive result archives."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _NONBLOCK | _CLOEXEC
_SOURCE_OPEN_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC
_DESTINATION_OPEN_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _NONBLOCK | _CLOEXEC
)
_COPY_CHUNK_BYTES = 1024 * 1024
_RENAME_NOREPLACE = 1

_LIGHTWEIGHT_EVIDENCE_SUFFIXES = {
    ".json",
    ".txt",
    ".log",
    ".yaml",
    ".yml",
    ".md",
    ".csv",
}
_COMPILED_PYTHON_SUFFIXES = {".pyc", ".pyo"}
_PRUNED_COMPONENTS = {
    "openclaw_workspaces",
    "node_modules",
    ".cache",
    ".venv",
    "pycache",
    "vectors",
    "vector",
    "vector_index",
    "vector_indexes",
    "vector_indices",
    "vector_store",
    "vectorstore",
    "faiss",
    "faiss_index",
    "bm25",
    "bm25_index",
    "bm25_indexes",
    "bm25_indices",
    "bm25_cache",
    "model_cache",
    "models_cache",
    "huggingface_cache",
    "transformers_cache",
    "torch_cache",
}


def _normalized_name(name: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "_", name.casefold()).strip("_")


def _pruned_component_reason(name: str) -> str | None:
    normalized = _normalized_name(name)
    if normalized in _PRUNED_COMPONENTS:
        return f"pruned_component:{normalized}"
    if normalized.startswith(("vector_index_", "bm25_index_", "model_cache_")):
        return f"pruned_component:{normalized}"
    return None


def _inclusion_reason(relative_path: PurePosixPath) -> str | None:
    suffix = relative_path.suffix.casefold()
    normalized_stem = _normalized_name(relative_path.stem)
    normalized_parts = tuple(_normalized_name(part) for part in relative_path.parts)

    if suffix == ".log":
        return "log_evidence"
    if any(part in {"log", "logs"} for part in normalized_parts[:-1]):
        if suffix in _LIGHTWEIGHT_EVIDENCE_SUFFIXES:
            return "log_evidence"
    if normalized_parts and normalized_parts[0] == "artifacts":
        if suffix in _LIGHTWEIGHT_EVIDENCE_SUFFIXES:
            return "artifacts_lightweight_evidence"
    if suffix == ".json":
        for prefix in ("answer", "search", "eval"):
            if normalized_stem.startswith(prefix):
                return f"{prefix}_json_evidence"
    if "resolved" in normalized_stem and "config" in normalized_stem:
        if suffix in {".json", ".yaml", ".yml"}:
            return "resolved_config"
    if "report" in normalized_stem and suffix in {".txt", ".md"}:
        return "report_evidence"
    if len(relative_path.parts) == 1 and suffix == ".json":
        stem_tokens = set(normalized_stem.split("_"))
        for evidence_kind in ("metrics", "summary", "diagnostics", "latency"):
            if evidence_kind in stem_tokens:
                return f"top_level_{evidence_kind}_json"
        if "content_overlap" in normalized_stem:
            return "top_level_content_overlap_json"
    return None


def _validate_archive_paths(
    result: Path | str, archive_root: Path | str
) -> tuple[Path, Path, Path]:
    source_input = Path(result).expanduser()
    try:
        source_stat = source_input.lstat()
    except OSError as exc:
        raise ValueError(
            f"source must be an existing regular directory: {source_input}"
        ) from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
        raise ValueError(
            f"source must be an existing regular directory: {source_input}"
        )

    source = source_input.resolve(strict=True)
    root_input = Path(archive_root).expanduser()
    if root_input.exists() and not root_input.is_dir():
        raise NotADirectoryError(f"archive root is not a directory: {root_input}")
    resolved_archive_root = root_input.resolve(strict=False)
    destination = (resolved_archive_root / source.name).resolve(strict=False)

    try:
        destination.relative_to(resolved_archive_root)
    except ValueError as exc:
        raise ValueError("archive destination escapes the archive root") from exc
    if os.path.lexists(destination):
        raise FileExistsError(f"archive destination already exists: {destination}")
    if destination == source or destination.is_relative_to(source):
        raise ValueError(f"archive destination is inside the source: {destination}")
    return source, resolved_archive_root, destination


def _open_absolute_directory_no_follow(
    path: Path, *, create: bool, purpose: str
) -> int:
    """Open an absolute directory path one component at a time."""

    if os.name != "posix" or not path.is_absolute():
        raise OSError(
            errno.ENOTSUP,
            "secure artifact archives require POSIX absolute-path dirfd support",
        )

    descriptor = os.open(path.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError(f"unsafe {purpose} directory component: {component!r}")
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child_descriptor = os.open(
                    component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                )
            except OSError as exc:
                raise ValueError(
                    f"{purpose} path contains a symlink or non-directory component: {path}"
                ) from exc
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _walk_archive_plan_fd(
    directory_fd: int,
    relative_directory: PurePosixPath,
    included: list[dict[str, Any]],
    excluded: list[dict[str, str]],
) -> None:
    try:
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as exc:
        relative = relative_directory.as_posix()
        excluded.append(
            {"path": relative, "reason": f"directory_unreadable:{exc.errno}"}
        )
        return

    for name in names:
        relative = relative_directory / name
        relative_string = relative.as_posix()
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            excluded.append(
                {"path": relative_string, "reason": f"metadata_unavailable:{exc.errno}"}
            )
            continue

        if stat.S_ISLNK(entry_stat.st_mode):
            excluded.append({"path": relative_string, "reason": "symlink_not_followed"})
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            prune_reason = _pruned_component_reason(name)
            if prune_reason is not None:
                excluded.append({"path": relative_string, "reason": prune_reason})
                continue
            try:
                child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                excluded.append(
                    {
                        "path": relative_string,
                        "reason": f"directory_unreadable:{exc.errno}",
                    }
                )
                continue
            excluded.append({"path": relative_string, "reason": "traversed_directory"})
            try:
                _walk_archive_plan_fd(child_fd, relative, included, excluded)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            excluded.append(
                {"path": relative_string, "reason": "unsupported_file_type"}
            )
            continue
        if relative.suffix.casefold() in _COMPILED_PYTHON_SUFFIXES:
            excluded.append({"path": relative_string, "reason": "compiled_python"})
            continue

        reason = _inclusion_reason(relative)
        if reason is None:
            excluded.append({"path": relative_string, "reason": "not_compact_evidence"})
        else:
            included.append(
                {"path": relative_string, "size": entry_stat.st_size, "reason": reason}
            )


def plan_archive(result: Path | str, archive_root: Path | str) -> dict[str, Any]:
    """Return a JSON-friendly compact archive plan without writing anything."""

    source, _, destination = _validate_archive_paths(result, archive_root)
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    source_fd = _open_absolute_directory_no_follow(
        source, create=False, purpose="archive source"
    )
    try:
        _walk_archive_plan_fd(source_fd, PurePosixPath("."), included, excluded)
    finally:
        os.close(source_fd)
    included.sort(key=lambda entry: entry["path"])
    excluded.sort(key=lambda entry: entry["path"])
    plan = {
        "schema_version": "1",
        "source": str(source),
        "destination": str(destination),
        "included": included,
        "excluded": excluded,
    }
    serialized_plan = json.dumps(
        plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    plan["plan_digest"] = hashlib.sha256(serialized_plan).hexdigest()
    return plan


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _git_sha(source: Path) -> tuple[str, str | None]:
    command = ["git", "-C", str(source), "rev-parse", "HEAD"]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        return "unknown", f"git unavailable: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        return (
            "unknown",
            f"git rev-parse failed with exit {completed.returncode}{suffix}",
        )
    sha = completed.stdout.strip()
    if not sha:
        return "unknown", "git rev-parse returned an empty SHA"
    return sha, None


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    relative_path = PurePosixPath(raw_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"archive entry is not a safe relative path: {raw_path}")
    if relative_path.as_posix() in {"", "."}:
        raise ValueError("archive entry path must not be empty")
    return relative_path


def _open_relative_directory_at(
    root_fd: int, components: tuple[str, ...], *, create: bool, purpose: str
) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in components:
            if component in {"", ".", ".."}:
                raise ValueError(f"unsafe {purpose} component: {component!r}")
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child_descriptor = os.open(
                    component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                )
            except OSError as exc:
                raise ValueError(
                    f"{purpose} contains a symlink or non-directory component: "
                    f"{'/'.join(components)}"
                ) from exc
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_source_entry_at(
    source_root_fd: int, relative_path: PurePosixPath, planned_size: int
) -> tuple[int, os.stat_result]:
    parent_fd = _open_relative_directory_at(
        source_root_fd,
        tuple(relative_path.parts[:-1]),
        create=False,
        purpose=f"archive source ancestor for {relative_path}",
    )
    try:
        descriptor = os.open(relative_path.name, _SOURCE_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(
            f"archive source entry is not a regular file: {relative_path}"
        ) from exc
    finally:
        os.close(parent_fd)

    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError(
                f"archive source entry is not a regular file: {relative_path}"
            )
        if opened_stat.st_size != planned_size:
            raise ValueError(
                f"archive source size changed since plan for {relative_path}: "
                f"planned {planned_size}, opened {opened_stat.st_size}"
            )
        return descriptor, opened_stat
    except BaseException:
        os.close(descriptor)
        raise


def _stable_file_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short write while creating artifact archive")
        remaining = remaining[written:]


def _hash_open_file(descriptor: int) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _copy_archive_entry(
    source_root_fd: int, staging_fd: int, entry: dict[str, Any]
) -> dict[str, Any]:
    relative_path = _safe_relative_path(entry["path"])
    planned_size = entry.get("size")
    if not isinstance(planned_size, int) or planned_size < 0:
        raise ValueError(f"archive entry has an invalid planned size: {relative_path}")

    source_fd, source_stat = _open_source_entry_at(
        source_root_fd, relative_path, planned_size
    )
    destination_parent_fd: int | None = None
    destination_fd: int | None = None
    try:
        destination_parent_fd = _open_relative_directory_at(
            staging_fd,
            tuple(relative_path.parts[:-1]),
            create=True,
            purpose=f"archive staging ancestor for {relative_path}",
        )
        destination_fd = os.open(
            relative_path.name,
            _DESTINATION_OPEN_FLAGS,
            stat.S_IMODE(source_stat.st_mode) & 0o777,
            dir_fd=destination_parent_fd,
        )
        destination_stat = os.fstat(destination_fd)
        if not stat.S_ISREG(destination_stat.st_mode):
            raise ValueError(
                f"archive destination entry is not a regular file: {relative_path}"
            )
        os.fchmod(destination_fd, stat.S_IMODE(source_stat.st_mode) & 0o777)

        digest = hashlib.sha256()
        copied_size = 0
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
            digest.update(chunk)
            copied_size += len(chunk)

        final_source_stat = os.fstat(source_fd)
        if _stable_file_identity(final_source_stat) != _stable_file_identity(
            source_stat
        ):
            raise ValueError(f"archive source changed during copy for {relative_path}")
        if copied_size != planned_size:
            raise ValueError(
                f"archive source size changed during copy for {relative_path}: "
                f"planned {planned_size}, copied {copied_size}"
            )

        os.fsync(destination_fd)
        copied_digest = digest.hexdigest()
        verified_size, verified_digest = _hash_open_file(destination_fd)
        if verified_size != copied_size or not hmac.compare_digest(
            verified_digest, copied_digest
        ):
            raise OSError(f"archive verification failed for {relative_path}")
        os.fsync(destination_parent_fd)
        return {
            "path": relative_path.as_posix(),
            "size": copied_size,
            "sha256": copied_digest,
            "reason": entry["reason"],
        }
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)


def _manifest_payload(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_manifest_at(staging_fd: int, manifest: dict[str, Any]) -> None:
    payload = _manifest_payload(manifest)
    descriptor = os.open(
        "manifest.json", _DESTINATION_OPEN_FLAGS, 0o600, dir_fd=staging_fd
    )
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError("archive manifest destination is not a regular file")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        verified_size, verified_digest = _hash_open_file(descriptor)
        expected_digest = hashlib.sha256(payload).hexdigest()
        if verified_size != len(payload) or not hmac.compare_digest(
            verified_digest, expected_digest
        ):
            raise OSError("archive manifest verification failed")
    finally:
        os.close(descriptor)
    os.fsync(staging_fd)


def _open_staging_regular_at(
    staging_fd: int, relative_path: PurePosixPath
) -> tuple[int, os.stat_result]:
    parent_fd = _open_relative_directory_at(
        staging_fd,
        tuple(relative_path.parts[:-1]),
        create=False,
        purpose=f"archive staging ancestor for {relative_path}",
    )
    try:
        descriptor = os.open(relative_path.name, _SOURCE_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(
            f"archive staging entry is not a regular file: {relative_path}"
        ) from exc
    finally:
        os.close(parent_fd)

    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError(
                f"archive staging entry is not a regular file: {relative_path}"
            )
        return descriptor, opened_stat
    except BaseException:
        os.close(descriptor)
        raise


def _collect_staging_tree(
    directory_fd: int,
    relative_directory: PurePosixPath,
    directories: set[str],
    files: set[str],
) -> None:
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        relative_path = relative_directory / name
        relative_string = relative_path.as_posix()
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            directories.add(relative_string)
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                _collect_staging_tree(child_fd, relative_path, directories, files)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            files.add(relative_string)
        else:
            raise ValueError(
                f"unexpected or unsafe archive staging entry: {relative_string}"
            )


def _verify_staging_against_manifest(staging_fd: int, manifest: dict[str, Any]) -> None:
    expected_files = {"manifest.json"}
    expected_directories: set[str] = set()
    included = manifest.get("included")
    if not isinstance(included, list):
        raise ValueError("archive manifest included entries must be a list")

    for entry in included:
        if not isinstance(entry, dict):
            raise ValueError("archive manifest included entry must be an object")
        relative_path = _safe_relative_path(entry.get("path", ""))
        relative_string = relative_path.as_posix()
        if relative_string == "manifest.json" or relative_string in expected_files:
            raise ValueError(f"duplicate archive manifest path: {relative_string}")
        expected_files.add(relative_string)
        for index in range(1, len(relative_path.parts)):
            expected_directories.add(
                PurePosixPath(*relative_path.parts[:index]).as_posix()
            )

    actual_directories: set[str] = set()
    actual_files: set[str] = set()
    _collect_staging_tree(
        staging_fd, PurePosixPath("."), actual_directories, actual_files
    )
    extra_directories = sorted(actual_directories - expected_directories)
    missing_directories = sorted(expected_directories - actual_directories)
    extra_files = sorted(actual_files - expected_files)
    missing_files = sorted(expected_files - actual_files)
    if extra_directories or missing_directories or extra_files or missing_files:
        raise ValueError(
            "archive staging tree differs from manifest: "
            f"extra_directories={extra_directories}, "
            f"missing_directories={missing_directories}, "
            f"extra_files={extra_files}, missing_files={missing_files}"
        )

    for entry in included:
        relative_path = _safe_relative_path(entry["path"])
        descriptor, opened_stat = _open_staging_regular_at(staging_fd, relative_path)
        try:
            verified_size, verified_digest = _hash_open_file(descriptor)
            final_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if _stable_file_identity(final_stat) != _stable_file_identity(opened_stat):
            raise ValueError(
                f"archive staging entry changed during verification: {relative_path}"
            )
        if verified_size != entry.get("size") or not hmac.compare_digest(
            verified_digest, str(entry.get("sha256", ""))
        ):
            raise ValueError(
                f"archive staging checksum or size mismatch: {relative_path}"
            )

    manifest_payload = _manifest_payload(manifest)
    manifest_fd, manifest_stat = _open_staging_regular_at(
        staging_fd, PurePosixPath("manifest.json")
    )
    try:
        manifest_size, manifest_digest = _hash_open_file(manifest_fd)
        final_manifest_stat = os.fstat(manifest_fd)
    finally:
        os.close(manifest_fd)
    if _stable_file_identity(final_manifest_stat) != _stable_file_identity(
        manifest_stat
    ):
        raise ValueError("archive staging manifest changed during verification")
    if manifest_size != len(manifest_payload) or not hmac.compare_digest(
        manifest_digest, hashlib.sha256(manifest_payload).hexdigest()
    ):
        raise ValueError("archive staging manifest checksum or size mismatch")


def _create_private_staging(archive_root_fd: int) -> tuple[str, int, os.stat_result]:
    for _ in range(128):
        name = f".artifact-hygiene-staging-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=archive_root_fd)
        except FileExistsError:
            continue
        created_stat: os.stat_result | None = None
        descriptor: int | None = None
        keep_staging = False
        try:
            created_stat = os.stat(name, dir_fd=archive_root_fd, follow_symlinks=False)
            descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=archive_root_fd)
            opened_stat = os.fstat(descriptor)
            current_stat = os.stat(name, dir_fd=archive_root_fd, follow_symlinks=False)
            if (
                stat.S_ISDIR(current_stat.st_mode)
                and current_stat.st_dev == created_stat.st_dev
                and current_stat.st_ino == created_stat.st_ino
                and opened_stat.st_dev == created_stat.st_dev
                and opened_stat.st_ino == created_stat.st_ino
            ):
                os.fsync(archive_root_fd)
                keep_staging = True
                return name, descriptor, opened_stat
            raise OSError("archive staging directory changed while being opened")
        finally:
            if not keep_staging:
                if descriptor is not None:
                    os.close(descriptor)
                if created_stat is None:
                    recovery_fd: int | None = None
                    try:
                        recovery_fd = os.open(
                            name, _DIRECTORY_OPEN_FLAGS, dir_fd=archive_root_fd
                        )
                        recovery_stat = os.fstat(recovery_fd)
                        current_stat = os.stat(
                            name, dir_fd=archive_root_fd, follow_symlinks=False
                        )
                        if (
                            stat.S_ISDIR(current_stat.st_mode)
                            and current_stat.st_dev == recovery_stat.st_dev
                            and current_stat.st_ino == recovery_stat.st_ino
                        ):
                            os.rmdir(name, dir_fd=archive_root_fd)
                            os.fsync(archive_root_fd)
                    finally:
                        if recovery_fd is not None:
                            os.close(recovery_fd)
                else:
                    try:
                        current_stat = os.stat(
                            name, dir_fd=archive_root_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        if (
                            stat.S_ISDIR(current_stat.st_mode)
                            and current_stat.st_dev == created_stat.st_dev
                            and current_stat.st_ino == created_stat.st_ino
                        ):
                            os.rmdir(name, dir_fd=archive_root_fd)
                            os.fsync(archive_root_fd)
    raise FileExistsError("could not allocate a unique archive staging directory")


def _clear_directory_fd(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                opened_stat = os.fstat(child_fd)
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            current_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                current_stat.st_dev != opened_stat.st_dev
                or current_stat.st_ino != opened_stat.st_ino
            ):
                raise OSError(f"archive staging child changed during cleanup: {name}")
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _cleanup_private_staging(
    archive_root_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_stat: os.stat_result,
) -> None:
    _clear_directory_fd(staging_fd)
    try:
        current_stat = os.stat(
            staging_name, dir_fd=archive_root_fd, follow_symlinks=False
        )
    except FileNotFoundError as exc:
        raise OSError(
            "archive staging path disappeared; refusing path-based cleanup"
        ) from exc
    if (
        not stat.S_ISDIR(current_stat.st_mode)
        or current_stat.st_dev != staging_stat.st_dev
        or current_stat.st_ino != staging_stat.st_ino
    ):
        raise OSError("archive staging path was replaced; refusing path-based cleanup")
    os.rmdir(staging_name, dir_fd=archive_root_fd)
    os.fsync(archive_root_fd)


def _rename_no_replace(
    directory_fd: int, source_name: str, destination_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOTSUP, "atomic no-replace archive publication is unavailable"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source_name),
        directory_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            f"archive destination already exists: {destination_name}",
            destination_name,
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise OSError(
            errno.ENOTSUP, "atomic no-replace archive publication is unavailable"
        )
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _publish_staging_no_replace(
    archive_root_fd: int, staging_name: str, destination_name: str
) -> None:
    try:
        os.stat(destination_name, dir_fd=archive_root_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(
            errno.EEXIST,
            f"archive destination already exists: {destination_name}",
            destination_name,
        )
    _rename_no_replace(archive_root_fd, staging_name, destination_name)


def _directory_entry_matches(
    directory_fd: int, name: str, expected_stat: os.stat_result
) -> bool:
    try:
        current_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current_stat.st_mode)
        and current_stat.st_dev == expected_stat.st_dev
        and current_stat.st_ino == expected_stat.st_ino
    )


def _revalidate_absolute_directory(
    path: Path, expected_stat: os.stat_result, *, purpose: str
) -> None:
    try:
        descriptor = _open_absolute_directory_no_follow(
            path, create=False, purpose=purpose
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{purpose} changed or contains a symlink before publication: {path}"
        ) from exc
    try:
        current_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        current_stat.st_dev != expected_stat.st_dev
        or current_stat.st_ino != expected_stat.st_ino
    ):
        raise ValueError(f"{purpose} changed before publication: {path}")


def create_archive(
    result: Path | str,
    archive_root: Path | str,
    *,
    execute: bool = False,
    git_sha: str | None = None,
    expected_plan_digest: str | None = None,
) -> dict[str, Any]:
    """Plan an archive, writing it only when ``execute`` is explicitly true."""

    plan = plan_archive(result, archive_root)
    if expected_plan_digest is not None and not hmac.compare_digest(
        expected_plan_digest, plan["plan_digest"]
    ):
        raise ValueError(
            "archive plan changed since review; run a new dry-run before execution"
        )
    if not execute:
        return plan

    source = Path(plan["source"])
    archive_root = Path(plan["destination"]).parent
    destination = Path(plan["destination"])
    if not source.name or destination.name != source.name:
        raise ValueError("archive source must have one safe destination name")
    if git_sha is None:
        resolved_git_sha, git_sha_error = _git_sha(source)
    else:
        resolved_git_sha = git_sha or "unknown"
        git_sha_error = None if git_sha else "explicit git SHA was empty"

    source_root_fd = _open_absolute_directory_no_follow(
        source, create=False, purpose="archive source"
    )
    source_root_stat = os.fstat(source_root_fd)
    archive_root_fd: int | None = None
    staging_fd: int | None = None
    staging_name: str | None = None
    staging_stat: os.stat_result | None = None
    published = False
    try:
        archive_root_fd = _open_absolute_directory_no_follow(
            archive_root, create=True, purpose="archive root"
        )
        archive_root_stat = os.fstat(archive_root_fd)
        staging_name, staging_fd, staging_stat = _create_private_staging(
            archive_root_fd
        )

        manifest_included = [
            _copy_archive_entry(source_root_fd, staging_fd, entry)
            for entry in plan["included"]
        ]
        manifest = {
            "schema_version": "1",
            "source": str(source),
            "destination": str(destination),
            "git_sha": resolved_git_sha,
            "git_sha_error": git_sha_error,
            "archived_at": _utc_timestamp(),
            "plan_digest": plan["plan_digest"],
            "included": manifest_included,
            "excluded": plan["excluded"],
        }
        _write_manifest_at(staging_fd, manifest)
        _verify_staging_against_manifest(staging_fd, manifest)
        _revalidate_absolute_directory(
            source, source_root_stat, purpose="archive source"
        )
        _revalidate_absolute_directory(
            archive_root, archive_root_stat, purpose="archive root"
        )
        try:
            _publish_staging_no_replace(archive_root_fd, staging_name, destination.name)
        finally:
            published = _directory_entry_matches(
                archive_root_fd, destination.name, staging_stat
            )
        if not published:
            raise OSError("archive staging publication did not commit")
        try:
            os.fsync(archive_root_fd)
        except OSError as exc:
            raise OSError(
                exc.errno or errno.EIO,
                "archive was published but archive-root fsync failed; "
                "the final archive exists with uncertain crash durability",
            ) from exc
        return manifest
    finally:
        try:
            if (
                not published
                and archive_root_fd is not None
                and staging_fd is not None
                and staging_name is not None
                and staging_stat is not None
            ):
                _cleanup_private_staging(
                    archive_root_fd, staging_name, staging_fd, staging_stat
                )
        finally:
            if staging_fd is not None:
                os.close(staging_fd)
            if archive_root_fd is not None:
                os.close(archive_root_fd)
            os.close(source_root_fd)
