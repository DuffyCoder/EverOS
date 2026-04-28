"""Shared palace operations used by the internal evaluation MemPalace package."""

import contextlib
import hashlib
import os
import re

from .backends.chroma import ChromaBackend

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    ".eggs",
    "htmlcov",
    "target",
}

NORMALIZE_VERSION = 2
_DEFAULT_BACKEND = ChromaBackend()

CLOSET_CHAR_LIMIT = 1500
CLOSET_EXTRACT_WINDOW = 5000

_ENTITY_STOPLIST = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "When",
        "Where",
        "What",
        "Why",
        "Who",
        "Which",
        "How",
        "After",
        "Before",
        "Then",
        "Now",
        "Here",
        "There",
        "And",
        "But",
        "Or",
        "Yet",
        "So",
        "If",
        "Else",
        "Yes",
        "No",
        "Maybe",
        "Okay",
        "User",
        "Assistant",
        "System",
        "Tool",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    }
)


def get_collection(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    create: bool = True,
):
    return _DEFAULT_BACKEND.get_collection(
        palace_path,
        collection_name=collection_name,
        create=create,
    )


def get_closets_collection(palace_path: str, create: bool = True):
    return get_collection(palace_path, collection_name="mempalace_closets", create=create)


def get_room_summaries_collection(palace_path: str, create: bool = True):
    return get_collection(
        palace_path,
        collection_name="mempalace_room_summaries",
        create=create,
    )


def _candidate_entity_words(text: str) -> list[str]:
    """Extract simple proper-noun candidates without extra i18n dependencies."""
    return re.findall(r"\b[A-Z][A-Za-z][A-Za-z'_-]*\b", text)


def build_closet_lines(
    source_file: str,
    drawer_ids: list[str],
    content: str,
    wing: str,
    room: str,
) -> list[str]:
    """Build compact closet pointer lines from a source's content."""
    from pathlib import Path

    drawer_ref = ",".join(drawer_ids[:3])
    window = content[:CLOSET_EXTRACT_WINDOW]

    words = _candidate_entity_words(window)
    word_freq: dict[str, int] = {}
    for word in words:
        if word in _ENTITY_STOPLIST:
            continue
        word_freq[word] = word_freq.get(word, 0) + 1
    entities = sorted(
        [word for word, count in word_freq.items() if count >= 2],
        key=lambda word: -word_freq[word],
    )[:5]
    entity_str = ";".join(entities) if entities else ""

    topics = []
    for pattern in [
        r"(?:built|fixed|wrote|added|pushed|tested|created|decided|migrated|reviewed|deployed|configured|removed|updated)\s+[\w\s]{3,40}",
    ]:
        topics.extend(re.findall(pattern, window, re.IGNORECASE))
    for header in re.findall(r"^#{1,3}\s+(.{5,60})$", window, re.MULTILINE):
        topics.append(header.strip())
    topics = list(dict.fromkeys(topic.strip().lower() for topic in topics if topic.strip()))[
        :12
    ]

    quotes = re.findall(r'"([^"]{15,150})"', window)

    lines = []
    for topic in topics:
        lines.append(f"{topic}|{entity_str}|→{drawer_ref}")
    for quote in quotes[:3]:
        lines.append(f'"{quote}"|{entity_str}|→{drawer_ref}')

    if not lines:
        name = Path(source_file).stem[:40]
        lines.append(f"{wing}/{room}/{name}|{entity_str}|→{drawer_ref}")

    return lines


def purge_file_closets(closets_col, source_file: str) -> None:
    try:
        closets_col.delete(where={"source_file": source_file})
    except Exception:
        pass


def upsert_closet_lines(closets_col, closet_id_base: str, lines: list[str], metadata: dict) -> int:
    closet_num = 1
    current_lines: list[str] = []
    current_chars = 0
    closets_written = 0

    def _flush() -> None:
        nonlocal closets_written
        if not current_lines:
            return
        closet_id = f"{closet_id_base}_{closet_num:02d}"
        text = "\n".join(current_lines)
        closets_col.upsert(documents=[text], ids=[closet_id], metadatas=[metadata])
        closets_written += 1

    for line in lines:
        line_len = len(line)
        if current_chars > 0 and current_chars + line_len + 1 > CLOSET_CHAR_LIMIT:
            _flush()
            closet_num += 1
            current_lines = []
            current_chars = 0
        current_lines.append(line)
        current_chars += line_len + 1

    _flush()
    return closets_written


@contextlib.contextmanager
def mine_lock(source_file: str):
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(
        lock_dir, hashlib.sha256(source_file.encode()).hexdigest()[:16] + ".lock"
    )

    lf = open(lock_path, "w")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        lf.close()


def file_already_mined(collection, source_file: str, check_mtime: bool = False) -> bool:
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        if not results.get("ids"):
            return False
        stored_meta = results.get("metadatas", [{}])[0] or {}
        stored_version = stored_meta.get("normalize_version", 1)
        if stored_version < NORMALIZE_VERSION:
            return False
        if check_mtime:
            stored_mtime = stored_meta.get("source_mtime")
            if stored_mtime is None:
                return False
            current_mtime = os.path.getmtime(source_file)
            return abs(float(stored_mtime) - current_mtime) < 0.001
        return True
    except Exception:
        return False
