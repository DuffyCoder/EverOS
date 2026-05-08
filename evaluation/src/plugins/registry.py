"""Plugin registry: load + validate plugin_registry.yaml.

Single source of truth for "plugin id -> kind + how-to-build". Used by
both build.py (decides what to install/stage) and evaluation.cli
(validates --memory-plugin / --context-engine arguments).

See evaluation/config/plugin_registry.yaml for the schema reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


VALID_KINDS = {"memory", "context-engine"}
VALID_TYPES = {"base-extension", "bundled-source", "npm"}

# Anchored at the repo root so callers don't accidentally resolve it
# against cwd. parents[3] = repo root (this file is at
# evaluation/src/plugins/registry.py).
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "evaluation" / "config" / "plugin_registry.yaml"
)


class RegistryError(ValueError):
    """Raised when registry contents are invalid or a lookup fails."""


@dataclass(frozen=True)
class PluginEntry:
    id: str
    kinds: frozenset[str]
    type: str
    npm_package: str | None
    source_dir: str | None

    def supports_kind(self, kind: str) -> bool:
        return kind in self.kinds

    def is_bundled(self) -> bool:
        return self.type in ("base-extension", "bundled-source")


def load_registry(path: str | Path) -> dict[str, PluginEntry]:
    p = Path(path)
    if not p.exists():
        raise RegistryError(f"plugin registry not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise RegistryError(
            f"{p} must be a top-level mapping, got {type(raw).__name__}"
        )
    return {pid: _parse_entry(pid, body, p) for pid, body in raw.items()}


def _parse_entry(plugin_id: str, body: object, registry_path: Path) -> PluginEntry:
    if not isinstance(body, dict):
        raise RegistryError(
            f"{registry_path}: '{plugin_id}' value must be a mapping, "
            f"got {type(body).__name__}"
        )

    kinds = _normalize_kinds(plugin_id, body.get("kind"), registry_path)

    plugin_type = body.get("type")
    if plugin_type not in VALID_TYPES:
        raise RegistryError(
            f"{registry_path}: '{plugin_id}' has type={plugin_type!r}, "
            f"expected one of {sorted(VALID_TYPES)}"
        )

    npm_package = body.get("npm_package")
    source_dir = body.get("source_dir")

    if plugin_type == "npm":
        if not npm_package:
            raise RegistryError(
                f"{registry_path}: '{plugin_id}' has type=npm but no npm_package"
            )
    elif plugin_type == "bundled-source":
        if source_dir is None:
            source_dir = f"openclaw-eval/plugins/{plugin_id}"
    elif plugin_type == "base-extension":
        if npm_package or source_dir:
            raise RegistryError(
                f"{registry_path}: '{plugin_id}' is base-extension; "
                f"npm_package/source_dir not allowed"
            )

    return PluginEntry(
        id=plugin_id,
        kinds=kinds,
        type=plugin_type,
        npm_package=npm_package,
        source_dir=source_dir,
    )


def _normalize_kinds(
    plugin_id: str,
    kind_field: object,
    registry_path: Path,
) -> frozenset[str]:
    if isinstance(kind_field, str):
        kinds: list[str] = [kind_field]
    elif isinstance(kind_field, list):
        kinds = list(kind_field)
    else:
        raise RegistryError(
            f"{registry_path}: '{plugin_id}' has kind={kind_field!r}, "
            f"expected string or list of strings"
        )
    if not kinds:
        raise RegistryError(
            f"{registry_path}: '{plugin_id}' has empty kind list"
        )
    bad = [k for k in kinds if k not in VALID_KINDS]
    if bad:
        raise RegistryError(
            f"{registry_path}: '{plugin_id}' has unknown kind(s): {bad}, "
            f"valid: {sorted(VALID_KINDS)}"
        )
    return frozenset(kinds)


# NOTE: registry exposes a plain dict + functional get/by_kind helpers
# rather than a Registry class. Reasons:
#   - dict[str, PluginEntry] is enough for PR1-5's read-only use cases
#   - promoting to a class is a 3-call-site refactor if/when we need to
#     attach path/schema_version/validators
# Reviewers: see codex review of commit 7617071 "Design Risks #3".


def get(registry: dict[str, PluginEntry], plugin_id: str) -> PluginEntry:
    if plugin_id not in registry:
        raise RegistryError(
            f"unknown plugin: '{plugin_id}'. add it to plugin_registry.yaml "
            f"or pass --plugin-spec for an ad-hoc override"
        )
    return registry[plugin_id]


def by_kind(registry: dict[str, PluginEntry], kind: str) -> list[PluginEntry]:
    return [e for e in registry.values() if e.supports_kind(kind)]
