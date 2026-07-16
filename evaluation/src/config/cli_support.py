"""CLI-only system configuration resolution and mutation helpers."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Any, NoReturn

from evaluation.src.config.system_index import (
    DEFAULT_SYSTEM_INDEX_PATH,
    SystemIndexError,
    load_system_index,
)
from evaluation.src.config.system_loader import (
    ResolvedSystemConfig,
    SystemConfigError,
    deep_merge_config,
    resolve_system_config,
)
from evaluation.src.config.system_policy import (
    PolicyFinding,
    SystemPolicyError,
    validate_runtime_system_policy,
)
from evaluation.src.config.system_schema import (
    SystemSchemaError,
    validate_system_config,
)
from evaluation.src.plugins.cli_overrides import (
    PluginOverrideResult,
    apply_plugin_overrides,
)
from evaluation.src.plugins.registry import (
    DEFAULT_REGISTRY_PATH,
    RegistryError,
    load_registry,
)
from evaluation.src.plugins.resolver import ResolverError, parse_ref


@dataclass(frozen=True)
class CLIPluginOverrideOutcome:
    """Result and user-facing warnings from adapter-gated plugin flags."""

    override: PluginOverrideResult | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PreparedSystemConfig:
    """Validated runtime configuration before transient context injection."""

    config: dict[str, Any]
    runtime_context: dict[str, Any]
    plugin_override: PluginOverrideResult | None
    warnings: tuple[str, ...]
    runtime_policy_findings: tuple[PolicyFinding, ...]

    def adapter_config(self) -> dict[str, Any]:
        """Return an adapter-only copy with transient runtime context."""
        config = deepcopy(self.config)
        config.update(self.runtime_context)
        return config


def resolve_system_for_cli(
    system_id: str,
    *,
    index_path: Path = DEFAULT_SYSTEM_INDEX_PATH,
    systems_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedSystemConfig:
    """Resolve a public id in compatibility mode or exit like argparse."""
    try:
        index = load_system_index(Path(index_path))
    except SystemIndexError as exc:
        _exit_two(f"invalid system registry: {exc}")

    if system_id not in index.systems:
        matches = get_close_matches(system_id, sorted(index.systems), n=5, cutoff=0.45)
        suffix = f" Close matches: {', '.join(matches)}." if matches else ""
        _exit_two(f"unknown system id {system_id!r}.{suffix}")

    try:
        return resolve_system_config(
            system_id,
            index_path=Path(index_path),
            systems_root=systems_root,
            environ=environ,
            # The source tree still contains the seven audited legacy policy
            # exceptions. Later migration tasks remove them; until then the
            # CLI keeps every locked public id runnable and makes findings
            # visible as warnings.
            allow_legacy=True,
        )
    except (SystemConfigError, SystemIndexError) as exc:
        _exit_two(f"invalid system configuration for {system_id!r}: {exc}")


def default_result_dir(
    evaluation_root: Path,
    *,
    dataset_id: str,
    requested_system_id: str,
    run_name: str | None,
) -> Path:
    """Preserve the historical requested-id result-directory convention."""
    suffix = f"{dataset_id}-{requested_system_id}"
    if run_name:
        suffix = f"{suffix}-{run_name}"
    return Path(evaluation_root) / "results" / suffix


def system_cli_warnings(resolution: ResolvedSystemConfig) -> tuple[str, ...]:
    """Return deterministic deprecation, experiment, and legacy warnings."""
    warnings: list[str] = []
    if resolution.warning:
        warnings.append(resolution.warning)
    if resolution.status == "experimental":
        warnings.append(f"system {resolution.requested_id!r} is experimental")
    for finding in resolution.policy_findings:
        warnings.append(
            f"legacy system-config policy finding "
            f"{finding.pointer or '<root>'} [{finding.code}]: {finding.message}"
        )
    return tuple(warnings)


def apply_plugin_overrides_for_cli(
    resolution: ResolvedSystemConfig,
    config: dict[str, Any],
    *,
    memory_plugin: str | None,
    context_engine: str | None,
    image: str | None,
    build_missing: bool,
) -> CLIPluginOverrideOutcome:
    """Apply plugin flags only to OpenClaw Docker configurations."""
    any_flag = any(
        (
            memory_plugin is not None,
            context_engine is not None,
            image is not None,
            build_missing,
        )
    )
    if resolution.adapter != "openclaw-docker":
        warnings = (
            (
                (
                    "plugin/image override flags are ignored for adapter "
                    f"{resolution.adapter!r}"
                ),
            )
            if any_flag
            else ()
        )
        return CLIPluginOverrideOutcome(override=None, warnings=warnings)

    # Validate both selectors before the mutating override helper runs. This
    # preserves the caller's config on unknown or wrong-kind plugin ids.
    try:
        registry = load_registry(DEFAULT_REGISTRY_PATH)
        if memory_plugin is not None:
            parse_ref(memory_plugin, expected_kind="memory", registry=registry)
        if context_engine is not None:
            parse_ref(context_engine, expected_kind="context-engine", registry=registry)
    except (RegistryError, ResolverError) as exc:
        _exit_two(f"invalid plugin override: {exc}")

    try:
        override = apply_plugin_overrides(
            config,
            memory_plugin=memory_plugin,
            context_engine=context_engine,
            image=image,
            build_missing=build_missing,
        )
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        _exit_two(str(exc.code))
    return CLIPluginOverrideOutcome(override=override, warnings=())


def prepare_system_config_for_cli(
    resolution: ResolvedSystemConfig,
    *,
    dataset_id: str,
    clean_groups: bool,
    memory_plugin: str | None,
    context_engine: str | None,
    image: str | None,
    build_missing: bool,
) -> PreparedSystemConfig:
    """Apply runtime-only patches in the required order and revalidate."""
    config = deepcopy(resolution.config)
    overrides = config.get("dataset_overrides")
    if isinstance(overrides, dict):
        selected = overrides.get(dataset_id)
        if isinstance(selected, dict):
            config = deep_merge_config(config, selected)
    # Source schema validates the full patch map. The selected patch becomes
    # ordinary runtime config and the map itself must never reach adapters.
    config.pop("dataset_overrides", None)

    plugin_outcome = apply_plugin_overrides_for_cli(
        resolution,
        config,
        memory_plugin=memory_plugin,
        context_engine=context_engine,
        image=image,
        build_missing=build_missing,
    )

    try:
        validate_system_config(resolution.adapter, config)
        runtime_findings = validate_runtime_system_policy(
            resolution.adapter,
            config,
            canonical_id=resolution.canonical_id,
            allow_legacy=True,
        )
    except SystemSchemaError as exc:
        _exit_two(f"final system configuration violates schema: {exc}")
    except SystemPolicyError as exc:
        _exit_two(f"final system configuration violates runtime policy: {exc}")

    runtime_context = {"dataset_name": dataset_id, "clean_groups": bool(clean_groups)}
    return PreparedSystemConfig(
        config=config,
        runtime_context=runtime_context,
        plugin_override=plugin_outcome.override,
        warnings=plugin_outcome.warnings,
        runtime_policy_findings=runtime_findings,
    )


def _exit_two(message: str) -> NoReturn:
    print(f"evaluation: error: {message}", file=sys.stderr)
    raise SystemExit(2)
