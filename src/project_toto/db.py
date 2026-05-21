import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
                    items_requested INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                );
                """
            )
            self._ensure_column(conn, "movies", "requested_in_radarr", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "movies", "requested_in_radarr_at", "TEXT")
            self._ensure_column(conn, "movies", "radarr_movie_id", "INTEGER")
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
                    items_requested = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (utc_now(), status, items_seen, items_enriched, items_requested, error_message, run_id),
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
