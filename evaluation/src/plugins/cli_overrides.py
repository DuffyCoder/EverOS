"""CLI plugin override + image resolution for evaluation.cli.

evaluation.cli accepts the same plugin grammar as build.py:
``--memory-plugin <id>[@v]|none`` and ``--context-engine <id>[@v]|none``.
This module applies those overrides to the loaded yaml ``system_config``
and resolves the docker image tag from ``image_manifest.yaml``.

Precedence for ``openclaw_docker.image``:

1. ``--image <tag>``                   (explicit override; bypasses manifest)
2. CLI plugin override + manifest     (resolved when --memory-plugin or
                                       --context-engine is passed)
3. yaml's existing ``openclaw_docker.image`` field
                                      (preserved when no CLI override)

If manifest lookup fails and ``--build-missing`` is set, this module
shells out to ``build.py`` with the same plugin selection, then
re-resolves.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from evaluation.src.plugins.manifest import (
    DEFAULT_MANIFEST_PATH,
    ManifestError,
    find_image,
    load_manifest,
)
from evaluation.src.plugins.registry import (
    DEFAULT_REGISTRY_PATH,
    load_registry,
)
from evaluation.src.plugins.resolver import (
    PluginRef,
    ResolverError,
    parse_ref,
)


@dataclass(frozen=True)
class PluginOverrideResult:
    """Outcome of ``apply_plugin_overrides`` for caller logging / tests."""

    image_resolved: Optional[str]
    memory_mode_applied: Optional[str]
    context_engine_mode_applied: Optional[str]
    triggered_build: bool


def apply_plugin_overrides(
    system_config: dict[str, Any],
    *,
    memory_plugin: Optional[str],
    context_engine: Optional[str],
    image: Optional[str],
    build_missing: bool,
    registry_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> PluginOverrideResult:
    """Apply CLI plugin overrides to ``system_config``.

    **This function mutates ``system_config`` in place** and also returns
    a summary of what was applied. Callers needing the original yaml
    intact should ``copy.deepcopy(system_config)`` before invoking this.

    See module docstring for precedence rules. None values for
    ``memory_plugin`` / ``context_engine`` / ``image`` mean the CLI flag
    was not passed; the yaml is preserved for that field.
    """
    if registry_path is None:
        registry_path = DEFAULT_REGISTRY_PATH
    if manifest_path is None:
        manifest_path = DEFAULT_MANIFEST_PATH

    registry = load_registry(registry_path)

    memory_ref: Optional[PluginRef] = None
    ce_ref: Optional[PluginRef] = None
    try:
        if memory_plugin is not None:
            memory_ref = parse_ref(
                memory_plugin, expected_kind="memory", registry=registry,
            )
        if context_engine is not None:
            ce_ref = parse_ref(
                context_engine, expected_kind="context-engine", registry=registry,
            )
    except ResolverError as e:
        sys.exit(f"[eval] ERROR: {e}")

    memory_mode_applied: Optional[str] = None
    if memory_plugin is not None:
        oc = system_config.setdefault("openclaw", {})
        memory_mode_applied = "noop" if memory_ref is None else memory_ref.id
        oc["memory_mode"] = memory_mode_applied

    context_engine_mode_applied: Optional[str] = None
    if context_engine is not None:
        oc = system_config.setdefault("openclaw", {})
        if ce_ref is None:
            # Explicit "none" -> unset; entrypoint.sh falls back to legacy CE.
            oc.pop("context_engine_mode", None)
            context_engine_mode_applied = ""
        else:
            context_engine_mode_applied = ce_ref.id
            oc["context_engine_mode"] = ce_ref.id

    triggered_build = False
    image_resolved: Optional[str] = None

    if image:
        # Highest precedence: explicit --image bypasses manifest.
        system_config.setdefault("openclaw_docker", {})["image"] = image
        image_resolved = image
    elif memory_plugin is not None or context_engine is not None:
        # Resolve image based on the overridden plugin selection.
        memory_constraint = _ref_to_constraint(
            memory_ref, plugin_override_passed=memory_plugin is not None,
        )
        ce_constraint = _ref_to_constraint(
            ce_ref, plugin_override_passed=context_engine is not None,
        )

        # Both constraints None means the overrides only changed slot wiring
        # (e.g. --memory-plugin none -> noop), not which plugins are baked
        # into the image. Trust the yaml's image rather than asking the
        # manifest to disambiguate among unrelated tags.
        if memory_constraint is None and ce_constraint is None:
            return PluginOverrideResult(
                image_resolved=None,
                memory_mode_applied=memory_mode_applied,
                context_engine_mode_applied=context_engine_mode_applied,
                triggered_build=False,
            )

        manifest = load_manifest(manifest_path)
        try:
            entry = find_image(
                manifest,
                memory_plugin=memory_constraint,
                context_engine=ce_constraint,
            )
        except ManifestError as e:
            if not build_missing:
                sys.exit(f"[eval] ERROR: image lookup failed: {e}")
            triggered_build = True
            _invoke_build(memory_plugin, context_engine, manifest_path)
            manifest = load_manifest(manifest_path)
            try:
                entry = find_image(
                    manifest,
                    memory_plugin=memory_constraint,
                    context_engine=ce_constraint,
                )
            except ManifestError as e2:
                sys.exit(
                    f"[eval] ERROR: build.py reported success but manifest "
                    f"still lacks the image: {e2}"
                )
        system_config.setdefault("openclaw_docker", {})["image"] = entry.image
        image_resolved = entry.image

    return PluginOverrideResult(
        image_resolved=image_resolved,
        memory_mode_applied=memory_mode_applied,
        context_engine_mode_applied=context_engine_mode_applied,
        triggered_build=triggered_build,
    )


def _ref_to_constraint(
    ref: Optional[PluginRef],
    *,
    plugin_override_passed: bool,
) -> Optional[tuple[str, Optional[str]]]:
    """Map a parsed PluginRef to a manifest find_image constraint.

    - ref=None (CLI passed "none" or flag absent): no constraint
    - ref.entry.type=base-extension (memory-core / noop): no constraint
      (these are baked into every image)
    - else: (id, version) — version=None matches any built version
    """
    if not plugin_override_passed:
        return None
    if ref is None:
        return None
    if ref.entry.type == "base-extension":
        return None
    return (ref.id, ref.version)


def _invoke_build(
    memory_plugin: Optional[str],
    context_engine: Optional[str],
    manifest_path: Path,
) -> None:
    """Shell out to build.py with the same plugin selection. Aborts on failure."""
    build_py = (
        Path(__file__).resolve().parents[3]
        / "openclaw-eval" / "harness" / "build.py"
    )
    cmd = [sys.executable, str(build_py)]
    if memory_plugin and memory_plugin.lower() not in ("none", ""):
        cmd.extend(["--memory-plugin", memory_plugin])
    if context_engine and context_engine.lower() not in ("none", ""):
        cmd.extend(["--context-engine", context_engine])
    cmd.extend(["--image-manifest-out", str(manifest_path)])
    print(f"[eval] image not in manifest; auto-building: "
          f"{' '.join(cmd[len([sys.executable, str(build_py)]):])}")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        sys.exit("[eval] build.py failed; see output above for details")
