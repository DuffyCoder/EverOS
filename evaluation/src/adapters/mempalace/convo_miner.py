#!/usr/bin/env python3
"""Internal conversation miner used by the evaluation MemPalace package."""

import hashlib
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .normalize import normalize
from .palace import (
    NORMALIZE_VERSION,
    SKIP_DIRS,
    file_already_mined,
    get_collection,
    mine_lock,
)

_HALL_KEYWORDS_CACHE = None


def _detect_hall_cached(content: str) -> str:
    global _HALL_KEYWORDS_CACHE
    if _HALL_KEYWORDS_CACHE is None:
        from .config import MempalaceConfig

        _HALL_KEYWORDS_CACHE = MempalaceConfig().hall_keywords
    content_lower = content[:3000].lower()
    scores = {}
    for hall, keywords in _HALL_KEYWORDS_CACHE.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[hall] = score
    return max(scores, key=scores.get) if scores else "general"


CONVO_EXTENSIONS = {".txt", ".md", ".json", ".jsonl"}
MIN_CHUNK_SIZE = 30
CHUNK_SIZE = 800
MAX_FILE_SIZE = 500 * 1024 * 1024


def _register_file(collection, source_file: str, wing: str, agent: str):
    sentinel_id = f"_reg_{hashlib.sha256(source_file.encode()).hexdigest()[:24]}"
    collection.upsert(
        documents=[f"[registry] {source_file}"],
        ids=[sentinel_id],
        metadatas=[
            {
                "wing": wing,
                "room": "_registry",
                "source_file": source_file,
                "added_by": agent,
                "filed_at": datetime.now().isoformat(),
                "ingest_mode": "registry",
                "normalize_version": NORMALIZE_VERSION,
            }
        ],
    )


def chunk_exchanges(content: str) -> list:
    lines = content.split("\n")
    quote_lines = sum(1 for line in lines if line.strip().startswith(">"))
    if quote_lines >= 3:
        return _chunk_by_exchange(lines)
    return _chunk_by_paragraph(content)


def _chunk_by_exchange(lines: list) -> list:
    chunks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(">"):
            user_turn = line.strip()
            i += 1
            ai_lines = []
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith(">") or next_line.strip().startswith("---"):
                    break
                if next_line.strip():
                    ai_lines.append(next_line.strip())
                i += 1
            content = f"{user_turn}\n{' '.join(ai_lines)}" if ai_lines else user_turn
            if len(content) > CHUNK_SIZE:
                first_part = content[:CHUNK_SIZE]
                if len(first_part.strip()) > MIN_CHUNK_SIZE:
                    chunks.append({"content": first_part, "chunk_index": len(chunks)})
                remainder = content[CHUNK_SIZE:]
                while remainder:
                    part = remainder[:CHUNK_SIZE]
                    remainder = remainder[CHUNK_SIZE:]
                    if len(part.strip()) > MIN_CHUNK_SIZE:
                        chunks.append({"content": part, "chunk_index": len(chunks)})
            elif len(content.strip()) > MIN_CHUNK_SIZE:
                chunks.append({"content": content, "chunk_index": len(chunks)})
        else:
            i += 1
    return chunks


def _chunk_by_paragraph(content: str) -> list:
    chunks = []
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1 and content.count("\n") > 20:
        lines = content.split("\n")
        for i in range(0, len(lines), 25):
            group = "\n".join(lines[i : i + 25]).strip()
            if len(group) > MIN_CHUNK_SIZE:
                chunks.append({"content": group, "chunk_index": len(chunks)})
        return chunks
    for para in paragraphs:
        if len(para) > MIN_CHUNK_SIZE:
            chunks.append({"content": para, "chunk_index": len(chunks)})
    return chunks


