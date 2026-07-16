from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest
import yaml

from evaluation.src.config.system_index import (
    DEFAULT_SYSTEM_INDEX_PATH,
    SystemIndexError,
    get_system_entry,
    load_system_index,
    resolve_alias,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = (
    REPO_ROOT
    / "tests"
    / "evaluation"
    / "fixtures"
    / "system_configs_before_cleanup.json"
)
EXPECTED_IDS_BY_CATEGORY = {
    "canonical": {
        "evermemos",
        "evermemos_cloud_api",
        "evermemos_local_api",
        "mem0",
        "memos",
        "memu",
        "zep",
        "hermes-holographic",
        "hermes-honcho",
        "hermes-hindsight",
        "openclaw",
        "openclaw-fts",
        "openclaw-fts-noflush",
        "openclaw-hybrid-noflush",
        "openclaw-vector",
        "openclaw-vector-noflush",
        "openclaw-docker",
        "openclaw-docker-evermemos",
        "openclaw-docker-mem0",
        "openclaw-docker-memcore-session-bundle",
        "openclaw-docker-openviking-session-bundle-memcore",
        "openclaw-docker-openviking-session-bundle-noop",
    },
    "alias": {"hermes", "openclaw-hybrid"},
    "experiment": {
        "openclaw-agent-local",
        "openclaw-hypercompositor",
        "openclaw-native-embed",
        "openclaw-native-noembed",
        "openclaw-noop",
        "openclaw-docker-hypercompositor",
        "openclaw-docker-memclaw",
        "openclaw-docker-stub",
    },
    "ablation": {
        "openclaw-docker-memcore-session-bundle-emptytail",
        "openclaw-docker-memcore-session-bundle-weaktail",
        "openclaw-docker-openviking-session-bundle-noop-fixpack",
    },
    "tooling": {"openclaw-docker-openviking-session-bundle-noop-serial"},
}


def _index_document() -> dict[str, object]:
    document = yaml.safe_load(DEFAULT_SYSTEM_INDEX_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _systems(document: dict[str, object]) -> dict[str, dict[str, object]]:
    systems = document["systems"]
    assert isinstance(systems, dict)
    return systems


def _write_index(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "index.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _write_raw_index(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "index.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _assert_invalid(
    tmp_path: Path, document: object, message_pattern: str
) -> SystemIndexError:
    path = _write_index(tmp_path, document)
    with pytest.raises(SystemIndexError, match=message_pattern) as error:
        load_system_index(path)
    return error.value


def test_shipped_index_preserves_and_classifies_all_legacy_ids() -> None:
    baseline_ids = set(json.loads(BASELINE_PATH.read_text(encoding="utf-8")))

    index = load_system_index()

    assert index.schema_version == 1
    assert set(index.systems) == baseline_ids
    assert len(index.systems) == 36
    assert {
        category: {
            system_id
            for system_id, entry in index.systems.items()
            if entry.category == category
        }
        for category in EXPECTED_IDS_BY_CATEGORY
    } == EXPECTED_IDS_BY_CATEGORY
    assert {
        category: len(ids) for category, ids in EXPECTED_IDS_BY_CATEGORY.items()
    } == {"canonical": 22, "alias": 2, "experiment": 8, "ablation": 3, "tooling": 1}
    assert {
        system_id: entry.alias_of
        for system_id, entry in index.systems.items()
        if entry.category == "alias"
    } == {"hermes": "hermes-holographic", "openclaw-hybrid": "openclaw"}

    for system_id, entry in index.systems.items():
        if entry.category == "canonical":
            assert entry.status == "active"
        elif entry.category == "alias":
            assert entry.status == "compatibility"
        else:
            assert entry.status == "experimental"

        assert entry.description.strip()
        assert entry.replacement is None
        assert entry.docs is None

        if entry.category == "alias":
            assert entry.path is None
        else:
            assert entry.path == PurePosixPath(f"{system_id}.yaml")
            assert (DEFAULT_SYSTEM_INDEX_PATH.parent / entry.path).is_file()


def test_shipped_index_uses_registered_adapter_families() -> None:
    index = load_system_index()

    assert get_system_entry(index, "evermemos").adapter == "evermemos"
    assert get_system_entry(index, "evermemos_cloud_api").adapter == "evermemos_api"
    assert get_system_entry(index, "evermemos_local_api").adapter == "evermemos_api"
    for system_id in ("mem0", "memos", "memu", "zep"):
        assert get_system_entry(index, system_id).adapter == system_id
    for system_id, entry in index.systems.items():
        if system_id.startswith("hermes"):
            assert entry.adapter == "hermes"
        elif system_id.startswith("openclaw-docker"):
            assert entry.adapter == "openclaw-docker"
        elif system_id.startswith("openclaw"):
            assert entry.adapter == "openclaw"


def test_index_entries_and_mapping_are_immutable() -> None:
    index = load_system_index()
    entry = get_system_entry(index, "openclaw")

    with pytest.raises(FrozenInstanceError):
        entry.adapter = "mem0"  # type: ignore[misc]
    with pytest.raises(TypeError):
        index.systems["new-system"] = entry  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        index.schema_version = 2  # type: ignore[misc]


def test_lookup_and_alias_resolution_report_canonical_chain() -> None:
    index = load_system_index()

    assert get_system_entry(index, "hermes").alias_of == "hermes-holographic"
    assert resolve_alias(index, "hermes") == (
        "hermes-holographic",
        ("hermes", "hermes-holographic"),
    )
    assert resolve_alias(index, "openclaw-hybrid") == (
        "openclaw",
        ("openclaw-hybrid", "openclaw"),
    )
    assert resolve_alias(index, "mem0") == ("mem0", ("mem0",))


def test_unknown_system_lookup_is_a_domain_error() -> None:
    index = load_system_index()

    with pytest.raises(SystemIndexError, match="unknown system id.*missing-system"):
        get_system_entry(index, "missing-system")
    with pytest.raises(SystemIndexError, match="unknown system id.*missing-system"):
        resolve_alias(index, "missing-system")


def test_missing_index_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemIndexError, match="not found"):
        load_system_index(tmp_path / "missing.yaml")


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "index.yaml"
    path.write_text("systems: [unterminated\n", encoding="utf-8")

    with pytest.raises(SystemIndexError, match="invalid YAML"):
        load_system_index(path)


def test_invalid_utf8_is_wrapped_as_a_system_index_error(tmp_path: Path) -> None:
    path = tmp_path / "index.yaml"
    path.write_bytes(b"\xff\xfeschema_version: 1\n")

    with pytest.raises(SystemIndexError, match="UTF-8") as error:
        load_system_index(path)

    assert isinstance(error.value.__cause__, UnicodeError)


@pytest.mark.parametrize(
    ("duplicate_key", "make_duplicate"),
    [
        (
            "schema_version",
            lambda text: text.replace(
                "schema_version: 1\n", "schema_version: 1\nschema_version: 1\n", 1
            ),
        ),
        ("systems", lambda text: f"{text}systems: {{}}\n"),
        ("evermemos", lambda text: f"{text}  evermemos: {{}}\n"),
        (
            "adapter",
            lambda text: text.replace(
                "    adapter: evermemos\n",
                "    adapter: evermemos\n    adapter: mem0\n",
                1,
            ),
        ),
        (
            "path",
            lambda text: text.replace(
                "    description: Default hybrid OpenClaw benchmark preset.\n",
                "    description: Default hybrid OpenClaw benchmark preset.\n"
                "    docs:\n"
                "      path: first.md\n"
                "      path: second.md\n",
                1,
            ),
        ),
    ],
)
def test_duplicate_mapping_keys_are_rejected_at_every_depth(
    tmp_path: Path, duplicate_key: str, make_duplicate: Callable[[str], str]
) -> None:
    raw = DEFAULT_SYSTEM_INDEX_PATH.read_text(encoding="utf-8")
    path = _write_raw_index(tmp_path, make_duplicate(raw))

    with pytest.raises(SystemIndexError) as error:
        load_system_index(path)

    message = str(error.value)
    assert "duplicate key" in message
    assert duplicate_key in message
    assert "line" in message
    assert "column" in message


@pytest.mark.parametrize(
    "yaml_text",
    [
        """
defaults: &defaults
  evermemos: {}
schema_version: 1
systems:
  <<: *defaults
""",
        """
first: &first
  evermemos: {}
second: &second
  mem0: {}
schema_version: 1
systems:
  <<: *first
  <<: *second
""",
        """
defaults: &defaults
  adapter: evermemos
schema_version: 1
systems:
  evermemos:
    <<: *defaults
    adapter: mem0
""",
    ],
    ids=["single-merge", "repeated-merges", "merge-with-explicit-override"],
)
def test_yaml_merge_keys_are_rejected_before_schema_validation(
    tmp_path: Path, yaml_text: str
) -> None:
    path = _write_raw_index(tmp_path, yaml_text)

    with pytest.raises(SystemIndexError) as error:
        load_system_index(path)

    message = str(error.value)
    assert "merge key" in message
    assert "<<" in message
    assert "line" in message
    assert "column" in message


@pytest.mark.parametrize("document", [None, [], "systems"])
def test_top_level_value_must_be_a_mapping(tmp_path: Path, document: object) -> None:
    _assert_invalid(tmp_path, document, "top-level mapping")


def test_unknown_top_level_keys_are_rejected(tmp_path: Path) -> None:
    document = _index_document()
    document["unexpected"] = True

    _assert_invalid(tmp_path, document, "unknown top-level key.*unexpected")


@pytest.mark.parametrize("schema_version", [None, 0, 2, "1", True])
def test_schema_version_must_be_integer_one(
    tmp_path: Path, schema_version: object
) -> None:
    document = _index_document()
    if schema_version is None:
        document.pop("schema_version")
    else:
        document["schema_version"] = schema_version

    _assert_invalid(tmp_path, document, "schema_version.*1")


def test_systems_value_must_be_a_mapping(tmp_path: Path) -> None:
    document = _index_document()
    document["systems"] = []

    _assert_invalid(tmp_path, document, "systems.*mapping")


def test_unknown_entry_keys_are_rejected(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["openclaw"]["unexpected"] = True

    _assert_invalid(tmp_path, document, "openclaw.*unknown key.*unexpected")


@pytest.mark.parametrize(
    "invalid_id",
    ["Invalid", "invalid/id", "../invalid", "invalid id", "-invalid", "invalid-"],
)
def test_invalid_system_ids_are_rejected(tmp_path: Path, invalid_id: str) -> None:
    document = _index_document()
    systems = _systems(document)
    systems[invalid_id] = systems.pop("evermemos")

    _assert_invalid(tmp_path, document, "invalid system id")


def test_entry_values_must_be_mappings(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["openclaw"] = []  # type: ignore[assignment]

    _assert_invalid(tmp_path, document, "openclaw.*mapping")


def test_entry_cannot_define_both_path_and_alias_target(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["evermemos"]["alias_of"] = "mem0"

    _assert_invalid(tmp_path, document, "evermemos.*both.*path.*alias_of")


@pytest.mark.parametrize(
    ("system_id", "path_value", "alias_value"),
    [("evermemos", "evermemos.yaml", None), ("hermes", None, "hermes-holographic")],
)
def test_path_and_alias_keys_are_mutually_exclusive_even_when_one_is_null(
    tmp_path: Path, system_id: str, path_value: object, alias_value: object
) -> None:
    document = _index_document()
    entry = _systems(document)[system_id]
    entry["path"] = path_value
    entry["alias_of"] = alias_value

    _assert_invalid(tmp_path, document, f"{system_id}.*both.*path.*alias_of")


@pytest.mark.parametrize(
    ("system_id", "selected_key", "invalid_value"),
    [
        ("evermemos", "path", None),
        ("evermemos", "path", ""),
        ("evermemos", "path", "   "),
        ("evermemos", "path", 7),
        ("hermes", "alias_of", None),
        ("hermes", "alias_of", ""),
        ("hermes", "alias_of", "   "),
        ("hermes", "alias_of", 7),
    ],
)
def test_selected_path_or_alias_key_must_have_a_non_empty_string_value(
    tmp_path: Path, system_id: str, selected_key: str, invalid_value: object
) -> None:
    document = _index_document()
    entry = _systems(document)[system_id]
    entry[selected_key] = invalid_value

    _assert_invalid(
        tmp_path,
        document,
        rf"{system_id}.*field ['\"]{selected_key}['\"].*non-empty string",
    )


def test_runnable_entry_must_define_a_path(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["evermemos"].pop("path")

    _assert_invalid(tmp_path, document, "evermemos.*path")


def test_alias_entry_must_define_an_alias_target(tmp_path: Path) -> None:
    document = _index_document()
    systems = _systems(document)
    systems["openclaw"].pop("path")
    systems["openclaw"]["category"] = "alias"
    systems["openclaw"]["status"] = "compatibility"

    _assert_invalid(tmp_path, document, "openclaw.*alias_of")


def test_alias_target_requires_alias_category(tmp_path: Path) -> None:
    document = _index_document()
    entry = _systems(document)["hermes"]
    entry["category"] = "canonical"
    entry["status"] = "active"

    _assert_invalid(tmp_path, document, "hermes.*category.*alias")


def test_alias_category_cannot_point_at_a_path(tmp_path: Path) -> None:
    document = _index_document()
    systems = _systems(document)
    systems["openclaw"]["category"] = "alias"
    systems["openclaw"]["status"] = "compatibility"

    _assert_invalid(tmp_path, document, "openclaw.*alias_of")


@pytest.mark.parametrize("field", ["adapter", "category", "status", "description"])
def test_required_entry_fields_cannot_be_omitted(tmp_path: Path, field: str) -> None:
    document = _index_document()
    _systems(document)["openclaw"].pop(field)

    _assert_invalid(tmp_path, document, f"openclaw.*{field}")


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("adapter", "not-registered", "registered adapter"),
        ("category", "base", "category"),
        ("status", "retired", "status"),
        ("description", "   ", "description"),
    ],
)
def test_entry_metadata_values_are_strictly_validated(
    tmp_path: Path, field: str, value: object, pattern: str
) -> None:
    document = _index_document()
    _systems(document)["openclaw"][field] = value

    _assert_invalid(tmp_path, document, f"openclaw.*{pattern}")


@pytest.mark.parametrize(
    ("category", "invalid_status"),
    [
        ("canonical", "compatibility"),
        ("canonical", "experimental"),
        ("alias", "active"),
        ("alias", "experimental"),
        ("experiment", "active"),
        ("experiment", "compatibility"),
        ("ablation", "active"),
        ("ablation", "compatibility"),
        ("tooling", "active"),
        ("tooling", "compatibility"),
    ],
)
def test_category_status_matrix_rejects_invalid_combinations(
    tmp_path: Path, category: str, invalid_status: str
) -> None:
    document = _index_document()
    system_id = min(EXPECTED_IDS_BY_CATEGORY[category])
    _systems(document)[system_id]["status"] = invalid_status

    _assert_invalid(
        tmp_path,
        document,
        rf"{system_id}.*category ['\"]{category}['\"].*status "
        rf"['\"]{invalid_status}['\"]",
    )


@pytest.mark.parametrize("unsafe_path", ["/absolute.yaml", "../escape.yaml"])
def test_config_paths_must_be_relative_and_contained(
    tmp_path: Path, unsafe_path: str
) -> None:
    document = _index_document()
    _systems(document)["openclaw"]["path"] = unsafe_path

    _assert_invalid(tmp_path, document, "openclaw.*path.*relative|escape")


@pytest.mark.parametrize("unsafe_path", ["/absolute.md", "../escape.md"])
def test_documentation_paths_must_be_relative_and_contained(
    tmp_path: Path, unsafe_path: str
) -> None:
    document = _index_document()
    _systems(document)["openclaw"]["docs"] = unsafe_path

    _assert_invalid(tmp_path, document, "openclaw.*docs.*relative|escape")


def test_alias_with_unknown_target_is_rejected(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["hermes"]["alias_of"] = "missing-target"

    _assert_invalid(tmp_path, document, "hermes.*unknown alias target.*missing-target")


def test_alias_cycles_are_rejected(tmp_path: Path) -> None:
    document = _index_document()
    systems = _systems(document)
    systems["hermes"]["alias_of"] = "openclaw-hybrid"
    systems["openclaw-hybrid"]["alias_of"] = "hermes"

    _assert_invalid(tmp_path, document, "alias cycle.*hermes.*openclaw-hybrid")


def test_alias_adapter_must_match_resolved_target(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["hermes"]["adapter"] = "openclaw"

    _assert_invalid(tmp_path, document, "hermes.*adapter.*hermes-holographic")


def test_duplicate_paths_for_runnable_entries_are_rejected(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["evermemos_cloud_api"]["path"] = "evermemos.yaml"

    _assert_invalid(
        tmp_path, document, "duplicate path.*evermemos.*evermemos_cloud_api"
    )


def test_deprecated_entry_requires_a_replacement(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["openclaw"]["status"] = "deprecated"

    _assert_invalid(tmp_path, document, "deprecated system.*openclaw.*replacement")


def test_non_deprecated_entry_cannot_define_a_replacement(tmp_path: Path) -> None:
    document = _index_document()
    _systems(document)["openclaw"]["replacement"] = "mem0"

    _assert_invalid(tmp_path, document, "non-deprecated system.*openclaw.*replacement")


def test_replacement_target_must_exist(tmp_path: Path) -> None:
    document = _index_document()
    entry = _systems(document)["openclaw"]
    entry["status"] = "deprecated"
    entry["replacement"] = "missing-system"

    _assert_invalid(tmp_path, document, "openclaw.*unknown replacement.*missing-system")


def test_system_cannot_replace_itself(tmp_path: Path) -> None:
    document = _index_document()
    entry = _systems(document)["openclaw"]
    entry["status"] = "deprecated"
    entry["replacement"] = "openclaw"

    _assert_invalid(tmp_path, document, "openclaw.*replace itself")


@pytest.mark.parametrize(
    "system_id",
    [
        "openclaw",
        "hermes",
        "openclaw-agent-local",
        "openclaw-docker-memcore-session-bundle-emptytail",
        "openclaw-docker-openviking-session-bundle-noop-serial",
    ],
)
def test_deprecated_status_is_valid_for_every_category(
    tmp_path: Path, system_id: str
) -> None:
    document = _index_document()
    entry = _systems(document)[system_id]
    entry["status"] = "deprecated"
    entry["replacement"] = "mem0"

    index = load_system_index(_write_index(tmp_path, document))

    assert index.systems[system_id].status == "deprecated"
    assert index.systems[system_id].replacement == "mem0"


@pytest.mark.parametrize("change", ["missing", "unexpected"])
def test_public_id_set_must_match_the_locked_legacy_surface(
    tmp_path: Path, change: str
) -> None:
    document = _index_document()
    systems = _systems(document)
    if change == "missing":
        systems.pop("evermemos")
    else:
        extra = deepcopy(systems["evermemos"])
        extra["path"] = "extra-system.yaml"
        systems["extra-system"] = extra

    error = _assert_invalid(tmp_path, document, "public system id set")
    assert change in str(error)
