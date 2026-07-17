from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from system_config_legacy import (  # noqa: E402
    legacy_system_ids,
    normalized_effective_config,
    normalized_raw_config,
    semantic_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ID_OVERRIDES = {"hermes": "hermes-holographic", "openclaw-hybrid": "openclaw"}


def build_baseline(repo_root: Path) -> dict[str, dict[str, Any]]:
    systems_dir = repo_root / "evaluation" / "config" / "systems"

    baseline: dict[str, dict[str, Any]] = {}
    for system_id in sorted(legacy_system_ids(repo_root)):
        path = systems_dir / f"{system_id}.yaml"
        raw_config = normalized_raw_config(path)
        effective_config = normalized_effective_config(system_id, raw_config)
        baseline[system_id] = {
            "adapter": raw_config["adapter"],
            "canonical_id": CANONICAL_ID_OVERRIDES.get(system_id, system_id),
            "default_result_suffix": f"locomo-{system_id}",
            "effective_config": effective_config,
            "effective_sha256": semantic_sha256(effective_config),
            "raw_config": raw_config,
            "raw_sha256": semantic_sha256(raw_config),
        }
    return baseline


def write_baseline(
    output: Path, *, repo_root: Path = REPO_ROOT, force: bool = False
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        build_baseline(repo_root),
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(f"{serialized}\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        if force:
            os.replace(temporary_path, output)
        else:
            try:
                os.link(temporary_path, output)
            except FileExistsError as error:
                raise FileExistsError(f"output already exists: {output}") from error
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture the immutable legacy system-config baseline."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        write_baseline(args.output, force=args.force)
    except FileExistsError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
