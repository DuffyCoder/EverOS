"""Declarative build commands for evaluation runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Sequence

import yaml


DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_REGISTRY_PATH = (
    DEFAULT_REPO_ROOT / "evaluation" / "config" / "runtime_registry.yaml"
)

_ALLOWED_PLACEHOLDERS = frozenset({"python", "repo_root"})


class RuntimeRegistryError(ValueError):
    """Raised when runtime metadata or a rendered command is invalid."""


@dataclass(frozen=True)
class RuntimeEntry:
    """A named evaluation runtime and its base build command."""

    id: str
    build_command: tuple[str, ...]


def load_runtime_registry(
    path: str | Path = DEFAULT_RUNTIME_REGISTRY_PATH,
) -> dict[str, RuntimeEntry]:
    """Load and validate a runtime registry without environment expansion."""
    registry_path = Path(path)
    if not registry_path.exists():
        raise RuntimeRegistryError(f"runtime registry not found: {registry_path}")

    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuntimeRegistryError(
            f"{registry_path} contains invalid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RuntimeRegistryError(
            f"{registry_path} must be a top-level mapping, " f"got {type(raw).__name__}"
        )

    registry: dict[str, RuntimeEntry] = {}
    for runtime_id, body in raw.items():
        if not isinstance(runtime_id, str) or not runtime_id:
            raise RuntimeRegistryError(
                f"{registry_path}: runtime ids must be non-empty strings"
            )
        if not isinstance(body, dict):
            raise RuntimeRegistryError(
                f"{registry_path}: '{runtime_id}' value must be a mapping, "
                f"got {type(body).__name__}"
            )

        build_command = body.get("build_command")
        if not isinstance(build_command, list):
            raise RuntimeRegistryError(
                f"{registry_path}: '{runtime_id}' build_command must be a list"
            )
        if not build_command:
            raise RuntimeRegistryError(
                f"{registry_path}: '{runtime_id}' build_command must be non-empty"
            )
        if any(not isinstance(argument, str) for argument in build_command):
            raise RuntimeRegistryError(
                f"{registry_path}: '{runtime_id}' build_command must contain "
                "only strings"
            )
        if any(not argument.strip() for argument in build_command):
            raise RuntimeRegistryError(
                f"{registry_path}: '{runtime_id}' build_command arguments "
                "must be non-empty"
            )

        registry[runtime_id] = RuntimeEntry(
            id=runtime_id, build_command=tuple(build_command)
        )

    return registry


def get_runtime(registry: dict[str, RuntimeEntry], runtime_id: str) -> RuntimeEntry:
    """Return a runtime entry or raise a domain-specific lookup error."""
    try:
        return registry[runtime_id]
    except KeyError:
        raise RuntimeRegistryError(
            f"unknown runtime: '{runtime_id}'. add it to runtime_registry.yaml"
        ) from None


def render_command(
    command: Sequence[str], *, repo_root: Path, python: Path
) -> list[str]:
    """Render a command to argv and constrain repo-derived paths to the repo.

    Only arguments containing ``{repo_root}`` are treated as repository paths.
    Other flags and values stay opaque, so plugin ids and image tags are not
    mistaken for filesystem paths.
    """
    resolved_repo_root = repo_root.resolve()
    replacements = {
        # Keep a selected virtualenv interpreter symlink intact. Resolving it
        # can silently switch the child process to the system Python.
        "python": str(python.absolute()),
        "repo_root": str(resolved_repo_root),
    }
    formatter = Formatter()
    argv: list[str] = []

    for argument in command:
        try:
            parsed = list(formatter.parse(argument))
        except ValueError as exc:
            raise RuntimeRegistryError(
                f"invalid command template {argument!r}: {exc}"
            ) from exc

        placeholders: list[str] = []
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in _ALLOWED_PLACEHOLDERS:
                raise RuntimeRegistryError(
                    f"unknown placeholder '{field_name}' in command argument "
                    f"{argument!r}"
                )
            if format_spec or conversion:
                raise RuntimeRegistryError(
                    f"format specifiers are not allowed for placeholder "
                    f"'{field_name}'"
                )
            placeholders.append(field_name)

        if placeholders.count("repo_root") > 1:
            raise RuntimeRegistryError(
                f"multiple repo_root placeholders are not allowed in one "
                f"command argument: {argument!r}"
            )

        rendered = argument.format_map(replacements)
        if "repo_root" in placeholders:
            root_offset = rendered.find(str(resolved_repo_root))
            candidate = Path(rendered[root_offset:]).resolve()
            if not candidate.is_relative_to(resolved_repo_root):
                raise RuntimeRegistryError(
                    f"rendered path escapes repo root: {rendered!r}"
                )
        argv.append(rendered)

    return argv