TOPIC_KEYWORDS = {
    "technical": ["code", "python", "function", "bug", "error", "api", "database", "server", "deploy", "git", "test", "debug", "refactor"],
    "architecture": ["architecture", "design", "pattern", "structure", "schema", "interface", "module", "component", "service", "layer"],
    "planning": ["plan", "roadmap", "milestone", "deadline", "priority", "sprint", "backlog", "scope", "requirement", "spec"],
    "decisions": ["decided", "chose", "picked", "switched", "migrated", "replaced", "trade-off", "alternative", "option", "approach"],
    "problems": ["problem", "issue", "broken", "failed", "crash", "stuck", "workaround", "fix", "solved", "resolved"],
}


def detect_convo_room(content: str) -> str:
    content_lower = content[:3000].lower()
    scores = {}
    for room, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"


def scan_convos(convo_dir: str) -> list:
    convo_path = Path(convo_dir).expanduser().resolve()
    files = []
    for root, dirs, filenames in os.walk(convo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(".meta.json"):
                continue
            filepath = Path(root) / filename
            if filepath.suffix.lower() in CONVO_EXTENSIONS:
                if filepath.is_symlink():
                    continue
                try:
                    if filepath.stat().st_size > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                files.append(filepath)
    return files


def _file_chunks_locked(collection, source_file, chunks, wing, room, agent, extract_mode):
    room_counts_delta = defaultdict(int)
    drawers_added = 0
    with mine_lock(source_file):
        if file_already_mined(collection, source_file):
            return 0, room_counts_delta, True
        try:
            collection.delete(where={"source_file": source_file})
        except Exception:
            pass

        for chunk in chunks:
            chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
            if extract_mode == "general":
                room_counts_delta[chunk_room] += 1
            drawer_id = (
                f"drawer_{wing}_{chunk_room}_"
                f"{hashlib.sha256((source_file + str(chunk['chunk_index'])).encode()).hexdigest()[:24]}"
            )
            collection.upsert(
                documents=[chunk["content"]],
                ids=[drawer_id],
                metadatas=[
                    {
                        "wing": wing,
                        "room": chunk_room,
                        "hall": _detect_hall_cached(chunk["content"]),
                        "source_file": source_file,
                        "chunk_index": chunk["chunk_index"],
                        "added_by": agent,
                        "filed_at": datetime.now().isoformat(),
                        "ingest_mode": "convos",
                        "extract_mode": extract_mode,
                        "normalize_version": NORMALIZE_VERSION,
                    }
                ],
            )
            drawers_added += 1

    return drawers_added, room_counts_delta, False


def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
    convo_path = Path(convo_dir).expanduser().resolve()
    if not wing:
        wing = convo_path.name.lower().replace(" ", "_").replace("-", "_")

    files = scan_convos(convo_dir)
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'-' * 55}\n")

    collection = get_collection(palace_path) if not dry_run else None
    total_drawers = 0
    files_skipped = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        source_file = str(filepath)
        if not dry_run and file_already_mined(collection, source_file):
            files_skipped += 1
            continue

        try:
            content = normalize(str(filepath))
        except (OSError, ValueError):
            if not dry_run:
                _register_file(collection, source_file, wing, agent)
            continue

        if not content or len(content.strip()) < MIN_CHUNK_SIZE:
            if not dry_run:
                _register_file(collection, source_file, wing, agent)
            continue

        if extract_mode == "general":
            raise NotImplementedError(
                "The internal evaluation MemPalace package supports extract_mode='exchange' only."
            )
        chunks = chunk_exchanges(content)

        if not chunks:
            if not dry_run:
                _register_file(collection, source_file, wing, agent)
            continue

        room = detect_convo_room(content)

        if dry_run:
            print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(chunks)} drawers)")
            total_drawers += len(chunks)
            room_counts[room] += 1
            continue

        room_counts[room] += 1
        drawers_added, room_delta, skipped = _file_chunks_locked(
            collection, source_file, chunks, wing, room, agent, extract_mode
        )
        if skipped:
            files_skipped += 1
            continue
        for r, n in room_delta.items():
            room_counts[r] += n
        total_drawers += drawers_added
        print(f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")
