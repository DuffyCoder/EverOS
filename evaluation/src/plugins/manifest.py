"""Image manifest read/write.

``evaluation/config/image_manifest.yaml`` records what plugins each
pre-built docker image contains. ``build.py`` appends a new entry per
build; ``evaluation.cli`` queries by ``(memory_plugin, context_engine)``
to pick a runnable image.

Schema (one entry per image)::

    - image: openclaw-eval:<sha>-<bundle>-<rev>-slim
      openclaw_sha: <git short sha>
      built_at: <iso utc>
      plugins:
        <id>:
          kind: memory | context-engine
          version: bundled | <semver>
          rev: <hash>           # optional; bundled-source content hash
          source: bundled | bundled-source | npm:<package>
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


DEFAULT_MANIFEST_PATH = "evaluation/config/image_manifest.yaml"


class ManifestError(ValueError):
    """Raised when the manifest is malformed or a query fails."""


@dataclass(frozen=True)
class ManifestPlugin:
    id: str
    kind: str
    version: str
    rev: str | None
    source: str


@dataclass(frozen=True)
class ManifestEntry:
    image: str
    openclaw_sha: str
    built_at: str
    plugins: dict[str, ManifestPlugin]

    def has_plugin(self, plugin_id: str, version: str | None = None) -> bool:
        if plugin_id not in self.plugins:
            return False
        if version is None:
            return True
        return self.plugins[plugin_id].version == version


def load_manifest(path: str | Path) -> list[ManifestEntry]:
    """Read manifest. Missing file -> empty list (not an error)."""
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or []
    if not isinstance(raw, list):
        raise ManifestError(
            f"{p} must be a top-level list, got {type(raw).__name__}"
        )
    return [_parse_entry(item, p) for item in raw]


def _parse_entry(item: object, manifest_path: Path) -> ManifestEntry:
    if not isinstance(item, dict):
        raise ManifestError(
            f"{manifest_path}: each entry must be a mapping, "
            f"got {type(item).__name__}"
        )
    image = item.get("image")
    if not image:
        raise ManifestError(f"{manifest_path}: entry missing 'image'")
    plugins_raw = item.get("plugins") or {}
    plugins: dict[str, ManifestPlugin] = {}
    for plugin_id, body in plugins_raw.items():
        body = body or {}
        plugins[plugin_id] = ManifestPlugin(
            id=plugin_id,
            kind=body.get("kind", ""),
            version=body.get("version", "bundled"),
            rev=body.get("rev"),
            source=body.get("source", ""),
        )
    return ManifestEntry(
        image=image,
        openclaw_sha=item.get("openclaw_sha", ""),
        built_at=item.get("built_at", ""),
        plugins=plugins,
    )


def append_entry(path: str | Path, entry: ManifestEntry) -> None:
    """Append ``entry`` to the manifest file, creating it if missing."""
    p = Path(path)
    existing = load_manifest(p)
    existing.append(entry)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_dump(existing))


def _dump(entries: list[ManifestEntry]) -> str:
    serializable = [
        {
            "image": e.image,
            "openclaw_sha": e.openclaw_sha,
            "built_at": e.built_at,
            "plugins": {
                pid: {
                    "kind": p.kind,
                    "version": p.version,
                    **({"rev": p.rev} if p.rev else {}),
                    "source": p.source,
                }
                for pid, p in e.plugins.items()
            },
        }
        for e in entries
    ]
    return yaml.safe_dump(serializable, sort_keys=False)


def now_iso() -> str:
    """UTC ISO-8601 timestamp truncated to seconds, with trailing 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_image(
    entries: list[ManifestEntry],
    *,
    memory_plugin: tuple[str, str | None] | None,
    context_engine: tuple[str, str | None] | None,
) -> ManifestEntry:
    """Find the unique image satisfying both plugin constraints.

    Each constraint is ``(plugin_id, version_or_None)`` or ``None`` to
    skip that constraint. ``version=None`` matches any version of that id.

    Raises:
        ManifestError: zero matches, or more than one match (ambiguous).
    """
    candidates = [
        e for e in entries
        if (memory_plugin is None or e.has_plugin(memory_plugin[0], memory_plugin[1]))
        and (context_engine is None or e.has_plugin(context_engine[0], context_engine[1]))
    ]
    if not candidates:
        raise ManifestError(
            f"no image matches memory={memory_plugin} ce={context_engine}. "
            f"build with: openclaw-eval/harness/build.py "
            f"--memory-plugin {_fmt(memory_plugin)} "
            f"--context-engine {_fmt(context_engine)}"
        )
    if len(candidates) > 1:
        tags = ", ".join(c.image for c in candidates)
        raise ManifestError(
            f"{len(candidates)} images match memory={memory_plugin} "
            f"ce={context_engine}: {tags}. "
            f"pin a version (id@version) to disambiguate."
        )
    return candidates[0]


def _fmt(constraint: tuple[str, str | None] | None) -> str:
    if constraint is None:
        return "none"
    plugin_id, version = constraint
    return f"{plugin_id}@{version}" if version else plugin_id
