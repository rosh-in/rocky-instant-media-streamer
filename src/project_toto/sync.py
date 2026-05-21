from __future__ import annotations
from datetime import datetime, timedelta, timezone

from project_toto.config import load_settings
from project_toto.db import Database
from project_toto.justwatch import JustWatchClient
from project_toto.letterboxd import fetch_watchlist
from project_toto.radarr import RadarrClient
from project_toto.tmdb import TmdbClient


def run_watchlist_sync() -> None:
    settings = load_settings()
    db = Database(settings.sqlite_path)
    db.init_schema()
    run_id = db.start_sync_run()

    seen = 0
    enriched = 0
    availability_refreshed = 0
    requested = 0
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

        if settings.justwatch_enabled:
            justwatch = JustWatchClient(
                country=settings.justwatch_country,
                language=settings.justwatch_language,
                max_results=settings.justwatch_max_results,
                best_only=settings.justwatch_best_only,
            )
            stale_before = (
                datetime.now(timezone.utc) - timedelta(hours=settings.justwatch_refresh_hours)
            ).isoformat()
            availability_targets = db.list_movies_for_availability_refresh(stale_before_iso=stale_before)

            for row in availability_targets:
                try:
                    offers = justwatch.lookup_movie_availability(
                        title=str(row["title"]),
                        year=row["year"],
                    )
                    db.replace_movie_availability(
                        movie_id=int(row["id"]),
                        country_code=settings.justwatch_country,
                        offers=offers,
                    )
                    availability_refreshed += 1
                except Exception as exc:
                    print(f"JustWatch refresh failed for {row['title']}: {exc}")
        if settings.radarr_enabled and settings.radarr_api_key:
            radarr = RadarrClient(
                base_url=settings.radarr_url,
                api_key=settings.radarr_api_key,
                root_folder=settings.radarr_root_folder,
                quality_profile_id=settings.radarr_quality_profile_id,
                monitored=settings.radarr_monitored,
                search_on_add=settings.radarr_search_on_add,
            )
            pending = db.list_unrequested_movies()
            if settings.radarr_dry_run:
                requested = len(pending)
                print(f"Radarr dry-run enabled. Would request {requested} movie(s).")
            else:
                for row in pending:
                    try:
                        radarr_movie_id = radarr.add_movie(int(row["tmdb_id"]))
                        db.mark_requested_in_radarr(movie_id=int(row["id"]), radarr_movie_id=radarr_movie_id)
                        requested += 1
                    except Exception as exc:
                        print(
                            "Radarr add failed for "
                            f"{row['title']} (tmdb={row['tmdb_id']}): {exc}"
                        )

        db.finish_sync_run(
            run_id=run_id,
            status="success",
            items_seen=seen,
            items_enriched=enriched,
            items_availability_refreshed=availability_refreshed,
            items_requested=requested,
        )
        print(
            "Sync complete. "
            "AvailabilityRefreshed="
            f"{availability_refreshed}, Seen={seen}, Enriched={enriched}, Requested={requested}, "
            f"DB={settings.sqlite_path}"
        )
    except Exception as exc:
        db.finish_sync_run(
            run_id=run_id,
            status="failed",
            items_seen=seen,
            items_enriched=enriched,
            items_availability_refreshed=availability_refreshed,
            items_requested=requested,
            error_message=str(exc),
        )
        raise
