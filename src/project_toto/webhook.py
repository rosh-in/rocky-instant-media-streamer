"""FastAPI webhook server for Jellyfin playback events and Radarr notifications.

Receives webhook POSTs and logs events to the SQLite database.
Run via: python -m project_toto.webhook
Or:     uvicorn project_toto.webhook:app --host 0.0.0.0 --port 8400
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from project_toto.config import load_settings
from project_toto.db import Database

logger = logging.getLogger("project_toto.webhook")

app = FastAPI(title="Project Toto Webhooks")


class PlaybackPayload(BaseModel):
    """Expected payload from Jellyfin playback completion webhook."""
    event: str = ""
    ItemId: str = ""
    Name: str = ""
    Year: int | None = None
    TmdbId: int | None = None
    ProviderIds: dict[str, str] | None = None


class RadarrPayload(BaseModel):
    """Expected payload from Radarr webhook."""
    eventType: str = ""
    movie: dict[str, Any] | None = None


def _get_db() -> Database:
    settings = load_settings()
    db = Database(settings.sqlite_path)
    db.init_schema()
    return db


def _extract_tmdb_id(payload: PlaybackPayload) -> int | None:
    """Extract TMDB ID from Jellyfin webhook payload."""
    if payload.TmdbId:
        return payload.TmdbId
    if payload.ProviderIds:
        tmdb = payload.ProviderIds.get("tmdb") or payload.ProviderIds.get("Tmdb")
        if tmdb:
            try:
                return int(tmdb)
            except ValueError:
                pass
    return None


@app.post("/webhook/jellyfin/playback")
async def jellyfin_playback(request: Request) -> dict:
    """Handle Jellyfin playback completion webhook.

    Logs the playback event to watch_history.
    Accepts both JSON and form-data payloads.
    """
    try:
        content_type = request.headers.get("content-type", "")

        if "application/json" in content_type:
            body = await request.json()
        else:
            # Jellyfin webhook plugin may send form data
            form = await request.form()
            body = dict(form)

        event = body.get("event", "")
        name = body.get("Name", body.get("name", ""))
        tmdb_id_raw = body.get("TmdbId") or body.get("tmdbId")

        # Extract TMDB ID
        tmdb_id = None
        if tmdb_id_raw:
            try:
                tmdb_id = int(tmdb_id_raw)
            except (ValueError, TypeError):
                pass

        # Check provider IDs for TMDB
        if not tmdb_id:
            provider_ids = body.get("ProviderIds") or {}
            if isinstance(provider_ids, str):
                import json
                try:
                    provider_ids = json.loads(provider_ids)
                except Exception:
                    provider_ids = {}
            tmdb_str = provider_ids.get("tmdb") or provider_ids.get("Tmdb")
            if tmdb_str:
                try:
                    tmdb_id = int(tmdb_str)
                except (ValueError, TypeError):
                    pass

        # Only log on playback completion events
        playback_events = {"PlaybackStop", "PlaybackStopped", "MovieFileDeleted"}
        if event in playback_events and name:
            db = _get_db()
            db.log_watch_history(
                tmdb_id=tmdb_id,
                title=name,
                reaction=None,
                reaction_emoji=None,
            )
            logger.info("Logged playback: %s (tmdb_id=%s, event=%s)", name, tmdb_id, event)

        return {"status": "ok", "event": event, "name": name}

    except Exception as exc:
        logger.exception("Jellyfin webhook processing failed")
        return {"status": "error", "message": str(exc)}


@app.post("/webhook/radarr")
async def radarr_webhook(request: Request) -> dict:
    """Handle Radarr webhook notifications.

    Logs movie grab/import events for reference.
    """
    try:
        content_type = request.headers.get("content-type", "")

        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)

        event_type = body.get("eventType", "")
        movie_data = body.get("movie", {})
        title = ""
        tmdb_id = None

        if isinstance(movie_data, dict):
            title = movie_data.get("title", "")
            tmdb_id = movie_data.get("tmdbId")

        logger.info("Radarr event: %s — %s (tmdb_id=%s)", event_type, title, tmdb_id)

        return {"status": "ok", "eventType": event_type, "title": title}

    except Exception as exc:
        logger.exception("Radarr webhook processing failed")
        return {"status": "error", "message": str(exc)}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def run_webhook() -> None:
    """Start the FastAPI webhook server (blocking)."""
    from project_toto.logging_config import setup_logging
    setup_logging()

    import uvicorn
    port = 8400
    logger.info("Starting Project Toto webhook server on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run_webhook()
