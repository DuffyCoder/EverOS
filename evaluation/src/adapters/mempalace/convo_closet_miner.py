#!/usr/bin/env python3
"""Conversation mining wrapper that adds source-faithful closet rebuilds."""

import hashlib
from datetime import datetime
from pathlib import Path

from .convo_miner import (
    MIN_CHUNK_SIZE,
    chunk_exchanges,
    detect_convo_room,
    mine_convos,
    scan_convos,
)
from .normalize import normalize
from .palace import (
    NORMALIZE_VERSION,
    build_closet_lines,
    file_already_mined,
    get_closets_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)


def rebuild_convo_closets(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
    """Rebuild closets for conversation-mined sources using source closet logic."""
    convo_path = Path(convo_dir).expanduser().resolve()
    if not wing:
        wing = convo_path.name.lower().replace(" ", "_").replace("-", "_")

    files = scan_convos(convo_dir)
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Closet Rebuild — Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — closets will not be written")
    print(f"{'-' * 55}\n")

    if extract_mode == "general":
        raise NotImplementedError(
            "The internal evaluation MemPalace package supports extract_mode='exchange' only."
        )

    closets_col = get_closets_collection(palace_path, create=not dry_run) if not dry_run else None
    total_closets = 0
    files_skipped = 0

    for i, filepath in enumerate(files, 1):
        source_file = str(filepath)
        if not dry_run and file_already_mined(closets_col, source_file):
            files_skipped += 1
            continue

        try:
            content = normalize(str(filepath))
        except (OSError, ValueError):
            continue

        if not content or len(content.strip()) < MIN_CHUNK_SIZE:
            continue

        chunks = chunk_exchanges(content)
        if not chunks:
            continue

        room = detect_convo_room(content)
        drawer_ids = [
            f"drawer_{wing}_{room}_"
            f"{hashlib.sha256((source_file + str(chunk['chunk_index'])).encode()).hexdigest()[:24]}"
            for chunk in chunks
        ]
        closet_lines = build_closet_lines(source_file, drawer_ids, content, wing, room)
        if not closet_lines:
            continue

        if dry_run:
            print(
                f"    [DRY RUN] {filepath.name} → room:{room} ({len(closet_lines)} closet lines)"
            )
            total_closets += len(closet_lines)
            continue

        with mine_lock(source_file):
            if file_already_mined(closets_col, source_file):
                files_skipped += 1
                continue

            closet_id_base = (
                f"closet_{wing}_{room}_{hashlib.sha256(source_file.encode()).hexdigest()[:24]}"
            )
            closet_meta = {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "drawer_count": len(drawer_ids),
                "filed_at": datetime.now().isoformat(),
                "normalize_version": NORMALIZE_VERSION,
            }
            purge_file_closets(closets_col, source_file)
            closets_written = upsert_closet_lines(
                closets_col, closet_id_base, closet_lines, closet_meta
            )
            total_closets += closets_written
            print(
                f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{closets_written} closets"
            )

    print(f"\n{'=' * 55}")
    print("  Closet rebuild done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (closets already filed): {files_skipped}")
    print(f"  Closets filed: {total_closets}")
    print(f"{'=' * 55}\n")


def mine_convos_with_closets(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
    """Mine conversations via convo_miner, then rebuild closets in a second pass."""
    mine_convos(
        convo_dir=convo_dir,
        palace_path=palace_path,
        wing=wing,
        agent=agent,
        limit=limit,
        dry_run=dry_run,
        extract_mode=extract_mode,
    )
    rebuild_convo_closets(
        convo_dir=convo_dir,
        palace_path=palace_path,
        wing=wing,
        limit=limit,
        dry_run=dry_run,
        extract_mode=extract_mode,
    )
