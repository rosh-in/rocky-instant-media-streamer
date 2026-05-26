"""
rocky/vector_store.py — ChromaDB semantic search layer for Rocky.

Embeds movies using Google text-embedding-004 and stores them in a
local ChromaDB collection. Provides semantic_search() which returns
movie dicts from SQLite enriched with similarity scores.

The collection is built once and persisted to disk. Call sync() whenever
new movies are added to the SQLite DB.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from google import genai
from google.genai import types as genai_types

from rocky.db import Database

logger = logging.getLogger("rocky.vector_store")

_EMBED_MODEL = "text-embedding-004"
_COLLECTION_NAME = "rocky_movies"
_CHROMA_PATH = "./data/chroma"
_EMBED_BATCH_SIZE = 50  # text-embedding-004 supports up to 100 per batch


def _movie_to_text(movie: dict) -> str:
    """Convert a movie dict to a single string for embedding.

    Combines the fields that carry semantic meaning. Runtime and
    has_file are excluded — they're structured filters, not
    semantic content.
    """
    parts = [
        movie.get("title", ""),
        str(movie.get("year", "") or ""),
        movie.get("genre", ""),
        movie.get("director", ""),
        movie.get("mood_tags", ""),
        movie.get("tmdb_overview", ""),
    ]
    return " | ".join(p for p in parts if p and str(p).strip())


class VectorStore:
    """ChromaDB-backed semantic search for Rocky's movie catalog.

    Usage:
        store = VectorStore(gemini_api_key="...", db_path=Path("./data/rocky.db"))
        store.sync()  # call once on startup, or when DB changes
        results = store.semantic_search("something heavy and emotional", country_code="IN", limit=10)
    """

    def __init__(
        self,
        gemini_api_key: str,
        db_path: Path,
        chroma_path: str = _CHROMA_PATH,
    ):
        self.db = Database(db_path)
        self._embed_client = genai.Client(vertexai=True, api_key=gemini_api_key)

        # Persistent ChromaDB client — survives restarts
        self._chroma = chromadb.PersistentClient(
            path=chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._chroma.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},  # cosine similarity
        )

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in batches using text-embedding-004."""
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[i : i + _EMBED_BATCH_SIZE]
            response = self._embed_client.models.embed_content(
                model=_EMBED_MODEL,
                contents=batch,
                config=genai_types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",
                ),
            )
            all_embeddings.extend([e.values for e in response.embeddings])
        return all_embeddings

    def _embed_query(self, query: str) -> list[float]:
        """Embed a single search query."""
        response = self._embed_client.models.embed_content(
            model=_EMBED_MODEL,
            contents=[query],
            config=genai_types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
            ),
        )
        return response.embeddings[0].values

    # ------------------------------------------------------------------
    # Sync — build/update the ChromaDB collection from SQLite
    # ------------------------------------------------------------------

    def sync(self) -> int:
        """Sync ChromaDB collection with SQLite movie catalog.

        Only embeds movies not already in the collection (incremental).
        Returns the number of new movies embedded.

        Call this:
        - Once on bot startup
        - Whenever new movies are added to SQLite
        """
        # Get all movies from SQLite
        all_movies = self.db.get_all_movies()
        if not all_movies:
            logger.warning("No movies found in DB to sync")
            return 0

        # Find which tmdb_ids are already in ChromaDB
        existing_ids = set(self._collection.get()["ids"])
        new_movies = [m for m in all_movies if str(m["tmdb_id"]) not in existing_ids]

        if not new_movies:
            logger.info("ChromaDB already up to date (%d movies)", len(existing_ids))
            return 0

        logger.info("Embedding %d new movies...", len(new_movies))

        # Embed in batches
        texts = [_movie_to_text(m) for m in new_movies]
        embeddings = self._embed_texts(texts)

        # Upsert into ChromaDB
        # metadatas stores structured fields for post-search filtering
        self._collection.upsert(
            ids=[str(m["tmdb_id"]) for m in new_movies],
            embeddings=embeddings,
            documents=texts,
            metadatas=[
                {
                    "tmdb_id": m["tmdb_id"],
                    "title": m.get("title", ""),
                    "year": m.get("year") or 0,
                    "genre": m.get("genre", ""),
                    "director": m.get("director", ""),
                    "mood_tags": m.get("mood_tags", ""),
                    "runtime": m.get("runtime") or 0,
                    "has_file": int(m.get("has_file", 0)),
                    "origin_country": m.get("origin_country", ""),
                }
                for m in new_movies
            ],
        )

        logger.info("Synced %d new movies into ChromaDB", len(new_movies))
        return len(new_movies)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def semantic_search(
        self,
        query: str,
        country_code: str,
        limit: int = 10,
        exclude_ids: Optional[list[int]] = None,
        min_similarity: float = 0.25,
    ) -> list[dict]:
        """Semantic search over the movie catalog.

        Returns movie dicts from SQLite (full data) sorted by similarity.
        Filters out excluded IDs and low-similarity results.

        Args:
            query: Natural language query e.g. "something heavy and emotional"
            country_code: For OTT platform filtering (passed to SQLite fetch)
            limit: Max results to return
            exclude_ids: tmdb_ids to exclude (already shown movies)
            min_similarity: Cosine similarity threshold (0-1). Results below
                           this are discarded as semantically irrelevant.
        """
        if self._collection.count() == 0:
            logger.warning("ChromaDB collection is empty — run sync() first")
            return []

        query_embedding = self._embed_query(query)

        # Fetch more than limit so we have room to filter
        fetch_limit = min((limit * 3) + len(exclude_ids or []), 50)

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=fetch_limit,
            include=["metadatas", "distances"],
        )

        if not results["ids"][0]:
            return []

        # ChromaDB returns distances (lower = more similar for cosine).
        # Convert to similarity score: similarity = 1 - distance
        candidates = []
        for chroma_id, metadata, distance in zip(
            results["ids"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            similarity = 1.0 - distance
            if similarity < min_similarity:
                continue
            tmdb_id = int(chroma_id)
            if exclude_ids and tmdb_id in exclude_ids:
                continue
            candidates.append((tmdb_id, similarity))

        if not candidates:
            return []

        # Fetch full movie data from SQLite (has OTT, jellyfin status etc.)
        # SQLite is the source of truth — ChromaDB only stores search data
        tmdb_ids = [c[0] for c in candidates[:limit]]
        movies = self.db.get_movies_by_ids(tmdb_ids, country_code=country_code)

        # Re-sort by similarity score (SQLite fetch loses ordering)
        id_to_score = {c[0]: c[1] for c in candidates}
        movies.sort(key=lambda m: id_to_score.get(m["tmdb_id"], 0), reverse=True)

        return movies[:limit]

    def count(self) -> int:
        """Return number of movies currently in the collection."""
        return self._collection.count()
