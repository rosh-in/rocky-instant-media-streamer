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
                    error_message TEXT
                );
                """
            )
            conn.commit()

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
                    error_message = ?
                WHERE id = ?
                """,
                (utc_now(), status, items_seen, items_enriched, error_message, run_id),
            )
            conn.commit()

    def upsert_movie(self, movie: WatchlistMovie, tmdb: Optional[TmdbMovie]) -> None:
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
            else:
                conn.execute(
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
                        first_seen_watchlist_at,
                        last_seen_watchlist_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        now,
                        now,
                        now,
                        now,
                    ),
                )
            conn.commit()
