"""Resolve plugin id+version arguments into concrete actions.

The CLI accepts ``<id>``, ``<id>@<version>``, or ``none`` for
``--memory-plugin`` / ``--context-engine``. This module parses those and
validates kind. The result drives:
  - build.py: an npm spec (when type=npm) or a "stage source dir" instruction
  - eval.cli: an image lookup against image_manifest.yaml

Version handling:
  - bundled plugins (base-extension / bundled-source) reject ``@version``
  - npm plugins require ``@version`` (no implicit "latest" — keeps eval
    runs reproducible)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from evaluation.src.plugins.registry import (
    PluginEntry,
    RegistryError,
    get as registry_get,
)


NONE_TOKENS = {"", "none"}

Kind = Literal["memory", "context-engine"]


class ResolverError(ValueError):
    """Raised when a CLI plugin argument is malformed or fails validation."""


@dataclass(frozen=True)
class PluginRef:
    """A resolved reference to a registry entry + optional version."""
    entry: PluginEntry
    version: str | None

    @property
    def id(self) -> str:
        return self.entry.id

    @property
    def is_npm(self) -> bool:
        return self.entry.type == "npm"

    def npm_spec(self, override: str | None = None) -> str:
        """Return the npm spec usable in Dockerfile.eval ``npm pack``.

        ``override`` short-circuits the registry-derived spec (used for
        ``--plugin-spec``). Bundled plugins raise.
        """
        if not self.is_npm:
            raise ResolverError(
                f"{self.id}: not an npm plugin; npm_spec applies only to type=npm"
            )
        if override is not None:
            return override
        if not self.version:
            raise ResolverError(
                f"{self.id}: npm plugin requires explicit version"
            )
        return f"npm:{self.entry.npm_package}@{self.version}"


def parse_ref(
    arg: str | None,
    *,
    expected_kind: Kind,
    registry: dict[str, PluginEntry],
) -> PluginRef | None:
    """Parse a CLI arg like ``evermemos`` / ``hypercompositor@0.9.6`` / ``none``.

    Returns:
        ``None`` if the arg is ``None``, empty, or "none" (case-insensitive).
        Otherwise a ``PluginRef`` with kind validated against ``expected_kind``.

    Raises:
        ResolverError: arg malformed, plugin unknown, kind mismatch, or
            version-handling rule violated.
    """
    if arg is None:
        return None
    s = arg.strip()
    if s.lower() in NONE_TOKENS:
        return None

    if "@" in s:
        plugin_id, _, version = s.partition("@")
        plugin_id = plugin_id.strip()
        version = version.strip()
        if not plugin_id or not version:
            raise ResolverError(
                f"malformed plugin arg {arg!r}; "
                f"expected <id> or <id>@<version>"
            )
    else:
        plugin_id, version = s, None

    try:
        entry = registry_get(registry, plugin_id)
    except RegistryError as e:
        raise ResolverError(str(e)) from e

    if not entry.supports_kind(expected_kind):
        flag = "--memory-plugin" if expected_kind == "memory" else "--context-engine"
        raise ResolverError(
            f"{plugin_id}: kind={sorted(entry.kinds)} does not include "
            f"{expected_kind!r}; can't pass it as {flag}"
        )

    if entry.type == "npm" and version is None:
        raise ResolverError(
            f"{plugin_id}: npm plugin requires explicit version "
            f"(use {plugin_id}@<version>)"
        )
    if entry.type != "npm" and version is not None:
        raise ResolverError(
            f"{plugin_id}: bundled plugin does not take @version "
            f"(version='{version}' provided)"
        )

    return PluginRef(entry=entry, version=version)


def parse_plugin_spec_overrides(pairs: list[str]) -> dict[str, str]:
    """Parse ``--plugin-spec id=spec`` arguments into a dict.

    The spec value may itself contain ``=`` (e.g. URLs with query strings),
    so we split on the first ``=`` only.
    """
    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise ResolverError(
                f"--plugin-spec requires id=spec, got {raw!r}"
            )
        plugin_id, _, spec = raw.partition("=")
        plugin_id = plugin_id.strip()
        spec = spec.strip()
        if not plugin_id or not spec:
            raise ResolverError(f"--plugin-spec malformed: {raw!r}")
        if plugin_id in out:
            raise ResolverError(
                f"--plugin-spec specified twice for {plugin_id!r}"
            )
        out[plugin_id] = spec
    return out
