import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Mood tags derived from genres
# Indian OTT provider allowlist — only these platforms are shown in recommendations
INDIAN_OTT_ALLOWLIST = {
    "Netflix",
    "Prime Video",
    "Hotstar",
    "SonyLIV",
    "Airtel Xstream",
}

MOOD_MAP = {
    "Drama": ["emotional", "heavy", "character-driven"],
    "Comedy": ["light", "fun", "easy watch"],
    "Thriller": ["tense", "gripping", "edge of seat"],
    "Horror": ["scary", "disturbing", "dark"],
    "Romance": ["warm", "tender", "feel-good"],
    "Animation": ["family", "light", "fun"],
    "History": ["slow burn", "weighty", "thought-provoking"],
    "Mystery": ["gripping", "cerebral", "twisty"],
    "Action": ["exciting", "fast-paced", "adrenaline"],
    "Science Fiction": ["mind-bending", "futuristic", "cerebral"],
    "Fantasy": ["escapist", "imaginative", "epic"],
    "Documentary": ["informative", "real", "thought-provoking"],
    "War": ["heavy", "intense", "weighty"],
    "Crime": ["gritty", "tense", "cerebral"],
    "Adventure": ["exciting", "escapist", "fun"],
}


def derive_mood_tags(genres: str) -> str:
    """Derive mood tags from a comma/slash-separated genre string."""
    if not genres:
        return ""
    tags = []
    # Handle both comma and slash separated genres
    for part in genres.replace("/", ",").split(","):
        genre = part.strip()
        tags.extend(MOOD_MAP.get(genre, []))
    return ",".join(sorted(set(tags)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WatchlistMovie:
    title: str
    year: Optional[int]
    letterboxd_slug: Optional[str]
    letterboxd_url: str


@dataclass(frozen=True)
class TmdbMovie:
    tmdb_id: int
    title: str
    original_title: str
    release_year: Optional[int]
    overview: str
    popularity: float


@dataclass(frozen=True)
class AvailabilityOffer:
    provider_name: str
    provider_code: Optional[str]
    monetization_type: str
    url: Optional[str]


def _ott_for_movie(movie_id: int, country_code: str, conn: sqlite3.Connection) -> str:
    """Centralized helper: filter raw JustWatch availability to the Indian OTT allowlist.

    Returns a comma-separated string of provider names from the allowlist only.
    If country_code is not 'IN', returns all providers (no filtering).
    """
    ott_rows = conn.execute(
        """
        SELECT DISTINCT provider_name
        FROM availability
        WHERE movie_id = ? AND country_code = ?
        """,
        (movie_id, country_code.upper()),
    ).fetchall()
    all_providers = [r["provider_name"] for r in ott_rows]
    if country_code.upper() != "IN":
        return ", ".join(all_providers)
    # Filter to Indian OTT allowlist
    filtered = [p for p in all_providers if p in INDIAN_OTT_ALLOWLIST]
    return ", ".join(filtered)


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS movies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    letterboxd_slug TEXT UNIQUE,
                    letterboxd_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    tmdb_id INTEGER UNIQUE,
                    tmdb_title TEXT,
                    tmdb_original_title TEXT,
                    tmdb_release_year INTEGER,
                    tmdb_overview TEXT,
                    tmdb_popularity REAL,
                    poster_url TEXT,
                    genre TEXT,
                    runtime INTEGER,
                    availability_last_checked_at TEXT,
                    requested_in_radarr INTEGER NOT NULL DEFAULT 0,
                    requested_in_radarr_at TEXT,
                    radarr_movie_id INTEGER,
                    first_seen_watchlist_at TEXT NOT NULL,
                    last_seen_watchlist_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    items_seen INTEGER NOT NULL DEFAULT 0,
                    items_enriched INTEGER NOT NULL DEFAULT 0,
                    items_availability_refreshed INTEGER NOT NULL DEFAULT 0,
                    items_requested INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS availability (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    movie_id INTEGER NOT NULL,
                    country_code TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    provider_code TEXT,
                    monetization_type TEXT NOT NULL,
                    url TEXT,
                    retrieved_at TEXT NOT NULL,
                    FOREIGN KEY(movie_id) REFERENCES movies(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS watch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tmdb_id INTEGER,
                    title TEXT,
                    watched_at TEXT DEFAULT NULL,
                    reaction TEXT,
                    reaction_emoji TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_availability_unique_offer
                ON availability (movie_id, country_code, provider_name, monetization_type, COALESCE(url, ''))
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_availability_movie
                ON availability (movie_id, country_code)
                """
            )
            self._ensure_column(conn, "movies", "availability_last_checked_at", "TEXT")
            self._ensure_column(conn, "movies", "requested_in_radarr", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "movies", "requested_in_radarr_at", "TEXT")
            self._ensure_column(conn, "movies", "radarr_movie_id", "INTEGER")
            self._ensure_column(conn, "movies", "poster_url", "TEXT")
            self._ensure_column(conn, "movies", "genre", "TEXT")
            self._ensure_column(conn, "movies", "runtime", "INTEGER")
            self._ensure_column(conn, "movies", "trailer_key", "TEXT")
            # Rich metadata columns for chat mode
            self._ensure_column(conn, "movies", "keywords", "TEXT")
            self._ensure_column(conn, "movies", "vote_average", "REAL")
            self._ensure_column(conn, "movies", "director", "TEXT")
            self._ensure_column(conn, "movies", "cast_top3", "TEXT")
            self._ensure_column(conn, "movies", "mood_tags", "TEXT")
            self._ensure_column(conn, "movies", "collection", "TEXT")
            self._ensure_column(conn, "movies", "origin_country", "TEXT")
            self._ensure_column(conn, "movies", "has_file", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(
                conn, "sync_runs", "items_availability_refreshed", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "sync_runs", "items_requested", "INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column_name: str,
        column_def: str,
    ) -> None:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in columns}
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}")

    def start_sync_run(self) -> int:
        started_at = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sync_runs (started_at, status)
                VALUES (?, ?)
                """,
                (started_at, "running"),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        status: str,
        items_seen: int,
        items_enriched: int,
        items_availability_refreshed: int,
        items_requested: int,
        error_message: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?,
                    status = ?,
                    items_seen = ?,
                    items_enriched = ?,
                    items_availability_refreshed = ?,
                    items_requested = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    items_seen,
                    items_enriched,
                    items_availability_refreshed,
                    items_requested,
                    error_message,
                    run_id,
                ),
            )
            conn.commit()

    def upsert_movie(self, movie: WatchlistMovie, tmdb: Optional[TmdbMovie]) -> int:
        now = utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM movies
                WHERE letterboxd_slug = ?
                   OR (letterboxd_slug IS NULL AND title = ? AND COALESCE(year, -1) = COALESCE(?, -1))
                LIMIT 1
                """,
                (movie.letterboxd_slug, movie.title, movie.year),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE movies
                    SET letterboxd_url = ?,
                        title = ?,
                        year = ?,
                        tmdb_id = COALESCE(?, tmdb_id),
                        tmdb_title = COALESCE(?, tmdb_title),
                        tmdb_original_title = COALESCE(?, tmdb_original_title),
                        tmdb_release_year = COALESCE(?, tmdb_release_year),
                        tmdb_overview = COALESCE(?, tmdb_overview),
                        tmdb_popularity = COALESCE(?, tmdb_popularity),
                        last_seen_watchlist_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        movie.letterboxd_url,
                        movie.title,
                        movie.year,
                        tmdb.tmdb_id if tmdb else None,
                        tmdb.title if tmdb else None,
                        tmdb.original_title if tmdb else None,
                        tmdb.release_year if tmdb else None,
                        tmdb.overview if tmdb else None,
                        tmdb.popularity if tmdb else None,
                        now,
                        now,
                        existing["id"],
                    ),
                )
                movie_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO movies (
                        letterboxd_slug,
                        letterboxd_url,
                        title,
                        year,
                        tmdb_id,
                        tmdb_title,
                        tmdb_original_title,
                        tmdb_release_year,
                        tmdb_overview,
                        tmdb_popularity,
                        requested_in_radarr,
                        requested_in_radarr_at,
                        radarr_movie_id,
                        first_seen_watchlist_at,
                        last_seen_watchlist_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        movie.letterboxd_slug,
                        movie.letterboxd_url,
                        movie.title,
                        movie.year,
                        tmdb.tmdb_id if tmdb else None,
                        tmdb.title if tmdb else None,
                        tmdb.original_title if tmdb else None,
                        tmdb.release_year if tmdb else None,
                        tmdb.overview if tmdb else None,
                        tmdb.popularity if tmdb else None,
                        0,
                        None,
                        None,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                movie_id = int(cursor.lastrowid)
            conn.commit()
            return movie_id

    def list_unrequested_movies(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, title, year, tmdb_id
                FROM movies
                WHERE tmdb_id IS NOT NULL
                  AND requested_in_radarr = 0
                ORDER BY first_seen_watchlist_at ASC
                """
            ).fetchall()

    def mark_requested_in_radarr(self, movie_id: int, radarr_movie_id: Optional[int]) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE movies
                SET requested_in_radarr = 1,
                    requested_in_radarr_at = ?,
                    radarr_movie_id = COALESCE(?, radarr_movie_id),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, radarr_movie_id, now, movie_id),
            )
            conn.commit()

    def list_movies_for_availability_refresh(self, stale_before_iso: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, title, year
                FROM movies
                WHERE tmdb_id IS NOT NULL
                  AND (
                        availability_last_checked_at IS NULL
                        OR availability_last_checked_at < ?
                  )
                ORDER BY first_seen_watchlist_at ASC
                """,
                (stale_before_iso,),
            ).fetchall()

    def replace_movie_availability(
        self,
        movie_id: int,
        country_code: str,
        offers: list[AvailabilityOffer],
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM availability
                WHERE movie_id = ?
                  AND country_code = ?
                """,
                (movie_id, country_code.upper()),
            )
            for offer in offers:
                conn.execute(
                    """
                    INSERT INTO availability (
                        movie_id,
                        country_code,
                        provider_name,
                        provider_code,
                        monetization_type,
                        url,
                        retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        movie_id,
                        country_code.upper(),
                        offer.provider_name,
                        offer.provider_code,
                        offer.monetization_type,
                        offer.url,
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE movies
                SET availability_last_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, movie_id),
            )
            conn.commit()

    def update_trailer_key(self, movie_id: int, trailer_key: Optional[str]) -> None:
        """Store the YouTube trailer key for a movie."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE movies
                SET trailer_key = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (trailer_key, now, movie_id),
            )
            conn.commit()

    def update_movie_details(
        self,
        movie_id: int,
        poster_url: Optional[str],
        genre: Optional[str],
        runtime: Optional[int],
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE movies
                SET poster_url = COALESCE(?, poster_url),
                    genre = COALESCE(?, genre),
                    runtime = COALESCE(?, runtime),
                    updated_at = ?
                WHERE id = ?
                """,
                (poster_url, genre, runtime, now, movie_id),
            )
            conn.commit()

    def sync_has_file_from_radarr(self, file_status: dict[int, bool]) -> int:
        """Update has_file for all tracked movies based on Radarr's hasFile status.

        Args:
            file_status: Mapping of tmdbId -> hasFile from Radarr.

        Returns:
            Number of movies updated.
        """
        if not file_status:
            return 0
        now = utc_now()
        updated = 0
        with self._connect() as conn:
            for tmdb_id, has_file in file_status.items():
                cursor = conn.execute(
                    """
                    UPDATE movies
                    SET has_file = ?, updated_at = ?
                    WHERE tmdb_id = ?
                    """,
                    (1 if has_file else 0, now, tmdb_id),
                )
                if cursor.rowcount > 0:
                    updated += cursor.rowcount
            conn.commit()
        return updated

    def get_movie_by_tmdb_id(self, tmdb_id: int, country_code: str = "IN") -> Optional[dict]:
        """Return a movie dict with all fields needed for recommendations, or None."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id = ?
                """,
                (tmdb_id,),
            ).fetchone()
            if not row:
                return None

            # Check Jellyfin availability (has_file means Radarr downloaded & imported)
            in_jellyfin = bool(row["has_file"])
            ott_platforms = _ott_for_movie(row["id"], country_code, conn)

            return {
                "id": row["id"],
                "title": row["title"],
                "year": row["year"],
                "tmdb_id": row["tmdb_id"],
                "poster_url": row["poster_url"],
                "genre": row["genre"],
                "runtime": row["runtime"],
                "overview": row["tmdb_overview"],
                "in_jellyfin": in_jellyfin,
                "ott_platforms": ott_platforms,
                "trailer_key": row["trailer_key"],
                "vote_average": row["vote_average"],
                "director": row["director"],
                "cast_top3": row["cast_top3"],
            }

    def get_short_movies(
        self,
        country_code: str = "IN",
        max_runtime: int = 90,
        exclude_ids: Optional[list[int]] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return movies with runtime <= max_runtime, optionally excluding tmdb_ids."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            query = """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND m.runtime IS NOT NULL
                  AND m.runtime <= ?
            """
            params: list = [max_runtime]
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY m.has_file DESC, m.tmdb_popularity DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

            results: list[dict] = []
            for row in rows:
                in_jellyfin = bool(row["has_file"])
                ott_platforms = _ott_for_movie(row["id"], country_code, conn)
                results.append({
                    "id": row["id"],
                    "title": row["title"],
                    "year": row["year"],
                    "tmdb_id": row["tmdb_id"],
                    "poster_url": row["poster_url"],
                    "genre": row["genre"],
                    "runtime": row["runtime"],
                    "overview": row["tmdb_overview"],
                    "in_jellyfin": in_jellyfin,
                    "ott_platforms": ott_platforms,
                    "trailer_key": row["trailer_key"],
                    "vote_average": row["vote_average"],
                    "director": row["director"],
                    "cast_top3": row["cast_top3"],
                })
            return results

    def get_random_movies(
        self,
        country_code: str = "IN",
        exclude_ids: Optional[list[int]] = None,
        limit: int = 3,
    ) -> list[dict]:
        """Return random movies from the library, optionally excluding tmdb_ids."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            query = """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
            """
            params: list = []
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY RANDOM() LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

            results: list[dict] = []
            for row in rows:
                in_jellyfin = bool(row["has_file"])
                ott_platforms = _ott_for_movie(row["id"], country_code, conn)
                results.append({
                    "id": row["id"],
                    "title": row["title"],
                    "year": row["year"],
                    "tmdb_id": row["tmdb_id"],
                    "poster_url": row["poster_url"],
                    "genre": row["genre"],
                    "runtime": row["runtime"],
                    "overview": row["tmdb_overview"],
                    "in_jellyfin": in_jellyfin,
                    "ott_platforms": ott_platforms,
                    "trailer_key": row["trailer_key"],
                    "vote_average": row["vote_average"],
                    "director": row["director"],
                    "cast_top3": row["cast_top3"],
                })
            return results

    def update_movie_enrichment(
        self,
        movie_id: int,
        keywords: Optional[str] = None,
        vote_average: Optional[float] = None,
        director: Optional[str] = None,
        cast_top3: Optional[str] = None,
        collection: Optional[str] = None,
        origin_country: Optional[str] = None,
        mood_tags: Optional[str] = None,
    ) -> None:
        """Store rich metadata (keywords, credits, mood_tags, etc.) for a movie."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE movies
                SET keywords = COALESCE(?, keywords),
                    vote_average = COALESCE(?, vote_average),
                    director = COALESCE(?, director),
                    cast_top3 = COALESCE(?, cast_top3),
                    mood_tags = COALESCE(?, mood_tags),
                    collection = COALESCE(?, collection),
                    origin_country = COALESCE(?, origin_country),
                    updated_at = ?
                WHERE id = ?
                """,
                (keywords, vote_average, director, cast_top3, mood_tags,
                 collection, origin_country, now, movie_id),
            )
            conn.commit()

    def log_watch_history(
        self,
        tmdb_id: Optional[int],
        title: Optional[str],
        reaction: Optional[str] = None,
        reaction_emoji: Optional[str] = None,
    ) -> None:
        """Log a watch event to the watch_history table."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watch_history (tmdb_id, title, watched_at, reaction, reaction_emoji)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tmdb_id, title, now, reaction, reaction_emoji),
            )
            conn.commit()

    def get_watched_movies(
        self,
        reaction: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Return watched movies, optionally filtered by reaction."""
        with self._connect() as conn:
            if reaction:
                rows = conn.execute(
                    """
                    SELECT wh.tmdb_id, wh.title, wh.reaction, m.genre, m.director,
                           m.origin_country, m.year
                    FROM watch_history wh
                    LEFT JOIN movies m ON wh.tmdb_id = m.tmdb_id
                    WHERE wh.reaction = ?
                    ORDER BY wh.watched_at DESC
                    LIMIT ?
                    """,
                    (reaction, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT wh.tmdb_id, wh.title, wh.reaction, m.genre, m.director,
                           m.origin_country, m.year
                    FROM watch_history wh
                    LEFT JOIN movies m ON wh.tmdb_id = m.tmdb_id
                    ORDER BY wh.watched_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_movies_for_chat_context(
        self,
        country_code: str = "IN",
        limit: int = 50,
    ) -> list[dict]:
        """Return compact movie data for the /chat context block.

        Returns top movies by relevance (Jellyfin-first, then popularity)
        with enriched metadata for Gemini reasoning.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.genre,
                       m.runtime, m.vote_average, m.director, m.mood_tags,
                       m.keywords, m.origin_country, m.collection,
                       m.has_file, m.cast_top3,
                       m.tmdb_title, m.tmdb_original_title
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                ORDER BY m.has_file DESC, m.tmdb_popularity DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            results: list[dict] = []
            for row in rows:
                in_jellyfin = bool(row["has_file"])
                ott_platforms = _ott_for_movie(row["id"], country_code, conn)
                results.append({
                    "tmdb_id": row["tmdb_id"],
                    "title": row["title"],
                    "tmdb_title": row["tmdb_title"],
                    "tmdb_original_title": row["tmdb_original_title"],
                    "year": row["year"],
                    "genre": row["genre"],
                    "runtime": row["runtime"],
                    "vote_average": row["vote_average"],
                    "director": row["director"],
                    "mood_tags": row["mood_tags"],
                    "keywords": row["keywords"],
                    "origin_country": row["origin_country"],
                    "collection": row["collection"],
                    "cast_top3": row["cast_top3"],
                    "in_jellyfin": in_jellyfin,
                    "ott_platforms": ott_platforms,
                })
            return results

    def get_relevant_movies(
        self,
        keywords: str,
        country_code: str = "IN",
        limit: int = 20,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return movies matching keywords for local pre-filtering before Gemini.

        Does simple LIKE matching on title, genre, and overview.
        Prioritises movies in Jellyfin.
        """
        exclude = exclude_ids or []
        with self._connect() as conn:
            # Build keyword conditions for title/genre/overview
            words = keywords.lower().split()
            if not words:
                # No keywords — just return most popular
                query = """
                    SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                           m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                           m.vote_average, m.director
                    FROM movies m
                    WHERE m.tmdb_id IS NOT NULL
                    ORDER BY m.has_file DESC, m.tmdb_popularity DESC
                    LIMIT ?
                """
                rows = conn.execute(query, (limit,)).fetchall()
            else:
                # Build WHERE clause with OR conditions for each word
                conditions = []
                params: list = []
                for w in words:
                    like = f"%{w}%"
                    conditions.append(
                        "(LOWER(m.title) LIKE ? OR LOWER(m.tmdb_title) LIKE ? "
                        "OR LOWER(m.tmdb_original_title) LIKE ? "
                        "OR LOWER(m.genre) LIKE ? OR LOWER(m.tmdb_overview) LIKE ? "
                        "OR LOWER(m.cast_top3) LIKE ?)"
                    )
                    params.extend([like, like, like, like, like, like])

                where_clause = " OR ".join(conditions)
                exclude_clause = ""
                if exclude:
                    placeholders = ",".join("?" for _ in exclude)
                    exclude_clause = f"AND m.tmdb_id NOT IN ({placeholders})"
                    params.extend(exclude)

                query = f"""
                    SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                           m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                           m.vote_average, m.director, m.cast_top3
                    FROM movies m
                    WHERE m.tmdb_id IS NOT NULL
                      AND ({where_clause})
                      {exclude_clause}
                    ORDER BY m.has_file DESC, m.tmdb_popularity DESC
                    LIMIT ?
                """
                params.append(limit)
                rows = conn.execute(query, params).fetchall()

            results: list[dict] = []
            for row in rows:
                in_jellyfin = bool(row["has_file"])
                ott_platforms = _ott_for_movie(row["id"], country_code, conn)

                results.append({
                    "id": row["id"],
                    "title": row["title"],
                    "year": row["year"],
                    "tmdb_id": row["tmdb_id"],
                    "poster_url": row["poster_url"],
                    "genre": row["genre"],
                    "runtime": row["runtime"],
                    "overview": row["tmdb_overview"],
                    "in_jellyfin": in_jellyfin,
                    "ott_platforms": ott_platforms,
                    "trailer_key": row["trailer_key"],
                    "vote_average": row["vote_average"],
                    "director": row["director"],
                    "cast_top3": row["cast_top3"],
                })
            return results

    def _movie_row_to_dict(self, row, country_code: str = "IN") -> dict:
        """Convert a movie query row to a full dict with availability info."""
        with self._connect() as conn:
            in_jellyfin = bool(row["has_file"])
            ott_platforms = _ott_for_movie(row["id"], country_code, conn)
            return {
                "id": row["id"],
                "title": row["title"],
                "year": row["year"],
                "tmdb_id": row["tmdb_id"],
                "poster_url": row["poster_url"],
                "genre": row["genre"],
                "runtime": row["runtime"],
                "overview": row["tmdb_overview"],
                "in_jellyfin": in_jellyfin,
                "ott_platforms": ott_platforms,
                "trailer_key": row["trailer_key"],
                "vote_average": row["vote_average"],
                "director": row["director"],
                "cast_top3": row["cast_top3"],
            }

    def get_movies_by_actor(
        self,
        actor: str,
        country_code: str = "IN",
        limit: int = 20,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return movies featuring an actor (LIKE match on cast_top3)."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            query = """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND LOWER(m.cast_top3) LIKE ?
            """
            params: list = [f"%{actor.lower()}%"]
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY m.has_file DESC, m.tmdb_popularity DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(self._movie_row_to_dict(row, country_code))
        return results

    def get_movies_by_director(
        self,
        director: str,
        country_code: str = "IN",
        limit: int = 20,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return movies by a director name (LIKE match)."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            query = """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND LOWER(m.director) LIKE ?
            """
            params: list = [f"%{director.lower()}%"]
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY m.has_file DESC, m.tmdb_popularity DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(self._movie_row_to_dict(row, country_code))
        return results

    def get_movies_by_genre(
        self,
        genre: str,
        country_code: str = "IN",
        limit: int = 20,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return movies matching a genre (LIKE match on genre column)."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            query = """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND LOWER(m.genre) LIKE ?
            """
            params: list = [f"%{genre.lower()}%"]
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY m.has_file DESC, m.tmdb_popularity DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(self._movie_row_to_dict(row, country_code))
        return results

    def get_movies_by_mood_tags(
        self,
        mood_tags: list[str],
        country_code: str = "IN",
        limit: int = 20,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return movies whose mood_tags contain any of the given tags."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            conditions = []
            params: list = []
            for tag in mood_tags:
                conditions.append("LOWER(m.mood_tags) LIKE ?")
                params.append(f"%{tag.lower()}%")
            where_clause = " OR ".join(conditions)

            query = f"""
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND ({where_clause})
            """
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY m.has_file DESC, m.tmdb_popularity DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(self._movie_row_to_dict(row, country_code))
        return results

    def get_world_cinema_movies(
        self,
        country_code: str = "IN",
        exclude_country: str = "US",
        limit: int = 20,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return movies not from the specified country (default: non-US)."""
        exclude = exclude_ids or []
        with self._connect() as conn:
            query = """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND (m.origin_country IS NULL OR LOWER(m.origin_country) NOT LIKE ?)
            """
            params: list = [f"%{exclude_country.lower()}%"]
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                query += f" AND m.tmdb_id NOT IN ({placeholders})"
                params.extend(exclude)
            query += " ORDER BY m.has_file DESC, m.tmdb_popularity DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(self._movie_row_to_dict(row, country_code))
        return results

    def fuzzy_search_title(
        self,
        query: str,
        country_code: str = "IN",
        limit: int = 5,
    ) -> list[dict]:
        """Fuzzy title search — matches if all words appear in any title field.

        Searches title, tmdb_title, and tmdb_original_title so that movies
        known by alternate names (e.g. "Another Round" vs "Druk") are found.
        Used for direct-play detection and specific-movie intent classification.
        Returns full movie dicts.
        """
        words = query.lower().split()
        if not words:
            return []
        with self._connect() as conn:
            conditions = []
            params: list = []
            for w in words:
                like = f"%{w}%"
                conditions.append(
                    "(LOWER(m.title) LIKE ? OR LOWER(m.tmdb_title) LIKE ? OR LOWER(m.tmdb_original_title) LIKE ?)"
                )
                params.extend([like, like, like])
            where_clause = " AND ".join(conditions)

            sql = f"""
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.tmdb_id IS NOT NULL
                  AND ({where_clause})
                ORDER BY m.has_file DESC, m.tmdb_popularity DESC
                LIMIT ?
            """
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(self._movie_row_to_dict(row, country_code))
        return results

    def get_all_movies(self) -> list[dict]:
        """Return all movies from the catalog for ChromaDB sync.

        Only needs semantic fields — not OTT/jellyfin (ChromaDB doesn't use those).
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT tmdb_id, title, year, genre, director,
                       mood_tags, tmdb_overview, runtime, origin_country,
                       has_file
                FROM movies
                WHERE tmdb_id IS NOT NULL
            """).fetchall()
        return [dict(r) for r in rows]

    def get_movies_by_ids(self, tmdb_ids: list[int], country_code: str = "IN") -> list[dict]:
        """Fetch full movie records by tmdb_id list, with OTT filtering.

        Returns movies in arbitrary order — caller re-sorts by relevance.
        """
        if not tmdb_ids:
            return []
        placeholders = ",".join("?" * len(tmdb_ids))
        with self._connect() as conn:
            rows = conn.execute(f"""
                SELECT m.id, m.title, m.year, m.tmdb_id, m.genre, m.runtime,
                       m.tmdb_overview, m.has_file, m.director, m.mood_tags,
                       m.poster_url, m.trailer_key, m.vote_average
                FROM movies m
                WHERE m.tmdb_id IN ({placeholders})
            """, tmdb_ids).fetchall()

            results: list[dict] = []
            for row in rows:
                in_jellyfin = bool(row["has_file"])
                ott_platforms = _ott_for_movie(row["id"], country_code, conn)
                results.append({
                    "id": row["id"],
                    "title": row["title"],
                    "year": row["year"],
                    "tmdb_id": row["tmdb_id"],
                    "genre": row["genre"],
                    "runtime": row["runtime"],
                    "overview": row["tmdb_overview"],
                    "in_jellyfin": in_jellyfin,
                    "ott_platforms": ott_platforms,
                    "director": row["director"],
                    "mood_tags": row["mood_tags"],
                    "poster_url": row["poster_url"],
                    "trailer_key": row["trailer_key"],
                    "vote_average": row["vote_average"],
                })
            return results

    def get_movie_details_for_tool(self, tmdb_id: int) -> Optional[dict]:
        """Return rich movie details for Gemini's get_movie_details tool.

        Includes all metadata fields needed for detailed discussion.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3, m.mood_tags,
                       m.collection, m.origin_country, m.keywords
                FROM movies m
                WHERE m.tmdb_id = ?
                """,
                (tmdb_id,),
            ).fetchone()
            if not row:
                return None

            in_jellyfin = bool(row["has_file"])
            ott_platforms = _ott_for_movie(row["id"], "IN", conn)

            return {
                "id": row["id"],
                "title": row["title"],
                "year": row["year"],
                "tmdb_id": row["tmdb_id"],
                "genre": row["genre"],
                "runtime": row["runtime"],
                "overview": row["tmdb_overview"],
                "in_jellyfin": in_jellyfin,
                "ott_platforms": ott_platforms,
                "director": row["director"],
                "cast_top3": row["cast_top3"],
                "mood_tags": row["mood_tags"],
                "collection": row["collection"],
                "origin_country": row["origin_country"],
                "keywords": row["keywords"],
                "vote_average": row["vote_average"],
            }

    def get_watch_history_for_tool(
        self,
        days_back: int = 7,
        reaction: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return recent watch history entries for Gemini's get_watch_history tool.

        Filters by days_back and optional reaction.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

        with self._connect() as conn:
            if reaction:
                rows = conn.execute(
                    """
                    SELECT wh.tmdb_id, wh.title, wh.reaction, wh.watched_at,
                           m.genre, m.director, m.runtime
                    FROM watch_history wh
                    LEFT JOIN movies m ON wh.tmdb_id = m.tmdb_id
                    WHERE wh.watched_at >= ?
                      AND wh.reaction = ?
                    ORDER BY wh.watched_at DESC
                    LIMIT ?
                    """,
                    (cutoff, reaction, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT wh.tmdb_id, wh.title, wh.reaction, wh.watched_at,
                           m.genre, m.director, m.runtime
                    FROM watch_history wh
                    LEFT JOIN movies m ON wh.tmdb_id = m.tmdb_id
                    WHERE wh.watched_at >= ?
                    ORDER BY wh.watched_at DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_all_directors(self) -> list[str]:
        """Return all unique non-null director names. Used for intent classification."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT director FROM movies WHERE director IS NOT NULL AND director != ''"
            ).fetchall()
            return [r["director"] for r in rows]

    def get_all_genres(self) -> list[str]:
        """Return all unique genre names (split from comma/slash-separated strings).
        Used for intent classification.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT genre FROM movies WHERE genre IS NOT NULL AND genre != ''"
            ).fetchall()
            genres: set[str] = set()
            for r in rows:
                for part in r["genre"].replace("/", ",").split(","):
                    g = part.strip()
                    if g:
                        genres.add(g.lower())
            return list(genres)

    def upsert_movie_from_chat(
        self,
        title: str,
        year: Optional[int] = None,
        tmdb_id: Optional[int] = None,
        tmdb_title: Optional[str] = None,
        tmdb_original_title: Optional[str] = None,
        tmdb_release_year: Optional[int] = None,
        tmdb_overview: Optional[str] = None,
        tmdb_popularity: Optional[float] = None,
        poster_url: Optional[str] = None,
        genre: Optional[str] = None,
        runtime: Optional[int] = None,
    ) -> int:
        """Insert a new movie from chat with synthetic letterboxd_slug/url.

        Creates a synthetic slug and URL so the movies table constraints
        (letterboxd_slug UNIQUE, letterboxd_url NOT NULL) are satisfied
        even when the movie doesn't come from a Letterboxd sync.
        If the movie already exists (by tmdb_id or title+year), it updates instead.
        """
        now = utc_now()
        # Synthetic slug to satisfy the UNIQUE constraint
        slug = f"chat-{title.lower().replace(' ', '-')}"
        if year:
            slug += f"-{year}"
        if tmdb_id:
            slug += f"-tmdb{tmdb_id}"
        synthetic_url = f"https://letterboxd.com/film/{slug}/"

        with self._connect() as conn:
            # Check if movie already exists by tmdb_id or title+year
            existing = None
            if tmdb_id:
                existing = conn.execute(
                    "SELECT id FROM movies WHERE tmdb_id = ? LIMIT 1",
                    (tmdb_id,),
                ).fetchone()
            if not existing and title:
                existing = conn.execute(
                    """
                    SELECT id FROM movies
                    WHERE title = ? AND COALESCE(year, -1) = COALESCE(?, -1)
                    LIMIT 1
                    """,
                    (title, year),
                ).fetchone()

            if existing:
                # Update existing movie with any new data
                conn.execute(
                    """
                    UPDATE movies
                    SET tmdb_id = COALESCE(?, tmdb_id),
                        tmdb_title = COALESCE(?, tmdb_title),
                        tmdb_original_title = COALESCE(?, tmdb_original_title),
                        tmdb_release_year = COALESCE(?, tmdb_release_year),
                        tmdb_overview = COALESCE(?, tmdb_overview),
                        tmdb_popularity = COALESCE(?, tmdb_popularity),
                        poster_url = COALESCE(?, poster_url),
                        genre = COALESCE(?, genre),
                        runtime = COALESCE(?, runtime),
                        last_seen_watchlist_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        tmdb_id, tmdb_title, tmdb_original_title,
                        tmdb_release_year, tmdb_overview, tmdb_popularity,
                        poster_url, genre, runtime,
                        now, now, existing["id"],
                    ),
                )
                movie_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO movies (
                        letterboxd_slug, letterboxd_url,
                        title, year,
                        tmdb_id, tmdb_title, tmdb_original_title,
                        tmdb_release_year, tmdb_overview, tmdb_popularity,
                        poster_url, genre, runtime,
                        requested_in_radarr, requested_in_radarr_at, radarr_movie_id,
                        first_seen_watchlist_at, last_seen_watchlist_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug, synthetic_url,
                        title, year,
                        tmdb_id, tmdb_title, tmdb_original_title,
                        tmdb_release_year, tmdb_overview, tmdb_popularity,
                        poster_url, genre, runtime,
                        0, None, None,
                        now, now, now, now,
                    ),
                )
                movie_id = int(cursor.lastrowid)
            conn.commit()
            return movie_id

    def get_ready_movies(
        self,
        country_code: str = "IN",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Return movies with has_file=1 (downloaded and ready to play).

        Paginated with limit/offset. Ordered by title alphabetically.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.title, m.year, m.tmdb_id, m.poster_url, m.genre,
                       m.runtime, m.tmdb_overview, m.has_file, m.trailer_key,
                       m.vote_average, m.director, m.cast_top3
                FROM movies m
                WHERE m.has_file = 1 AND m.tmdb_id IS NOT NULL
                ORDER BY m.title ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

            results: list[dict] = []
            for row in rows:
                ott_platforms = _ott_for_movie(row["id"], country_code, conn)
                results.append({
                    "id": row["id"],
                    "title": row["title"],
                    "year": row["year"],
                    "tmdb_id": row["tmdb_id"],
                    "poster_url": row["poster_url"],
                    "genre": row["genre"],
                    "runtime": row["runtime"],
                    "overview": row["tmdb_overview"],
                    "in_jellyfin": True,
                    "ott_platforms": ott_platforms,
                    "trailer_key": row["trailer_key"],
                    "vote_average": row["vote_average"],
                    "director": row["director"],
                    "cast_top3": row["cast_top3"],
                })
            return results

    def count_ready_movies(self) -> int:
        """Count movies with has_file=1 (downloaded and ready)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM movies WHERE has_file = 1 AND tmdb_id IS NOT NULL"
            ).fetchone()
            return int(row[0]) if row else 0
