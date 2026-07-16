"""Resolve public evaluation systems into runnable configurations."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from evaluation.src.config.system_index import (
    DEFAULT_SYSTEM_INDEX_PATH,
    SystemIndexEntry,
    SystemIndexError,
    get_system_entry,
    load_system_index,
    resolve_alias,
)
from evaluation.src.config.system_policy import (
    PolicyFinding,
    SystemPolicyError,
    validate_raw_system_policy,
)
from evaluation.src.config.system_schema import (
    SystemSchemaError,
    validate_system_config,
)
from evaluation.src.config.yaml_loader import strict_safe_load

_ENV_MARKER = re.compile(r"\$\{([^:}]+)(?::([^}]*))?\}")


class SystemConfigError(SystemIndexError):
    """Raised when a registered system configuration cannot be resolved."""


@dataclass(frozen=True)
class ResolvedSystemConfig:
    requested_id: str
    canonical_id: str
    category: str
    status: str
    adapter: str
    config: dict[str, Any]
    raw_config: dict[str, Any]
    source_paths: tuple[Path, ...]
    alias_chain: tuple[str, ...]
    policy_findings: tuple[PolicyFinding, ...]
    warning: str | None


def deep_merge_config(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    """Recursively merge mappings while replacing every non-mapping value."""
    merged = {key: deepcopy(value) for key, value in base.items()}
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge_config(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def resolve_system_config(
    system_id: str,
    *,
    index_path: Path = DEFAULT_SYSTEM_INDEX_PATH,
    systems_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    allow_legacy: bool = False,
) -> ResolvedSystemConfig:
    """Resolve aliases, inheritance, and environment markers for a public id."""
    resolved_index_path = Path(index_path)
    index = load_system_index(resolved_index_path)
    requested_entry = get_system_entry(index, system_id)
    canonical_id, alias_chain = resolve_alias(index, system_id)
    canonical_entry = get_system_entry(index, canonical_id)
    if canonical_entry.path is None:
        raise SystemConfigError(
            f"resolved system {canonical_id!r} does not define a configuration path"
        )

    root = _resolve_systems_root(
        Path(systems_root) if systems_root is not None else resolved_index_path.parent
    )
    alias_paths = frozenset(
        PurePosixPath(f"{entry.system_id}.yaml")
        for entry in index.systems.values()
        if entry.category == "alias"
    )
    raw_config, source_paths = _load_inheritance_chain(
        canonical_entry.path, systems_root=root, alias_paths=alias_paths, stack=()
    )

    adapter = canonical_entry.adapter
    try:
        policy_findings = validate_raw_system_policy(
            adapter, raw_config, canonical_id=canonical_id, allow_legacy=allow_legacy
        )
    except SystemPolicyError as exc:
        raise SystemConfigError(
            f"resolved config for system {canonical_id!r} violates raw policy: {exc}"
        ) from exc

    environment = os.environ if environ is None else environ
    config = _replace_env_markers(raw_config, environment)
    try:
        validate_system_config(adapter, config)
    except SystemSchemaError as exc:
        raise SystemConfigError(
            f"resolved config for system {canonical_id!r} violates schema: {exc}"
        ) from exc
    return ResolvedSystemConfig(
        requested_id=system_id,
        canonical_id=canonical_id,
        category=requested_entry.category,
        status=requested_entry.status,
        adapter=adapter,
        config=config,
        raw_config=raw_config,
        source_paths=source_paths,
        alias_chain=alias_chain,
        policy_findings=policy_findings,
        warning=_deprecation_warning(system_id, alias_chain, index.systems),
    )


def _resolve_systems_root(systems_root: Path) -> Path:
    try:
        resolved = systems_root.resolve(strict=True)
    except OSError as exc:
        raise SystemConfigError(
            f"systems root not found or inaccessible: {systems_root}"
        ) from exc
    if not resolved.is_dir():
        raise SystemConfigError(f"systems root is not a directory: {systems_root}")
    return resolved


def _load_inheritance_chain(
    relative_path: PurePosixPath | str,
    *,
    systems_root: Path,
    alias_paths: frozenset[PurePosixPath],
    stack: tuple[tuple[Path, str], ...],
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    posix_path = _relative_config_path(relative_path, "config path")
    config_path = _secure_config_path(systems_root, posix_path)
    display_path = posix_path.as_posix()

    positions = {path: position for position, (path, _) in enumerate(stack)}
    if config_path in positions:
        cycle_start = positions[config_path]
        cycle = [display for _, display in stack[cycle_start:]] + [display_path]
        raise SystemConfigError(f"inheritance cycle detected: {' -> '.join(cycle)}")

    document = _load_config_mapping(config_path, display_path)
    if "extends" not in document:
        return deepcopy(document), (config_path,)

    parent_path = _relative_config_path(document["extends"], f"{display_path} extends")
    if parent_path in alias_paths:
        alias_id = parent_path.stem
        raise SystemConfigError(
            f"{display_path} extends alias configuration "
            f"{parent_path.as_posix()!r} for public id {alias_id!r}; aliases "
            "cannot be inheritance targets"
        )

    child = {key: value for key, value in document.items() if key != "extends"}
    parent, parent_sources = _load_inheritance_chain(
        parent_path,
        systems_root=systems_root,
        alias_paths=alias_paths,
        stack=stack + ((config_path, display_path),),
    )
    return deep_merge_config(parent, child), parent_sources + (config_path,)


def _relative_config_path(value: object, context: str) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise SystemConfigError(f"{context} must be a non-empty POSIX string")
    raw_path = str(value)
    if not raw_path.strip():
        raise SystemConfigError(f"{context} must be a non-empty POSIX string")
    if "\\" in raw_path:
        raise SystemConfigError(f"{context} must use a POSIX path, got {raw_path!r}")

    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise SystemConfigError(f"{context} must be relative, got {raw_path!r}")
    if path == PurePosixPath(".") or ".." in path.parts:
        raise SystemConfigError(
            f"{context} escapes the systems root or names no file: {raw_path!r}"
        )
    if path.parts and path.parts[0].endswith(":"):
        raise SystemConfigError(f"{context} must be relative, got {raw_path!r}")
    return path


def _secure_config_path(systems_root: Path, relative_path: PurePosixPath) -> Path:
    candidate = systems_root.joinpath(*relative_path.parts)
    component = systems_root
    logical_parts: list[str] = []
    for part in relative_path.parts:
        logical_parts.append(part)
        component = component / part
        if component.is_symlink():
            logical_component = PurePosixPath(*logical_parts).as_posix()
            raise SystemConfigError(
                f"config path {relative_path.as_posix()!r} traverses symlink "
                f"component {logical_component!r}; symlinked configs and "
                "directories are not allowed"
            )

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise SystemConfigError(
            f"could not resolve config path {relative_path.as_posix()!r}: {exc}"
        ) from exc
    try:
        resolved.relative_to(systems_root)
    except ValueError:
        raise SystemConfigError(
            f"config path {relative_path.as_posix()!r} escapes or resolves "
            f"outside systems root {systems_root}"
        ) from None

    if not candidate.exists():
        raise SystemConfigError(
            f"config path {relative_path.as_posix()!r} not found under "
            f"systems root {systems_root}"
        )
    if not candidate.is_file():
        raise SystemConfigError(
            f"config path {relative_path.as_posix()!r} is not a regular file"
        )
    return resolved


def _load_config_mapping(config_path: Path, display_path: str) -> dict[str, Any]:
    try:
        text = config_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise SystemConfigError(
            f"config {display_path!r} must be valid UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise SystemConfigError(
            f"could not read config {display_path!r}: {exc}"
        ) from exc

    try:
        document = strict_safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemConfigError(
            f"config {display_path!r} contains invalid YAML: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise SystemConfigError(
            f"config {display_path!r} must contain a mapping, "
            f"got {type(document).__name__}"
        )
    return document


def _replace_env_markers(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_env_markers(nested_value, environ)
            for key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_replace_env_markers(item, environ) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        default = match.group(2) or ""
        return environ[variable] if variable in environ else default

    return _ENV_MARKER.sub(replace, value)


def _deprecation_warning(
    requested_id: str,
    alias_chain: tuple[str, ...],
    entries: Mapping[str, SystemIndexEntry],
) -> str | None:
    messages: list[str] = []
    for resolved_id in alias_chain:
        entry = entries[resolved_id]
        if entry.status != "deprecated":
            continue
        if resolved_id == requested_id:
            messages.append(
                f"requested system {requested_id!r} is deprecated; replacement: "
                f"{entry.replacement!r}"
            )
        elif entry.category == "alias":
            messages.append(
                f"requested system {requested_id!r} resolves through deprecated "
                f"alias {resolved_id!r}; replacement: {entry.replacement!r}"
            )
        else:
            messages.append(
                f"requested system {requested_id!r} resolves to deprecated system "
                f"{resolved_id!r}; replacement: {entry.replacement!r}"
            )
    return " ".join(messages) or None
