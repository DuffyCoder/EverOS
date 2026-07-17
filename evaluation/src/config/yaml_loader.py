"""Shared strict and safe YAML parsing for evaluation configuration."""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe loader that rejects merge keys and duplicate mapping keys."""


def _construct_strict_mapping(
    loader: _StrictSafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    for key_node, _ in node.value:
        if key_node.tag == _YAML_MERGE_TAG:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "YAML merge key '<<' is not allowed",
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


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_strict_mapping
)


def strict_safe_load(text: str) -> Any:
    """Parse YAML safely while rejecting duplicate keys and merge directives."""
    return yaml.load(text, Loader=_StrictSafeLoader)
