from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from evaluation.src.config.system_index import load_system_index, resolve_alias
from evaluation.src.config.yaml_loader import strict_safe_load

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_ROOT = REPO_ROOT / "evaluation" / "config" / "systems"
CATALOG_PATH = REPO_ROOT / "evaluation" / "docs" / "system-configs" / "README.md"
BASELINE_PATH = (
    REPO_ROOT
    / "tests"
    / "evaluation"
    / "fixtures"
    / "system_configs_before_cleanup.json"
)
DIRECTORY_BY_CATEGORY = {
    "canonical": "canonical",
    "experiment": "experiments",
    "ablation": "ablations",
    "tooling": "tooling",
}
RUNNABLE_DIRECTORIES = tuple(DIRECTORY_BY_CATEGORY.values())
LINK_CHECK_PATHS = (
    CATALOG_PATH,
    REPO_ROOT / "evaluation" / "docs" / "system-configs" / "openviking.md",
    REPO_ROOT / "evaluation" / "README.md",
    REPO_ROOT / "evaluation" / "docs" / "openclaw_adapter.md",
    REPO_ROOT / "docs" / "locomo-fair-baseline.md",
)
CATALOG_HEADER = (
    "| ID | Category | Status | Adapter | Canonical target | Physical config | "
    "Intended use |"
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _legacy_ids() -> set[str]:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert isinstance(baseline, dict)
    return set(baseline)


def _yaml_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".yaml", ".yml"}
    )


def _safe_config_path(relative_path: object) -> Path:
    assert isinstance(relative_path, str), relative_path
    path = PurePosixPath(relative_path)
    assert relative_path == path.as_posix(), relative_path
    assert not path.is_absolute(), relative_path
    assert "\\" not in relative_path, relative_path
    assert all(part not in {"", ".", ".."} for part in path.parts), relative_path

    candidate = SYSTEMS_ROOT.joinpath(*path.parts)
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(SYSTEMS_ROOT.resolve(strict=True))
    assert candidate.absolute() == resolved, f"symlinked config path: {relative_path}"
    assert resolved.is_file(), relative_path
    return resolved


def _inheritance_chain(relative_path: str) -> tuple[Path, ...]:
    chain: list[Path] = []
    current = relative_path
    while True:
        path = _safe_config_path(current)
        assert path not in chain, f"inheritance cycle: {chain + [path]}"
        chain.append(path)
        document = strict_safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), path
        parent = document.get("extends")
        if parent is None:
            return tuple(reversed(chain))
        assert isinstance(parent, str), path
        current = parent


def _catalog_rows() -> dict[str, list[str]]:
    lines = CATALOG_PATH.read_text(encoding="utf-8").splitlines()
    assert CATALOG_HEADER in lines
    header_index = lines.index(CATALOG_HEADER)
    assert re.fullmatch(r"\|(?:\s*:?-+:?\s*\|){7}", lines[header_index + 1])

    rows: dict[str, list[str]] = {}
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 7, line
        match = re.fullmatch(r"`([^`]+)`", cells[0])
        assert match is not None, line
        system_id = match.group(1)
        assert system_id not in rows, f"duplicate catalog row: {system_id}"
        rows[system_id] = cells
    return rows


def test_index_and_physical_tree_form_one_complete_catalog() -> None:
    index = load_system_index()
    legacy_ids = _legacy_ids()

    assert len(legacy_ids) == 36
    assert set(index.systems) == legacy_ids
    assert not tuple(
        path
        for path in _yaml_paths(SYSTEMS_ROOT)
        if path.parent == SYSTEMS_ROOT and path.name != "index.yaml"
    )

    registered_paths = [
        entry.path for entry in index.systems.values() if entry.category != "alias"
    ]
    assert all(path is not None for path in registered_paths)
    path_counts = Counter(registered_paths)
    assert all(count == 1 for count in path_counts.values()), path_counts

    physical_paths = {
        path.relative_to(SYSTEMS_ROOT)
        for path in _yaml_paths(SYSTEMS_ROOT)
        if path.name != "index.yaml"
        and path.relative_to(SYSTEMS_ROOT).parts[0] != "_bases"
    }
    assert all(path.parts[0] in RUNNABLE_DIRECTORIES for path in physical_paths)
    assert set(path_counts) == {
        PurePosixPath(path.as_posix()) for path in physical_paths
    }

    for system_id, entry in index.systems.items():
        if entry.category == "alias":
            assert entry.path is None
            assert not tuple(path for path in physical_paths if path.stem == system_id)
        else:
            assert entry.path is not None
            assert entry.path.parts[0] == DIRECTORY_BY_CATEGORY[entry.category]
            _safe_config_path(entry.path.as_posix())

    physical_documents = [
        strict_safe_load(path.read_text(encoding="utf-8"))
        for path in _yaml_paths(SYSTEMS_ROOT)
        if path.name != "index.yaml"
    ]
    fingerprints = [
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for document in physical_documents
    ]
    assert len(fingerprints) == len(set(fingerprints))


def test_every_base_is_reached_by_a_registered_source_chain() -> None:
    index = load_system_index()
    reached_paths = {
        path
        for entry in index.systems.values()
        if entry.path is not None
        for path in _inheritance_chain(entry.path.as_posix())
    }
    base_paths = {
        path.resolve(strict=True) for path in _yaml_paths(SYSTEMS_ROOT / "_bases")
    }

    assert base_paths
    assert base_paths <= reached_paths


def test_catalog_has_one_metadata_complete_row_per_legacy_id() -> None:
    index = load_system_index()
    rows = _catalog_rows()

    assert set(rows) == set(index.systems) == _legacy_ids()
    for system_id, entry in index.systems.items():
        _, category, status, adapter, canonical_target, physical_config, intended = (
            rows[system_id]
        )
        assert category == f"`{entry.category}`"
        assert status == f"`{entry.status}`"
        assert adapter == f"`{entry.adapter}`"
        resolved_target, _ = resolve_alias(index, system_id)
        assert canonical_target == f"`{resolved_target}`"
        if entry.path is None:
            assert physical_config == "—"
        else:
            expected_link = f"../../config/systems/{entry.path.as_posix()}"
            assert f"({expected_link})" in physical_config
        assert intended

    serial_intended_use = rows["openclaw-docker-openviking-session-bundle-noop-serial"][
        -1
    ].lower()
    assert "diagnostic-only" in serial_intended_use
    assert "not suitable for benchmark scoring" in serial_intended_use


def test_active_system_documentation_has_only_resolvable_local_links() -> None:
    root = REPO_ROOT.resolve(strict=True)
    for document_path in LINK_CHECK_PATHS:
        text = document_path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            raw_destination = match.group(1).strip()
            if raw_destination.startswith("<"):
                closing_bracket = raw_destination.find(">")
                assert closing_bracket > 1, (document_path, raw_destination)
                destination = raw_destination[1:closing_bracket]
            else:
                destination = raw_destination.split(maxsplit=1)[0]
            if not destination or destination.startswith("#"):
                continue
            if destination.startswith("//"):
                continue
            if re.match(r"^[a-z][a-z0-9+.-]*:", destination, re.IGNORECASE):
                continue

            local_path = unquote(destination.split("#", 1)[0].split("?", 1)[0])
            assert local_path, (document_path, destination)
            assert not PurePosixPath(local_path).is_absolute(), (
                document_path,
                destination,
            )
            resolved = (document_path.parent / local_path).resolve(strict=False)
            resolved.relative_to(root)
            assert resolved.exists(), f"broken link in {document_path}: {destination}"
