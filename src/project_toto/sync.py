from __future__ import annotations

from project_toto.config import load_settings
from project_toto.db import Database
from project_toto.letterboxd import fetch_watchlist
from project_toto.tmdb import TmdbClient


def run_watchlist_sync() -> None:
    settings = load_settings()
    db = Database(settings.sqlite_path)
    db.init_schema()
    run_id = db.start_sync_run()

    seen = 0
    enriched = 0
    try:
        watchlist = fetch_watchlist(
            username=settings.letterboxd_username,
            max_pages=settings.letterboxd_max_pages,
        )
        tmdb_client = TmdbClient(settings.tmdb_api_key)

        for movie in watchlist:
            seen += 1
            tmdb_movie = tmdb_client.enrich_movie(movie)
            if tmdb_movie:
                enriched += 1
            db.upsert_movie(movie, tmdb_movie)

        db.finish_sync_run(
            run_id=run_id,
            status="success",
            items_seen=seen,
            items_enriched=enriched,
        )
        print(f"Sync complete. Seen={seen}, Enriched={enriched}, DB={settings.sqlite_path}")
    except Exception as exc:
        db.finish_sync_run(
            run_id=run_id,
            status="failed",
            items_seen=seen,
            items_enriched=enriched,
            error_message=str(exc),
        )
        raise
