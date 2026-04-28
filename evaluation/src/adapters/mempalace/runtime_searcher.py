#!/usr/bin/env python3
"""Runtime searcher for the evaluation MemPalace adapter.

This module keeps the adapter-facing retrieval logic small and explicit:

- `raw`: vector top-k, optional closet boost, optional BM25 rerank
- `hybrid`: benchmark-style keyword/predicate v5 rerank
- `palace`: room-summary routing, then benchmark-style v5 rerank
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path

from .palace import (
    get_closets_collection,
    get_collection,
    get_room_summaries_collection,
)

logger = logging.getLogger("mempalace_mcp")
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)

STOP_WORDS = {
    "what",
    "when",
    "where",
    "who",
    "how",
    "which",
    "did",
    "do",
    "was",
    "were",
    "have",
    "has",
    "had",
    "is",
    "are",
    "the",
    "a",
    "an",
    "my",
    "me",
    "i",
    "you",
    "your",
    "their",
    "it",
    "its",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "ago",
    "last",
    "that",
    "this",
    "there",
    "about",
    "get",
    "got",
    "give",
    "gave",
    "buy",
    "bought",
    "made",
    "make",
    "said",
}

NOT_NAMES = {
    "What",
    "When",
    "Where",
    "Who",
    "How",
    "Which",
    "Why",
    "The",
    "This",
    "That",
    "These",
    "Those",
    "There",
    "Here",
    "Someone",
    "Anybody",
    "Anything",
    "Question",
    "Answer",
    "Speaker",
    "Person",
    "Time",
    "Date",
    "Year",
    "Day",
}


def _first_or_empty(results, key: str) -> list:
    outer = getattr(results, key, None) if not isinstance(results, dict) else results.get(key)
    if not outer:
        return []
    return outer[0] or []


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _kw(text: str) -> list[str]:
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [word for word in words if word not in STOP_WORDS]


def _kw_overlap(query_kws: list[str], doc_text: str) -> float:
    if not query_kws:
        return 0.0
    doc_lower = doc_text.lower()
    hits = sum(1 for kw in query_kws if kw in doc_lower)
    return hits / len(query_kws)


def _quoted_phrases(text: str) -> list[str]:
    phrases = []
    for pattern in [r"'([^']{3,60})'", r'"([^"]{3,60})"']:
        phrases.extend(re.findall(pattern, text))
    return [phrase.strip() for phrase in phrases if len(phrase.strip()) >= 3]


def _quoted_boost(phrases: list[str], doc_text: str) -> float:
    if not phrases:
        return 0.0
    doc_lower = doc_text.lower()
    hits = sum(1 for phrase in phrases if phrase.lower() in doc_lower)
    return min(hits / len(phrases), 1.0)


def _person_names(text: str) -> list[str]:
    words = re.findall(r"\b[A-Z][a-z]{2,15}\b", text)
    return list({word for word in words if word not in NOT_NAMES})


def _bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    n_docs = len(documents)
    query_terms = set(_tokenize(query))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(document) for document in documents]
    doc_lens = [len(tokens) for tokens in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    df = {term: 0 for term in query_terms}
    for tokens in tokenized:
        for term in set(tokens) & query_terms:
            df[term] += 1

    idf = {
        term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1)
        for term in query_terms
    }

    scores = []
    for tokens, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf = {}
        for token in tokens:
            if token in query_terms:
                tf[token] = tf.get(token, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def _bm25_rerank(results: list[dict], query: str) -> list[dict]:
    if not results:
        return results
    docs = [result.get("text", "") for result in results]
    bm25_raw = _bm25_scores(query, docs)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [score / max_bm25 for score in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for result, raw, norm in zip(results, bm25_raw, bm25_norm):
        vec_sim = max(0.0, 1.0 - result.get("distance", 1.0))
        result["bm25_score"] = round(raw, 3)
        scored.append((0.6 * vec_sim + 0.4 * norm, result))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [result for _, result in scored]


def build_where_filter(wing: str = None, room=None, source_files=None) -> dict:
    clauses = []
    if wing:
        clauses.append({"wing": wing})
    if room:
        if isinstance(room, (list, tuple, set)):
            rooms = [value for value in room if value]
            if len(rooms) == 1:
                clauses.append({"room": rooms[0]})
            elif rooms:
                clauses.append({"room": {"$in": rooms}})
        else:
            clauses.append({"room": room})
    if source_files:
        files = [value for value in source_files if value]
        if len(files) == 1:
            clauses.append({"source_file": files[0]})
        elif files:
            clauses.append({"source_file": {"$in": files}})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _query_collection(collection, query: str, n_results: int, where: dict | None = None):
    kwargs = {
        "query_texts": [query],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where
    return collection.query(**kwargs)


def _collect_closet_boosts(query: str, closets_col, where: dict | None, n_results: int) -> dict:
    closet_boost_by_source = {}
    closet_results = _query_collection(closets_col, query, n_results=max(n_results * 2, n_results), where=where)
    for rank, (doc, meta, dist) in enumerate(
        zip(
            _first_or_empty(closet_results, "documents"),
            _first_or_empty(closet_results, "metadatas"),
            _first_or_empty(closet_results, "distances"),
        )
    ):
        meta = meta or {}
        source = meta.get("source_file", "")
        if source and source not in closet_boost_by_source:
            closet_boost_by_source[source] = (rank, dist, doc[:200])
    return closet_boost_by_source


def _load_room_summary_entries(room_summaries_col, wing: str | None) -> list[dict]:
    where = build_where_filter(wing=wing)
    kwargs = {"include": ["documents", "metadatas"]}
    if where:
        kwargs["where"] = where
    results = room_summaries_col.get(**kwargs)
    entries = []
    for document, metadata in zip(results.documents, results.metadatas):
        metadata = metadata or {}
        room_name = metadata.get("room")
        if room_name and document:
            entries.append(
                {
                    "room": room_name,
                    "summary": document,
                    "source_file": metadata.get("source_file", ""),
                    "session_id": metadata.get("session_id", ""),
                }
            )
    return entries


def _select_palace_target_sources(
    room_summaries_col,
    wing: str | None,
    predicate_kws: list[str],
    room_top_k: int,
) -> tuple[list[str], list[str]]:
    entries = _load_room_summary_entries(room_summaries_col, wing=wing)
    if not entries:
        return [], []

    room_chunks: dict[str, list[str]] = {}
    room_sources: dict[str, list[str]] = {}
    for entry in entries:
        room_name = entry["room"]
        room_chunks.setdefault(room_name, []).append(entry["summary"])
        source_file = entry.get("source_file") or ""
        if source_file:
            room_sources.setdefault(room_name, []).append(source_file)

    room_scores = []
    for room_name, summaries in room_chunks.items():
        room_text = " ".join(summaries)
        overlap = _kw_overlap(predicate_kws, room_text) if predicate_kws else 0.0
        room_scores.append((overlap, room_name))
    room_scores.sort(reverse=True)

    if not room_scores:
        return [], []
    if room_scores[0][0] == 0.0:
        target_rooms = [room_name for _, room_name in room_scores]
    else:
        target_rooms = [room_name for _, room_name in room_scores[: max(1, room_top_k)]]

    target_sources = []
    seen_sources = set()
    for room_name in target_rooms:
        for source_file in room_sources.get(room_name, []):
            if source_file and source_file not in seen_sources:
                seen_sources.add(source_file)
                target_sources.append(source_file)
    return target_rooms, target_sources


def _build_hit(doc: str, meta: dict, dist: float, sort_key: float, matched_via: str, source: str) -> dict:
    return {
        "text": doc,
        "wing": meta.get("wing", "unknown"),
        "room": meta.get("room", "unknown"),
        "source_file": Path(source).name if source else "?",
        "created_at": meta.get("filed_at", "unknown"),
        "similarity": round(max(0.0, 1 - sort_key), 3),
        "distance": round(dist, 4),
        "effective_distance": round(sort_key, 4),
        "matched_via": matched_via,
        "_sort_key": sort_key,
        "_source_file_full": source,
        "_chunk_index": meta.get("chunk_index"),
    }


def _apply_v5_rerank(query: str, hits: list[dict]) -> list[dict]:
    names = _person_names(query)
    name_words = {name.lower() for name in names}
    all_kws = _kw(query)
    predicate_kws = [kw for kw in all_kws if kw not in name_words]
    quoted = _quoted_phrases(query)

    reranked = []
    for hit in hits:
        doc = hit["text"]
        fused_dist = hit["_sort_key"]
        predicate_overlap = _kw_overlap(predicate_kws, doc)
        fused_dist = fused_dist * (1.0 - 0.50 * predicate_overlap)

        quoted_match = _quoted_boost(quoted, doc)
        if quoted_match > 0:
            fused_dist = fused_dist * (1.0 - 0.60 * quoted_match)

        name_match = 0.0
        if names:
            doc_lower = doc.lower()
            name_hits = sum(1 for name in names if name.lower() in doc_lower)
            name_match = min(name_hits / len(names), 1.0)
            if name_match > 0:
                fused_dist = fused_dist * (1.0 - 0.20 * name_match)

        hit["predicate_overlap"] = round(predicate_overlap, 3)
        hit["quoted_boost"] = round(quoted_match, 3)
        hit["name_boost"] = round(name_match, 3)
        hit["pre_v5_effective_distance"] = round(hit["_sort_key"], 4)
        hit["_sort_key"] = fused_dist
        hit["effective_distance"] = round(fused_dist, 4)
        hit["similarity"] = round(max(0.0, 1 - fused_dist), 3)
        reranked.append(hit)

    reranked.sort(key=lambda entry: entry["_sort_key"])
    return reranked


def _hydrate_closet_hits(drawers_col, hits: list[dict], query: str) -> None:
    max_hydration_chars = 10000
    query_terms = set(_tokenize(query))

    for hit in hits:
        if hit.get("matched_via") != "drawer+closet":
            continue
        full_source = hit.get("_source_file_full") or ""
        if not full_source:
            continue
        try:
            source_drawers = drawers_col.get(
                where={"source_file": full_source},
                include=["documents", "metadatas"],
            )
        except Exception:
            continue

        docs = source_drawers.documents
        metas = source_drawers.metadatas
        if len(docs) <= 1:
            continue

        indexed = []
        for idx, (doc, meta) in enumerate(zip(docs, metas)):
            chunk_index = meta.get("chunk_index", idx) if isinstance(meta, dict) else idx
            if not isinstance(chunk_index, int):
                chunk_index = idx
            indexed.append((chunk_index, doc))
        indexed.sort(key=lambda pair: pair[0])
        ordered_docs = [doc for _, doc in indexed]

        best_idx, best_score = 0, -1
        for idx, doc in enumerate(ordered_docs):
            score = sum(1 for term in query_terms if term in doc.lower())
            if score > best_score:
                best_score, best_idx = score, idx

        start = max(0, best_idx - 1)
        end = min(len(ordered_docs), best_idx + 2)
        expanded = "\n\n".join(ordered_docs[start:end])
        if len(expanded) > max_hydration_chars:
            expanded = (
                expanded[:max_hydration_chars]
                + f"\n\n[...truncated. {len(ordered_docs)} total drawers. "
                "Use mempalace_get_drawer for full content.]"
            )
        hit["text"] = expanded
        hit["drawer_index"] = best_idx
        hit["total_drawers"] = len(ordered_docs)


def search_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    search_options: dict | None = None,
) -> dict:
    options = search_options or {}
    retrieval_mode = str(options.get("retrieval_mode", "raw") or "raw").strip().lower()
    if retrieval_mode not in {"raw", "hybrid", "palace"}:
        return {"error": f"Unsupported retrieval_mode: {retrieval_mode}"}

    use_bm25_rerank = bool(options.get("use_bm25_rerank", False))
    use_closet_boost = bool(options.get("use_closet_boost", False)) if retrieval_mode == "raw" else False
    max_distance = float(options.get("max_distance", 0.0) or 0.0)
    palace_room_top_k = int(options.get("palace_room_top_k", 3) or 3)

    try:
        drawers_col = get_collection(palace_path, create=False)
    except Exception as exc:
        logger.error("No palace found at %s: %s", palace_path, exc)
        return {
            "error": "No palace found",
            "hint": "Run add stage before retrieval",
        }

    target_rooms = []
    target_sources = []
    retrieval_route = retrieval_mode
    where = build_where_filter(wing, room)

    if retrieval_mode == "palace" and not room:
        try:
            room_summaries_col = get_room_summaries_collection(palace_path, create=False)
            names = _person_names(query)
            name_words = {name.lower() for name in names}
            predicate_kws = [kw for kw in _kw(query) if kw not in name_words]
            target_rooms, target_sources = _select_palace_target_sources(
                room_summaries_col,
                wing=wing,
                predicate_kws=predicate_kws,
                room_top_k=palace_room_top_k,
            )
            if target_sources:
                where = build_where_filter(wing=wing, source_files=target_sources)
                retrieval_route = "palace_session_subset_route"
            elif target_rooms:
                retrieval_route = "palace_room_summary_without_sources"
            else:
                retrieval_route = "palace_no_room_summary_signal"
        except Exception:
            retrieval_route = "palace_room_summary_missing"

    fetch_k = n_results if retrieval_mode == "raw" and not (use_bm25_rerank or use_closet_boost) else max(
        n_results * 3,
        n_results,
    )

    try:
        drawer_results = _query_collection(drawers_col, query, n_results=fetch_k, where=where)
    except Exception as exc:
        return {"error": f"Search error: {exc}"}

    closet_boost_by_source = {}
    if use_closet_boost:
        try:
            closets_col = get_closets_collection(palace_path, create=False)
            closet_boost_by_source = _collect_closet_boosts(query, closets_col, where, n_results)
        except Exception:
            closet_boost_by_source = {}

    closet_rank_boosts = [0.40, 0.25, 0.15, 0.08, 0.04]
    closet_distance_cap = 1.5

    scored = []
    for doc, meta, dist in zip(
        _first_or_empty(drawer_results, "documents"),
        _first_or_empty(drawer_results, "metadatas"),
        _first_or_empty(drawer_results, "distances"),
    ):
        if max_distance > 0.0 and dist > max_distance:
            continue

        meta = meta or {}
        source = meta.get("source_file", "") or ""
        matched_via = "drawer"
        sort_key = dist

        if use_closet_boost and source in closet_boost_by_source:
            closet_rank, closet_dist, closet_preview = closet_boost_by_source[source]
            if closet_dist <= closet_distance_cap and closet_rank < len(closet_rank_boosts):
                closet_boost = closet_rank_boosts[closet_rank]
                sort_key = dist - closet_boost
                matched_via = "drawer+closet"
            else:
                closet_preview = None
                closet_boost = 0.0
        else:
            closet_preview = None
            closet_boost = 0.0

        hit = _build_hit(doc, meta, dist, sort_key, matched_via, source)
        hit["closet_boost"] = round(closet_boost, 3)
        if closet_preview:
            hit["closet_preview"] = closet_preview
        scored.append(hit)

    scored.sort(key=lambda entry: entry["_sort_key"])
    if retrieval_mode in {"hybrid", "palace"}:
        scored = _apply_v5_rerank(query, scored)

    hits = scored[:n_results]
    if use_closet_boost:
        _hydrate_closet_hits(drawers_col, hits, query)
    if retrieval_mode == "raw" and use_bm25_rerank:
        hits = _bm25_rerank(hits, query)

    for hit in hits:
        hit.pop("_sort_key", None)
        hit.pop("_source_file_full", None)
        hit.pop("_chunk_index", None)

    backend_mode = [retrieval_mode]
    if retrieval_mode == "raw" and use_closet_boost:
        backend_mode.append("closet")
    if retrieval_mode in {"hybrid", "palace"}:
        backend_mode.append("v5")
    if retrieval_mode == "raw" and use_bm25_rerank:
        backend_mode.append("bm25")

    return {
        "query": query,
        "filters": {"wing": wing, "room": room},
        "total_before_filter": len(_first_or_empty(drawer_results, "documents")),
        "retrieval_route": retrieval_route,
        "backend_mode": "+".join(backend_mode),
        "target_rooms": target_rooms,
        "target_sources": [Path(source).name for source in target_sources],
        "results": hits,
    }
