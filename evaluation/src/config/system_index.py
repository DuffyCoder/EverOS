"""Strict loader for the public evaluation system index."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from evaluation.src.adapters.registry import list_adapters

SystemCategory = Literal["canonical", "alias", "experiment", "ablation", "tooling"]
SystemStatus = Literal["active", "compatibility", "experimental", "deprecated"]

VALID_CATEGORIES = frozenset(
    {"canonical", "alias", "experiment", "ablation", "tooling"}
)
VALID_STATUSES = frozenset({"active", "compatibility", "experimental", "deprecated"})
ALLOWED_STATUSES_BY_CATEGORY = {
    "canonical": frozenset({"active", "deprecated"}),
    "alias": frozenset({"compatibility", "deprecated"}),
    "experiment": frozenset({"experimental", "deprecated"}),
    "ablation": frozenset({"experimental", "deprecated"}),
    "tooling": frozenset({"experimental", "deprecated"}),
}
EXPECTED_PUBLIC_SYSTEM_IDS = frozenset(
    {
        "evermemos",
        "evermemos_cloud_api",
        "evermemos_local_api",
        "hermes",
        "hermes-hindsight",
        "hermes-holographic",
        "hermes-honcho",
        "mem0",
        "memos",
        "memu",
        "openclaw",
        "openclaw-agent-local",
        "openclaw-docker",
        "openclaw-docker-evermemos",
        "openclaw-docker-hypercompositor",
        "openclaw-docker-mem0",
        "openclaw-docker-memclaw",
        "openclaw-docker-memcore-session-bundle",
        "openclaw-docker-memcore-session-bundle-emptytail",
        "openclaw-docker-memcore-session-bundle-weaktail",
        "openclaw-docker-openviking-session-bundle-memcore",
        "openclaw-docker-openviking-session-bundle-noop",
        "openclaw-docker-openviking-session-bundle-noop-fixpack",
        "openclaw-docker-openviking-session-bundle-noop-serial",
        "openclaw-docker-stub",
        "openclaw-fts",
        "openclaw-fts-noflush",
        "openclaw-hybrid",
        "openclaw-hybrid-noflush",
        "openclaw-hypercompositor",
        "openclaw-native-embed",
        "openclaw-native-noembed",
        "openclaw-noop",
        "openclaw-vector",
        "openclaw-vector-noflush",
        "zep",
    }
)

DEFAULT_SYSTEM_INDEX_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "systems" / "index.yaml"
)

_SYSTEM_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")
_TOP_LEVEL_KEYS = frozenset({"schema_version", "systems"})
_ENTRY_KEYS = frozenset(
    {
        "adapter",
        "category",
        "status",
        "path",
        "alias_of",
        "replacement",
        "description",
        "docs",
    }
)
_REQUIRED_ENTRY_KEYS = frozenset({"adapter", "category", "status", "description"})
_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"


class SystemIndexError(ValueError):
    """Raised when the system index is malformed or a lookup fails."""


@dataclass(frozen=True)
class SystemIndexEntry:
    system_id: str
    adapter: str
    category: SystemCategory
    status: SystemStatus
    path: PurePosixPath | None
    alias_of: str | None
    replacement: str | None
    description: str
    docs: PurePosixPath | None


@dataclass(frozen=True)
class SystemIndex:
    schema_version: int
    systems: Mapping[str, SystemIndexEntry]


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys at every mapping depth."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    for key_node, _ in node.value:
        if key_node.tag == _YAML_MERGE_TAG:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "YAML merge key '<<' is not allowed in the system index",
                key_node.start_mark,
            )

    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found unhashable key {key!r}",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_system_index(path: Path = DEFAULT_SYSTEM_INDEX_PATH) -> SystemIndex:
    """Load and validate the complete public system registry."""
    index_path = Path(path)
    if not index_path.is_file():
        raise SystemIndexError(f"system index not found: {index_path}")

    try:
        text = index_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise SystemIndexError(
            f"system index {index_path} must be valid UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise SystemIndexError(
            f"could not read system index {index_path}: {exc}"
        ) from exc

    try:
        raw = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise SystemIndexError(f"{index_path} contains invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SystemIndexError(
            f"{index_path} must contain a top-level mapping, "
            f"got {type(raw).__name__}"
        )
    _require_string_keys(raw, f"{index_path} top level")

    unknown_top_level_keys = set(raw) - _TOP_LEVEL_KEYS
    if unknown_top_level_keys:
        raise SystemIndexError(
            f"{index_path} has unknown top-level key(s): "
            f"{sorted(unknown_top_level_keys)}"
        )

    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise SystemIndexError(
            f"{index_path} schema_version must be integer 1, " f"got {schema_version!r}"
        )

    systems_raw = raw.get("systems")
    if not isinstance(systems_raw, dict):
        raise SystemIndexError(
            f"{index_path} field 'systems' must be a mapping, "
            f"got {type(systems_raw).__name__}"
        )
    _require_string_keys(systems_raw, f"{index_path} systems")

    for system_id in systems_raw:
        _validate_system_id(system_id, f"{index_path} invalid system id")

    actual_system_ids = set(systems_raw)
    if actual_system_ids != EXPECTED_PUBLIC_SYSTEM_IDS:
        missing = sorted(EXPECTED_PUBLIC_SYSTEM_IDS - actual_system_ids)
        unexpected = sorted(actual_system_ids - EXPECTED_PUBLIC_SYSTEM_IDS)
        raise SystemIndexError(
            f"{index_path} public system id set must match the locked legacy "
            f"surface; missing: {missing}; unexpected: {unexpected}"
        )

    registered_adapters = frozenset(list_adapters())
    entries: dict[str, SystemIndexEntry] = {}
    for system_id, body in systems_raw.items():
        entries[system_id] = _parse_entry(
            system_id, body, index_path, registered_adapters
        )

    index = SystemIndex(
        schema_version=schema_version, systems=MappingProxyType(entries)
    )
    _validate_aliases(index, index_path)
    _validate_replacements(index, index_path)
    _validate_unique_paths(index, index_path)
    return index


def get_system_entry(index: SystemIndex, system_id: str) -> SystemIndexEntry:
    """Return a public system entry or raise a domain-specific error."""
    try:
        return index.systems[system_id]
    except KeyError:
        raise SystemIndexError(
            f"unknown system id {system_id!r}; available ids: "
            f"{', '.join(sorted(index.systems))}"
        ) from None


def resolve_alias(index: SystemIndex, system_id: str) -> tuple[str, tuple[str, ...]]:
    """Resolve an id to its runnable target and return the full id chain."""
    get_system_entry(index, system_id)

    chain: list[str] = []
    positions: dict[str, int] = {}
    current_id = system_id
    while True:
        if current_id in positions:
            cycle = chain[positions[current_id] :] + [current_id]
            raise SystemIndexError(f"alias cycle detected: {' -> '.join(cycle)}")

        positions[current_id] = len(chain)
        chain.append(current_id)
        entry = index.systems[current_id]
        if entry.alias_of is None:
            return current_id, tuple(chain)

        target_id = entry.alias_of
        if target_id not in index.systems:
            raise SystemIndexError(
                f"system {current_id!r} has unknown alias target {target_id!r}"
            )
        current_id = target_id


def _parse_entry(
    system_id: str, body: object, index_path: Path, registered_adapters: frozenset[str]
) -> SystemIndexEntry:
    if not isinstance(body, dict):
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} entry must be a mapping, "
            f"got {type(body).__name__}"
        )
    _require_string_keys(body, f"{index_path}: system {system_id!r}")

    unknown_keys = set(body) - _ENTRY_KEYS
    if unknown_keys:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} has unknown key(s): "
            f"{sorted(unknown_keys)}"
        )

    missing_keys = _REQUIRED_ENTRY_KEYS - set(body)
    if missing_keys:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} is missing required field(s): "
            f"{sorted(missing_keys)}"
        )

    adapter = _required_string(body["adapter"], system_id, "adapter", index_path)
    if adapter not in registered_adapters:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} adapter {adapter!r} is not a "
            f"registered adapter; expected one of {sorted(registered_adapters)}"
        )

    category_value = _required_string(
        body["category"], system_id, "category", index_path
    )
    if category_value not in VALID_CATEGORIES:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} category {category_value!r} "
            f"must be one of {sorted(VALID_CATEGORIES)}"
        )
    category = cast(SystemCategory, category_value)

    status_value = _required_string(body["status"], system_id, "status", index_path)
    if status_value not in VALID_STATUSES:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} status {status_value!r} "
            f"must be one of {sorted(VALID_STATUSES)}"
        )
    status = cast(SystemStatus, status_value)
    allowed_statuses = ALLOWED_STATUSES_BY_CATEGORY[category]
    if status not in allowed_statuses:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} category {category!r} does "
            f"not allow status {status!r}; expected one of "
            f"{sorted(allowed_statuses)}"
        )

    description = _required_string(
        body["description"], system_id, "description", index_path
    )

    has_path = "path" in body
    has_alias = "alias_of" in body
    if has_path and has_alias:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} defines both 'path' and "
            "'alias_of'; exactly one is allowed"
        )
    if not has_path and not has_alias:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} must define exactly one of "
            "'path' and 'alias_of'"
        )

    path: PurePosixPath | None = None
    alias_of: str | None = None
    if category == "alias":
        if not has_alias:
            raise SystemIndexError(
                f"{index_path}: system {system_id!r} category 'alias' must "
                "define alias_of instead of path"
            )
        alias_of = _required_string(body["alias_of"], system_id, "alias_of", index_path)
        _validate_system_id(
            alias_of, f"{index_path}: system {system_id!r} has invalid alias_of"
        )
    else:
        if has_alias:
            raise SystemIndexError(
                f"{index_path}: system {system_id!r} defines alias_of but "
                f"category is {category!r}; category must be 'alias'"
            )
        if not has_path:
            raise SystemIndexError(
                f"{index_path}: runnable system {system_id!r} must define path"
            )
        path = _relative_posix_path(body["path"], system_id, "path", index_path)

    replacement_value = body.get("replacement")
    replacement: str | None = None
    if replacement_value is not None:
        replacement = _required_string(
            replacement_value, system_id, "replacement", index_path
        )
        _validate_system_id(
            replacement, f"{index_path}: system {system_id!r} has invalid replacement"
        )

    if status == "deprecated" and replacement is None:
        raise SystemIndexError(
            f"{index_path}: deprecated system {system_id!r} must define replacement"
        )
    if status != "deprecated" and replacement is not None:
        raise SystemIndexError(
            f"{index_path}: non-deprecated system {system_id!r} cannot define "
            "replacement"
        )

    docs_value = body.get("docs")
    docs = (
        None
        if docs_value is None
        else _relative_posix_path(docs_value, system_id, "docs", index_path)
    )

    return SystemIndexEntry(
        system_id=system_id,
        adapter=adapter,
        category=category,
        status=status,
        path=path,
        alias_of=alias_of,
        replacement=replacement,
        description=description,
        docs=docs,
    )


def _required_string(
    value: object, system_id: str, field: str, index_path: Path
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} field {field!r} must be a "
            "non-empty string"
        )
    return value


def _relative_posix_path(
    value: object, system_id: str, field: str, index_path: Path
) -> PurePosixPath:
    raw_path = _required_string(value, system_id, field, index_path)
    if "\\" in raw_path:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} {field} must use a POSIX path"
        )

    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} {field} must be relative"
        )
    if path == PurePosixPath(".") or ".." in path.parts:
        raise SystemIndexError(
            f"{index_path}: system {system_id!r} {field} escapes the index "
            "directory or names no file"
        )
    return path


def _validate_system_id(system_id: str, context: str) -> None:
    if not _SYSTEM_ID_PATTERN.fullmatch(system_id):
        raise SystemIndexError(
            f"{context}: {system_id!r}; use lowercase letters, digits, "
            "underscores, and interior hyphens"
        )


def _validate_aliases(index: SystemIndex, index_path: Path) -> None:
    for system_id, entry in index.systems.items():
        if entry.category != "alias":
            continue
        canonical_id, _ = resolve_alias(index, system_id)
        target = index.systems[canonical_id]
        if entry.adapter != target.adapter:
            raise SystemIndexError(
                f"{index_path}: system {system_id!r} adapter {entry.adapter!r} "
                f"does not match resolved target {canonical_id!r} adapter "
                f"{target.adapter!r}"
            )


def _validate_replacements(index: SystemIndex, index_path: Path) -> None:
    for system_id, entry in index.systems.items():
        if entry.replacement is None:
            continue
        if entry.replacement not in index.systems:
            raise SystemIndexError(
                f"{index_path}: system {system_id!r} has unknown replacement "
                f"{entry.replacement!r}"
            )
        if entry.replacement == system_id:
            raise SystemIndexError(
                f"{index_path}: system {system_id!r} cannot replace itself"
            )


def _validate_unique_paths(index: SystemIndex, index_path: Path) -> None:
    owners: dict[PurePosixPath, str] = {}
    for system_id, entry in index.systems.items():
        if entry.path is None:
            continue
        previous = owners.get(entry.path)
        if previous is not None:
            raise SystemIndexError(
                f"{index_path}: duplicate path {str(entry.path)!r} for runnable "
                f"systems {previous!r} and {system_id!r}"
            )
        owners[entry.path] = system_id


def _require_string_keys(mapping: Mapping[object, object], context: str) -> None:
    non_string_keys = [key for key in mapping if not isinstance(key, str)]
    if non_string_keys:
        raise SystemIndexError(
            f"{context} mapping keys must be strings; got {non_string_keys!r}"
        )
