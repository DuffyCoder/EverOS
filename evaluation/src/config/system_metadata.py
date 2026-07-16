"""Redacted, resume-safe metadata for resolved evaluation system configs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from evaluation.src.config.system_loader import ResolvedSystemConfig, deep_merge_config
from evaluation.src.config.system_policy import _is_secret_key, _normalize_policy_key

METADATA_FILENAME = "resolved-system-config.json"
METADATA_SCHEMA = "evaluation-system-config/v1"

_ENV_MARKER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")
_FULL_ENV_MARKER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PRIVATE_KEY_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_NETWORK_URL_SCHEMES = frozenset(
    {
        "ftp",
        "ftps",
        "http",
        "https",
        "mongodb",
        "mongodb+srv",
        "mysql",
        "postgres",
        "postgresql",
        "redis",
        "rediss",
        "ws",
        "wss",
    }
)
_SECRET_ENVIRONMENT_NAMES = frozenset({"apikey", "jwt", "pass", "passwd", "pat"})
_SECRET_ENVIRONMENT_SUFFIXES = (
    "_apikey",
    "_jwt",
    "_key_pem",
    "_pass",
    "_passwd",
    "_pat",
    "_token",
)
_CHECKPOINT_FILE = re.compile(r"^checkpoint_.+\.json$")
_RESPONSE_CHECKPOINT_FILE = re.compile(r"^responses_checkpoint_\d+\.json$")
_MEMCELL_FILE = re.compile(r"^memcell_list_conv_.+\.json$")
_ROOT_MAPPING_ARTIFACTS = frozenset(
    {"search_results_checkpoint.json", "eval_results.json"}
)
_ROOT_LIST_ARTIFACTS = frozenset({"search_results.json", "answer_results.json"})
_TEMPORARY_METADATA_FILE = re.compile(
    rf"^\.{re.escape(METADATA_FILENAME)}\.[A-Za-z0-9_-]{{6,64}}\.tmp$"
)
_MISSING = object()
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class MetadataWriteResult:
    """Outcome of a metadata write-or-verify operation."""

    created: bool
    adopted_legacy: bool
    warning: str | None = None


def write_resolved_system_metadata(
    resolution: ResolvedSystemConfig,
    final_runtime_config: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    output_path: Path,
    dataset_id: str,
    *,
    systems_root: Path,
    adopt_legacy: bool = False,
) -> MetadataWriteResult:
    """Atomically write or verify redacted system provenance.

    The output directory itself is locked across the complete
    check/compare/install critical section so two cooperating CLI processes
    cannot race into last-writer-wins provenance.
    """
    context = _validate_runtime_context(runtime_context, dataset_id)
    relative_sources = _relative_source_paths(resolution.source_paths, systems_root)
    payload = _build_payload(
        resolution,
        final_runtime_config,
        context,
        dataset_id=dataset_id,
        source_paths=relative_sources,
    )
    serialized = _pretty_json(payload)

    output_dir = Path(output_path)
    _prepare_output_directory(output_dir)
    try:
        directory_fd = os.open(
            output_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
        )
    except OSError:
        _exit_two("result directory could not be opened safely")
    locked = False
    try:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
        except OSError:
            _exit_two("result directory could not be locked")
        locked = True
        _assert_output_directory_identity(output_dir, directory_fd)
        entries = _list_directory_entries(directory_fd)
        _remove_stale_temporary_files(directory_fd, entries)

        existing = _load_existing_metadata(directory_fd)
        if existing is not None:
            adopted = existing.get("provenance_status") == ("adopted-legacy-unverified")
            expected = deepcopy(payload)
            if adopted:
                expected["provenance_status"] = "adopted-legacy-unverified"
            if _canonical_json(existing) != _canonical_json(expected):
                _exit_two(
                    "result directory metadata does not match the requested run; "
                    "choose a new output directory"
                )
            _assert_output_directory_identity(output_dir, directory_fd)
            return MetadataWriteResult(created=False, adopted_legacy=adopted)

        entries = _list_directory_entries(directory_fd)
        adopted = False
        warning: str | None = None
        if entries:
            if not adopt_legacy:
                _exit_two(
                    "result directory is non-empty but has no "
                    f"{METADATA_FILENAME}; pass --adopt-legacy-result-dir only "
                    "for a recognized legacy checkpoint"
                )
            if not _has_recognized_legacy_artifact(directory_fd, entries):
                _exit_two(
                    "legacy result-directory adoption requires a recognized "
                    "checkpoint or progress artifact"
                )
            adopted = True
            warning = (
                "UNVERIFIED legacy result directory adopted: its historical "
                "checkpoint configuration cannot be verified"
            )
            payload["provenance_status"] = "adopted-legacy-unverified"
            serialized = _pretty_json(payload)

        _assert_output_directory_identity(output_dir, directory_fd)
        _atomic_install(directory_fd, serialized)
        _assert_output_directory_identity(output_dir, directory_fd)
        return MetadataWriteResult(
            created=True, adopted_legacy=adopted, warning=warning
        )
    finally:
        if locked:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _build_payload(
    resolution: ResolvedSystemConfig,
    final_runtime_config: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    *,
    dataset_id: str,
    source_paths: list[str],
) -> dict[str, Any]:
    if not isinstance(final_runtime_config, Mapping):
        _exit_two("final runtime system configuration must be a mapping")

    selected_raw = _selected_raw_config(resolution.raw_config, dataset_id)
    references: set[str] = set()
    redacted_runtime = _redact_value(
        final_runtime_config, selected_raw, references=references
    )
    redacted_raw = _redact_value(
        resolution.raw_config, resolution.raw_config, references=set()
    )
    if not isinstance(redacted_runtime, dict) or not isinstance(redacted_raw, dict):
        _exit_two("resolved system configuration must be a mapping")

    return {
        "schema": METADATA_SCHEMA,
        "provenance_status": "verified",
        "dataset_id": dataset_id,
        "requested_id": resolution.requested_id,
        "canonical_id": resolution.canonical_id,
        "adapter": resolution.adapter,
        "category": resolution.category,
        "status": resolution.status,
        "alias_chain": list(resolution.alias_chain),
        "source_paths": source_paths,
        "raw_config_sha256": _sha256_json(redacted_raw),
        "runtime_config_sha256": _sha256_json(
            {"config": redacted_runtime, "runtime_context": runtime_context}
        ),
        "environment_references": sorted(references),
        "runtime_context": deepcopy(dict(runtime_context)),
        "config": redacted_runtime,
    }


def _selected_raw_config(
    raw_config: Mapping[str, Any], dataset_id: str
) -> dict[str, Any]:
    selected = deepcopy(dict(raw_config))
    overrides = selected.pop("dataset_overrides", None)
    if isinstance(overrides, Mapping):
        patch = overrides.get(dataset_id)
        if isinstance(patch, Mapping):
            selected = deep_merge_config(selected, patch)
    selected.pop("dataset_overrides", None)
    return selected


def _redact_value(
    runtime_value: Any,
    raw_value: Any,
    *,
    references: set[str],
    force_secret: bool = False,
) -> Any:
    _collect_marker_references(raw_value, references)

    if isinstance(runtime_value, Mapping):
        raw_mapping = raw_value if isinstance(raw_value, Mapping) else {}
        redacted: dict[str, Any] = {}
        for key, nested in runtime_value.items():
            if not isinstance(key, str):
                _exit_two("system metadata only supports string mapping keys")
            normalized = _normalize_policy_key(key)
            raw_nested = raw_mapping.get(key, _MISSING)

            if normalized == "env_vars":
                redacted[key] = deepcopy(nested)
                _collect_bare_env_references(nested, references)
                continue
            if normalized.endswith("_env"):
                redacted[key] = deepcopy(nested)
                _collect_bare_env_references(nested, references)
                continue

            nested_force_secret = force_secret
            if not force_secret and _is_secret_key(normalized):
                nested_force_secret = True
            redacted[key] = _redact_value(
                nested,
                raw_nested,
                references=references,
                force_secret=nested_force_secret,
            )
        return redacted

    if isinstance(runtime_value, list):
        raw_items = raw_value if isinstance(raw_value, list) else []
        return [
            _redact_value(
                nested,
                raw_items[index] if index < len(raw_items) else _MISSING,
                references=references,
                force_secret=force_secret,
            )
            for index, nested in enumerate(runtime_value)
        ]

    if force_secret:
        if isinstance(raw_value, str) and _FULL_ENV_MARKER.fullmatch(raw_value):
            return _sanitize_environment_markers(raw_value, force_redact_defaults=True)
        if raw_value == "" and runtime_value == "":
            return ""
        return "<redacted>"

    runtime_is_raw_template = (
        isinstance(runtime_value, str)
        and isinstance(raw_value, str)
        and runtime_value == raw_value
        and _ENV_MARKER.search(raw_value) is not None
    )
    if runtime_is_raw_template:
        sanitized_template = _sanitize_environment_markers(raw_value)
        if _url_contains_userinfo(sanitized_template):
            _exit_two("runtime configuration URL contains embedded credentials")
        if _PRIVATE_KEY_PEM.search(sanitized_template):
            return "<redacted>"
        return sanitized_template

    if isinstance(runtime_value, str) and _url_contains_userinfo(runtime_value):
        _exit_two("runtime configuration URL contains embedded credentials")
    if isinstance(runtime_value, str) and _PRIVATE_KEY_PEM.search(runtime_value):
        if isinstance(raw_value, str) and _ENV_MARKER.search(raw_value):
            return _sanitize_environment_markers(raw_value, force_redact_defaults=True)
        return "<redacted>"
    if isinstance(raw_value, str) and _contains_sensitive_environment_marker(raw_value):
        return _sanitize_environment_markers(raw_value)
    if isinstance(runtime_value, str):
        return _sanitize_environment_markers(runtime_value)
    return deepcopy(runtime_value)


def _collect_marker_references(value: Any, references: set[str]) -> None:
    if not isinstance(value, str):
        return
    references.update(match.group(1) for match in _ENV_MARKER.finditer(value))


def _sanitize_environment_markers(
    value: str, *, force_redact_defaults: bool = False
) -> str:
    def replace_marker(match: re.Match[str]) -> str:
        default = match.group(2)
        if default is None or default == "":
            return match.group(0)
        if force_redact_defaults or _environment_marker_is_sensitive(match):
            return f"${{{match.group(1)}:<redacted-default>}}"
        return match.group(0)

    return _ENV_MARKER.sub(replace_marker, value)


def _contains_sensitive_environment_marker(value: str) -> bool:
    return any(
        _environment_marker_is_sensitive(match) for match in _ENV_MARKER.finditer(value)
    )


def _environment_marker_is_sensitive(match: re.Match[str]) -> bool:
    if _environment_name_is_sensitive(match.group(1)):
        return True
    default = match.group(2)
    return default is not None and _environment_default_is_sensitive(default)


def _environment_name_is_sensitive(name: str) -> bool:
    normalized = _normalize_policy_key(name)
    return (
        _is_secret_key(normalized)
        or normalized in _SECRET_ENVIRONMENT_NAMES
        or normalized.endswith(_SECRET_ENVIRONMENT_SUFFIXES)
    )


def _collect_bare_env_references(value: Any, references: set[str]) -> None:
    if isinstance(value, str):
        if _ENV_NAME.fullmatch(value):
            references.add(value)
        return
    if isinstance(value, list):
        for nested in value:
            if isinstance(nested, str) and _ENV_NAME.fullmatch(nested):
                references.add(nested)


def _url_contains_userinfo(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        if _looks_like_network_url(value):
            _exit_two("runtime configuration contains a malformed URL")
        return False
    if bool(parsed.netloc) and (
        parsed.username is not None or parsed.password is not None
    ):
        return True
    if _malformed_network_url_contains_userinfo(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=parsed.path,
        scheme_relative=value.startswith("//"),
    ):
        _exit_two("runtime configuration contains a malformed URL")
    return _query_contains_secret(parsed.query) or _fragment_contains_secret(
        parsed.fragment
    )


def _environment_default_is_sensitive(default: str) -> bool:
    if _PRIVATE_KEY_PEM.search(default):
        return True
    try:
        parsed = urlsplit(default)
    except ValueError:
        return _looks_like_network_url(default)
    if bool(parsed.netloc) and (
        parsed.username is not None or parsed.password is not None
    ):
        return True
    if _malformed_network_url_contains_userinfo(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=parsed.path,
        scheme_relative=default.startswith("//"),
    ):
        return True
    return _query_contains_secret(parsed.query) or _fragment_contains_secret(
        parsed.fragment
    )


def _looks_like_network_url(value: str) -> bool:
    if value.startswith("//") or "://" in value:
        return True
    scheme, separator, remainder = value.partition(":")
    return (
        bool(separator) and scheme.lower() in _NETWORK_URL_SCHEMES and "@" in remainder
    )


def _malformed_network_url_contains_userinfo(
    *, scheme: str, netloc: str, path: str, scheme_relative: bool
) -> bool:
    return (
        not netloc
        and (scheme.lower() in _NETWORK_URL_SCHEMES or scheme_relative)
        and "@" in path
    )


def _query_contains_secret(value: str) -> bool:
    for key, nested in parse_qsl(value, keep_blank_values=True):
        if nested and _environment_name_is_sensitive(key):
            return True
    return False


def _fragment_contains_secret(value: str) -> bool:
    if _query_contains_secret(value):
        return True
    return any(_query_contains_secret(query) for query in value.split("?")[1:])


def _validate_runtime_context(
    runtime_context: Mapping[str, Any], dataset_id: str
) -> dict[str, Any]:
    if not isinstance(runtime_context, Mapping):
        _exit_two("runtime context must be a mapping")
    context = dict(runtime_context)
    if set(context) != {"dataset_name", "clean_groups"}:
        _exit_two("runtime context must contain exactly dataset_name and clean_groups")
    if context["dataset_name"] != dataset_id:
        _exit_two("runtime context dataset_name must match dataset_id")
    if type(context["clean_groups"]) is not bool:
        _exit_two("runtime context clean_groups must be a boolean")
    return deepcopy(context)


def _relative_source_paths(
    source_paths: tuple[Path, ...], systems_root: Path
) -> list[str]:
    try:
        root = Path(systems_root).resolve(strict=True)
    except OSError:
        _exit_two("systems root is missing or inaccessible")
    if not root.is_dir():
        _exit_two("systems root must be a directory")

    relative_paths: list[str] = []
    for source_path in source_paths:
        supplied = Path(source_path)
        if supplied.is_symlink():
            _exit_two("system configuration source paths may not be symlinks")
        try:
            source = supplied.resolve(strict=True)
            relative = source.relative_to(root)
        except (OSError, ValueError):
            _exit_two("system configuration source path escapes the systems root")
        if not source.is_file():
            _exit_two("system configuration source path must be a regular file")
        relative_paths.append(relative.as_posix())
    return relative_paths


def _prepare_output_directory(output_dir: Path) -> None:
    if output_dir.is_symlink():
        _exit_two("result directory may not be a symlink")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        _exit_two("result directory could not be created")
    if not output_dir.is_dir():
        _exit_two("result output path must be a directory")


def _assert_output_directory_identity(output_dir: Path, directory_fd: int) -> None:
    try:
        path_stat = os.stat(output_dir, follow_symlinks=False)
        descriptor_stat = os.fstat(directory_fd)
    except OSError:
        _exit_two("result directory identity could not be verified")
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_dev != descriptor_stat.st_dev
        or path_stat.st_ino != descriptor_stat.st_ino
    ):
        _exit_two("result directory changed while metadata was being written")


def _list_directory_entries(directory_fd: int) -> list[str]:
    try:
        return sorted(os.listdir(directory_fd))
    except OSError:
        _exit_two("result directory could not be enumerated safely")


def _remove_stale_temporary_files(directory_fd: int, entries: list[str]) -> None:
    removed = False
    for name in entries:
        if _TEMPORARY_METADATA_FILE.fullmatch(name) is None:
            continue
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            _exit_two("stale system metadata temporary file could not be inspected")
        if not stat.S_ISREG(entry_stat.st_mode):
            continue
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except OSError:
            _exit_two("stale system metadata temporary file could not be removed")
        removed = True
    if removed:
        _fsync_directory(
            directory_fd, "stale system metadata cleanup could not be synchronized"
        )


def _load_existing_metadata(directory_fd: int) -> dict[str, Any] | None:
    try:
        metadata_stat = os.stat(
            METADATA_FILENAME, dir_fd=directory_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return None
    except OSError:
        _exit_two("existing system metadata could not be inspected")

    if not stat.S_ISREG(metadata_stat.st_mode):
        _exit_two("existing system metadata must be a regular non-symlink file")

    try:
        descriptor = os.open(
            METADATA_FILENAME, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=directory_fd
        )
    except OSError:
        _exit_two("existing system metadata could not be opened safely")
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            _exit_two("existing system metadata must be a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as metadata_file:
            descriptor = -1
            text = metadata_file.read()
    except (OSError, UnicodeError):
        _exit_two("existing system metadata is unreadable")
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        _exit_two("existing system metadata is not valid strict JSON")
    if not isinstance(parsed, dict):
        _exit_two("existing system metadata must contain a JSON object")
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _has_recognized_legacy_artifact(directory_fd: int, entries: list[str]) -> bool:
    for name in entries:
        if (
            _CHECKPOINT_FILE.fullmatch(name)
            or _RESPONSE_CHECKPOINT_FILE.fullmatch(name)
            or name in _ROOT_MAPPING_ARTIFACTS
            or name in _ROOT_LIST_ARTIFACTS
        ):
            payload = _read_legacy_json(directory_fd, name)
            if payload is _MISSING:
                continue
            if _CHECKPOINT_FILE.fullmatch(name):
                if isinstance(payload, dict) and isinstance(
                    payload.get("completed_stages"), list
                ):
                    return True
            elif name in _ROOT_LIST_ARTIFACTS:
                if isinstance(payload, list) and payload:
                    return True
            elif isinstance(payload, dict):
                return True

    try:
        memcells_fd = os.open(
            "memcells",
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            dir_fd=directory_fd,
        )
    except OSError:
        return False
    try:
        try:
            memcell_entries = sorted(os.listdir(memcells_fd))
        except OSError:
            return False
        for name in memcell_entries:
            if not _MEMCELL_FILE.fullmatch(name):
                continue
            payload = _read_legacy_json(memcells_fd, name)
            if isinstance(payload, list) and payload:
                return True
        return False
    finally:
        try:
            os.close(memcells_fd)
        except OSError:
            pass


def _read_legacy_json(directory_fd: int, name: str) -> Any:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=directory_fd
        )
    except OSError:
        return _MISSING
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return _MISSING
        with os.fdopen(descriptor, "r", encoding="utf-8") as artifact_file:
            descriptor = -1
            return json.load(
                artifact_file,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
    except (OSError, UnicodeError, TypeError, ValueError):
        return _MISSING
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _atomic_install(directory_fd: int, serialized: str) -> None:
    try:
        encoded = serialized.encode("utf-8")
    except UnicodeError:
        _exit_two("system metadata could not be encoded as UTF-8")
    descriptor, temporary_name = _create_temporary_metadata_file(directory_fd)
    published = False
    try:
        offset = 0
        try:
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("short write while creating system metadata")
                offset += written
            os.fsync(descriptor)
        except OSError:
            _exit_two("temporary system metadata could not be written durably")
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

        # Publish with atomic no-replace semantics. The temporary file is in
        # this same directory, so a hard link is a single-filesystem atomic
        # install and cannot overwrite provenance from a racing writer.
        try:
            os.link(
                temporary_name,
                METADATA_FILENAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _exit_two("system metadata appeared concurrently; retry to verify it")
        except OSError:
            _exit_two("system metadata could not be published atomically")
        published = True
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_failed = False
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_failed = True
        sync_failed = False
        try:
            os.fsync(directory_fd)
        except OSError:
            sync_failed = True
        if not active_error and cleanup_failed:
            if published:
                _exit_two(
                    "system metadata was published but temporary-file cleanup "
                    "could not be confirmed; retry to verify it"
                )
            _exit_two("temporary system metadata could not be removed")
        if not active_error and sync_failed:
            if published:
                _exit_two(
                    "system metadata was published but directory durability "
                    "could not be confirmed; retry to verify it"
                )
            _exit_two("temporary system metadata cleanup could not be synchronized")


def _create_temporary_metadata_file(directory_fd: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
    for _ in range(32):
        name = f".{METADATA_FILENAME}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        except OSError:
            _exit_two("temporary system metadata file could not be created")
        return descriptor, name
    _exit_two("a unique temporary system metadata file could not be created")


def _fsync_directory(directory_fd: int, message: str) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        _exit_two(message)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        _exit_two("system metadata contains a non-JSON-compatible value")


def _pretty_json(value: Any) -> str:
    try:
        return (
            json.dumps(
                value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n"
        )
    except (TypeError, ValueError):
        _exit_two("system metadata contains a non-JSON-compatible value")


def _exit_two(message: str) -> Any:
    print(f"evaluation: error: {message}", file=sys.stderr)
    raise SystemExit(2)
