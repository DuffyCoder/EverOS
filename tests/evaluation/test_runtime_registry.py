"""Tests for declarative evaluation runtime integration."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evaluation.src.plugins.runtime_registry import (
    DEFAULT_RUNTIME_REGISTRY_PATH,
    RuntimeEntry,
    RuntimeRegistryError,
    get_runtime,
    load_runtime_registry,
    render_command,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_registry(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "runtime_registry.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_shipped_registry_declares_openclaw_build_command():
    registry = load_runtime_registry(DEFAULT_RUNTIME_REGISTRY_PATH)

    entry = get_runtime(registry, "openclaw-docker")

    assert entry == RuntimeEntry(
        id="openclaw-docker",
        build_command=("{python}", "{repo_root}/openclaw-eval/harness/build.py"),
    )


def test_custom_registry_command_renders_absolute_argv(tmp_path: Path):
    registry_path = _write_registry(
        tmp_path,
        """
        custom-runtime:
          build_command:
            - "{python}"
            - "{repo_root}/custom/build.py"
        """,
    )
    registry = load_runtime_registry(registry_path)
    entry = get_runtime(registry, "custom-runtime")
    repo_root = tmp_path / "repo"
    python = tmp_path / "venv" / "bin" / "python"

    argv = render_command(entry.build_command, repo_root=repo_root, python=python)

    assert isinstance(argv, list)
    assert argv == [
        str(python.absolute()),
        str((repo_root / "custom" / "build.py").resolve()),
    ]


def test_python_placeholder_preserves_selected_interpreter_symlink(tmp_path: Path):
    real_python = tmp_path / "python3.12"
    real_python.touch()
    selected_python = tmp_path / "venv" / "bin" / "python"
    selected_python.parent.mkdir(parents=True)
    selected_python.symlink_to(real_python)

    argv = render_command(
        ("{python}",), repo_root=tmp_path / "repo", python=selected_python
    )

    assert argv == [str(selected_python.absolute())]


def test_runtime_entry_is_frozen():
    entry = RuntimeEntry(id="runtime", build_command=("{python}",))

    with pytest.raises((AttributeError, TypeError)):
        entry.id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("body", ["- runtime\n", "", "null\n"])
def test_registry_top_level_must_be_mapping(tmp_path: Path, body: str):
    path = _write_registry(tmp_path, body)

    with pytest.raises(RuntimeRegistryError, match="top-level mapping"):
        load_runtime_registry(path)


def test_registry_entry_must_be_mapping(tmp_path: Path):
    path = _write_registry(tmp_path, "runtime: not-a-mapping\n")

    with pytest.raises(RuntimeRegistryError, match="must be a mapping"):
        load_runtime_registry(path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("runtime: {}\n", "build_command"),
        ("runtime:\n  build_command: []\n", "non-empty"),
        ("runtime:\n  build_command: 42\n", "list"),
        ("runtime:\n  build_command: ['{python}', 42]\n", "strings"),
        ("runtime:\n  build_command: ['{python}', '']\n", "non-empty"),
    ],
)
def test_build_command_must_be_non_empty_string_list(
    tmp_path: Path, body: str, message: str
):
    path = _write_registry(tmp_path, body)

    with pytest.raises(RuntimeRegistryError, match=message):
        load_runtime_registry(path)


def test_unknown_runtime_id_is_rejected(tmp_path: Path):
    path = _write_registry(
        tmp_path,
        """
        known:
          build_command: ["{python}"]
        """,
    )
    registry = load_runtime_registry(path)

    with pytest.raises(RuntimeRegistryError, match="unknown runtime"):
        get_runtime(registry, "missing")


def test_unknown_command_placeholder_is_rejected(tmp_path: Path):
    path = _write_registry(
        tmp_path,
        """
        runtime:
          build_command: ["{python}", "{workspace}/build.py"]
        """,
    )
    entry = get_runtime(load_runtime_registry(path), "runtime")

    with pytest.raises(RuntimeRegistryError, match="unknown placeholder.*workspace"):
        render_command(
            entry.build_command, repo_root=REPO_ROOT, python=Path("/usr/bin/python3")
        )


@pytest.mark.parametrize("argument", ["{repo_root", "{repo_root}}"])
def test_malformed_command_template_is_rejected(tmp_path: Path, argument: str):
    with pytest.raises(RuntimeRegistryError, match="invalid command template"):
        render_command(
            (argument,), repo_root=tmp_path / "repo", python=Path("/usr/bin/python3")
        )


@pytest.mark.parametrize(
    "argument", ["{repo_root!s}/build.py", "{repo_root:>10}/build.py"]
)
def test_placeholder_conversion_and_format_spec_are_rejected(
    tmp_path: Path, argument: str
):
    with pytest.raises(RuntimeRegistryError, match="not allowed"):
        render_command(
            (argument,), repo_root=tmp_path / "repo", python=Path("/usr/bin/python3")
        )


def test_multiple_repo_root_placeholders_in_one_argument_are_rejected(tmp_path: Path):
    command = ("{repo_root}/safe:{repo_root}/../outside",)

    with pytest.raises(RuntimeRegistryError, match="multiple.*repo_root"):
        render_command(
            command, repo_root=tmp_path / "repo", python=Path("/usr/bin/python3")
        )


def test_repo_path_symlink_cannot_escape_repo_root(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_builder = tmp_path / "outside" / "build.py"
    outside_builder.parent.mkdir()
    outside_builder.touch()
    (repo_root / "build.py").symlink_to(outside_builder)

    with pytest.raises(RuntimeRegistryError, match="escapes repo root"):
        render_command(
            ("{repo_root}/build.py",),
            repo_root=repo_root,
            python=Path("/usr/bin/python3"),
        )


def test_repo_path_argument_cannot_escape_repo_root(tmp_path: Path):
    path = _write_registry(
        tmp_path,
        """
        runtime:
          build_command:
            - "{python}"
            - "{repo_root}/../outside/build.py"
        """,
    )
    entry = get_runtime(load_runtime_registry(path), "runtime")

    with pytest.raises(RuntimeRegistryError, match="escapes repo root"):
        render_command(
            entry.build_command,
            repo_root=tmp_path / "repo",
            python=Path("/usr/bin/python3"),
        )


def test_opaque_flags_and_values_are_not_treated_as_paths(tmp_path: Path):
    command = (
        "{python}",
        "{repo_root}/build.py",
        "--plugin",
        "@scope/package",
        "--tag=registry/image:latest",
    )

    assert render_command(
        command, repo_root=tmp_path / "repo", python=Path("/usr/bin/python3")
    )[-3:] == ["--plugin", "@scope/package", "--tag=registry/image:latest"]
