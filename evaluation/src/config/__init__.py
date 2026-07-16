"""Strict evaluation configuration registries."""

from evaluation.src.config.system_index import (
    DEFAULT_SYSTEM_INDEX_PATH,
    SystemIndex,
    SystemIndexEntry,
    SystemIndexError,
    get_system_entry,
    load_system_index,
    resolve_alias,
)

__all__ = [
    "DEFAULT_SYSTEM_INDEX_PATH",
    "SystemIndex",
    "SystemIndexEntry",
    "SystemIndexError",
    "get_system_entry",
    "load_system_index",
    "resolve_alias",
]
