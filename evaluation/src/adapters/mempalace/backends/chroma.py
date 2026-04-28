"""Minimal ChromaDB backend used by the internal evaluation MemPalace package."""

from dataclasses import dataclass
import os
import threading

import chromadb


@dataclass(frozen=True)
class QueryResult:
    ids: list
    documents: list
    metadatas: list
    distances: list

    def get(self, key, default=None):
        return getattr(self, key, default)


@dataclass(frozen=True)
class GetResult:
    ids: list
    documents: list
    metadatas: list

    def get(self, key, default=None):
        return getattr(self, key, default)


class ChromaCollection:
    def __init__(self, collection):
        self._collection = collection

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        kwargs = {"documents": documents, "ids": ids}
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        self._collection.upsert(**kwargs)

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        kwargs = {"documents": documents, "ids": ids}
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        self._collection.add(**kwargs)

    def query(
        self,
        *,
        query_texts=None,
        query_embeddings=None,
        n_results=10,
        where=None,
        where_document=None,
        include=None,
    ):
        kwargs = {"n_results": n_results, "include": include or ["documents", "metadatas", "distances"]}
        if query_texts is not None:
            kwargs["query_texts"] = query_texts
        if query_embeddings is not None:
            kwargs["query_embeddings"] = query_embeddings
        if where is not None:
            kwargs["where"] = where
        if where_document is not None:
            kwargs["where_document"] = where_document
        raw = self._collection.query(**kwargs)
        return QueryResult(
            ids=raw.get("ids") or [],
            documents=raw.get("documents") or [],
            metadatas=raw.get("metadatas") or [],
            distances=raw.get("distances") or [],
        )

    def get(
        self,
        *,
        ids=None,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        include=None,
    ):
        kwargs = {"include": include or ["documents", "metadatas"]}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        if where_document is not None:
            kwargs["where_document"] = where_document
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        raw = self._collection.get(**kwargs)
        return GetResult(
            ids=raw.get("ids") or [],
            documents=raw.get("documents") or [],
            metadatas=raw.get("metadatas") or [],
        )

    def delete(self, *, ids=None, where=None):
        kwargs = {}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        self._collection.delete(**kwargs)

    def count(self):
        return self._collection.count()


class ChromaBackend:
    def __init__(self):
        self._clients = {}
        self._lock = threading.RLock()

    def _client(self, palace_path: str):
        with self._lock:
            client = self._clients.get(palace_path)
            if client is None:
                client = chromadb.PersistentClient(path=palace_path)
                self._clients[palace_path] = client
            return client

    def get_collection(self, palace_path: str, collection_name: str = "mempalace_drawers", create: bool = True):
        if not create and not os.path.isdir(palace_path):
            raise FileNotFoundError(palace_path)
        if create:
            os.makedirs(palace_path, exist_ok=True)
        client = self._client(palace_path)
        if create:
            collection = client.get_or_create_collection(
                collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        else:
            collection = client.get_collection(collection_name)
        return ChromaCollection(collection)
