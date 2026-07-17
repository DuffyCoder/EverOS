from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ONLINE_ADAPTER_IDS = frozenset({"mem0", "memos", "memu", "zep", "evermemos_api"})


def legacy_system_ids(repo_root: Path) -> set[str]:
    systems_dir = repo_root / "evaluation" / "config" / "systems"
    return {
        path.stem for path in systems_dir.glob("*.yaml") if path.name != "index.yaml"
    }


def normalized_raw_config(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"system configuration must be a mapping: {path}")
    _validate_json_compatible(value)
    return value


def normalized_effective_config(system_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    effective = deepcopy(raw)

    if effective.get("adapter") not in ONLINE_ADAPTER_IDS:
        answer = effective.get("answer")
        if isinstance(answer, dict):
            answer.pop("max_retries", None)

    openclaw = effective.get("openclaw")
    if isinstance(openclaw, dict):
        openclaw.pop("prompts", None)
        embedding = openclaw.get("embedding")
        if (
            isinstance(embedding, dict)
            and embedding.get("api_key") == "${SOPH_API_KEY}"
        ):
            embedding.pop("api_key")
            embedding["api_key_env"] = "SOPH_API_KEY"

    if system_id == "memos":
        effective.pop("request_interval", None)
    elif system_id == "memu":
        effective.pop("min_similarity", None)

    return effective


def semantic_sha256(value: Any) -> str:
    _validate_json_compatible(value)
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def json_pointer_differences(before: Any, after: Any) -> set[str]:
    differences: set[str] = set()
    _collect_json_pointer_differences(before, after, "", differences)
    return differences


def _collect_json_pointer_differences(
    before: Any, after: Any, pointer: str, differences: set[str]
) -> None:
    if type(before) is not type(after):
        differences.add(pointer)
        return

    if isinstance(before, Mapping):
        before_keys = set(before)
        after_keys = set(after)
        for key in before_keys | after_keys:
            child_pointer = _join_json_pointer(pointer, str(key))
            if key not in before or key not in after:
                differences.add(child_pointer)
            else:
                _collect_json_pointer_differences(
                    before[key], after[key], child_pointer, differences
                )
        return

    if isinstance(before, Sequence) and not isinstance(before, (str, bytes, bytearray)):
        common_length = min(len(before), len(after))
        for index in range(common_length):
            _collect_json_pointer_differences(
                before[index],
                after[index],
                _join_json_pointer(pointer, str(index)),
                differences,
            )
        for index in range(common_length, max(len(before), len(after))):
            differences.add(_join_json_pointer(pointer, str(index)))
        return

    if before != after:
        differences.add(pointer)


def _join_json_pointer(pointer: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}"


def _validate_json_compatible(value: Any, pointer: str = "") -> None:
    location = pointer or "<root>"

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"invalid JSON-compatible value at {location}: "
                    f"mapping keys must be strings; got {key!r}"
                )
            _validate_json_compatible(child, _join_json_pointer(pointer, key))
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_compatible(child, _join_json_pointer(pointer, str(index)))
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"invalid JSON-compatible value at {location}: "
                "float values must be finite"
            )
        return

    if value is None or isinstance(value, (str, bool, int)):
        return

    raise ValueError(
        f"invalid JSON-compatible value at {location}: "
        f"unsupported scalar type {type(value).__name__}"
    )
