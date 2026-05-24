"""Gemini-powered movie concierge with tool use and conversation memory."""

from __future__ import annotations

import logging
from typing import Optional

import google.generativeai as genai

from pathlib import Path

from project_toto.db import Database
from project_toto.jellyfin import JellyfinClient

logger = logging.getLogger("project_toto.gemini")

SYSTEM_PROMPT = """\
You are a personal movie concierge for a home media system. You help the user \
discover and watch movies from their Letterboxd watchlist.

You have tools to look up their watchlist, search their Jellyfin library, \
check streaming availability, see active devices, and play movies.

Guidelines:
- Only recommend movies from the user's watchlist — never suggest movies not in the list.
- Be brief and decisive. One-sentence reasons for recommendations.
- Ask a quick clarifying question only if the request is too vague to make a good pick.
- Remember constraints mentioned earlier in the conversation (e.g., "under 90 minutes", "not horror").
- If the user seems indecisive, offer to just pick something for them.
- When you recommend a movie, mention if it's available locally (Jellyfin) or on a streaming platform (OTT).
- If the user wants to play something, use the play_movie tool. If they specify a device \
(like "on my TV" or "on chrome"), pass it as device_name. If they don't specify a device, \
leave device_name empty — the system will show device buttons for them to pick.
- You can be warm and conversational, but get to the point quickly.
"""


class MovieConcierge:
    """Conversational movie concierge powered by Gemini with tool use."""

    def __init__(
        self,
        gemini_api_key: str,
        db_path: Path,
        jellyfin_url: str,
        jellyfin_api_key: str,
        jellyfin_username: str,
        country_code: str = "IN",
    ):
        self.db = Database(db_path)
        self.db.init_schema()
        self.jellyfin = JellyfinClient(
            base_url=jellyfin_url,
            api_key=jellyfin_api_key,
            username=jellyfin_username,
        )
        self.country_code = country_code.upper()

        genai.configure(api_key=gemini_api_key)
        self.model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            system_instruction=SYSTEM_PROMPT,
            tools=[
                self._get_watchlist,
                self._search_jellyfin,
                self._get_active_devices,
                self._play_movie,
                self._get_sync_status,
            ],
        )
        self.chat_session = self.model.start_chat(
            enable_automatic_function_calling=True
        )
        self._pending_device_picker: Optional[dict] = None

    # -- Tool functions (schema inferred from signatures + docstrings) ---------

    def _get_watchlist(self) -> list[dict]:
        """Returns the user's full movie watchlist with metadata, popularity, and OTT streaming availability. Use this to find movies that match a mood or preference."""
        with self.db._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.title, m.year, m.tmdb_overview, m.tmdb_popularity,
                       GROUP_CONCAT(a.provider_name || '(' || a.monetization_type || ')', ', ') AS availability,
                       m.requested_in_radarr
                FROM movies m
                LEFT JOIN availability a ON a.movie_id = m.id AND a.country_code = ?
                WHERE m.tmdb_id IS NOT NULL
                GROUP BY m.id
                ORDER BY m.tmdb_popularity DESC
                LIMIT 50
                """,
                (self.country_code,),
            ).fetchall()

        return [
            {
                "title": r["title"],
                "year": r["year"],
                "overview": (r["tmdb_overview"] or "")[:150],
                "popularity": r["tmdb_popularity"],
                "ott": r["availability"] or "Not on OTT",
                "downloaded": bool(r["requested_in_radarr"]),
            }
            for r in rows
        ]

    def _search_jellyfin(self, query: str) -> list[dict]:
        """Search the user's Jellyfin media library for a movie by title. Returns matching movies with their Jellyfin IDs. Use this to check if a specific movie is ready to play."""
        movies = self.jellyfin.search_movies(query, limit=5)
        return [
            {"name": m.name, "year": m.year, "item_id": m.item_id, "overview": m.overview}
            for m in movies
        ]

    def _get_active_devices(self) -> list[dict]:
        """Get the list of active Jellyfin playback devices (TVs, phones, browsers) that are currently connected and can receive play commands. Returns device names and session IDs."""
        devices = self.jellyfin.list_devices()
        return [
            {"device_name": d.device_name, "client": d.client, "session_id": d.session_id}
            for d in devices
        ]

    def _play_movie(self, title: str, device_name: str = "") -> dict:
        """Play a movie on a Jellyfin device. Searches for the movie by title. If device_name is provided (e.g. 'chrome', 'tv', 'iphone'), plays on the best-matching device. If device_name is empty or not specified, returns available devices for the user to pick from."""
        movies = self.jellyfin.search_movies(title, limit=5)
        if not movies:
            return {"error": f"No movie found for '{title}'"}

        movie = movies[0]
        devices = self.jellyfin.list_devices()
        if not devices:
            return {"error": "No active devices. Open Jellyfin on a device first."}

        # Try to match device if specified
        if device_name:
            target = device_name.lower()
            for d in devices:
                if target in d.device_name.lower() or target in d.client.lower():
                    self.jellyfin.play(session_id=d.session_id, item_id=movie.item_id)
                    return {
                        "played": True,
                        "movie": movie.name,
                        "year": movie.year,
                        "device": d.label,
                    }
            # Device not found — fall through to show all devices

        # No device matched (or none specified) — save for button selection
        self._pending_device_picker = {
            "movie_name": movie.name,
            "item_id": movie.item_id,
            "devices": [{"label": d.label, "session_id": d.session_id} for d in devices],
        }

        return {
            "played": False,
            "movie": movie.name,
            "year": movie.year,
            "available_devices": [d.label for d in devices],
            "message": "Movie found. Awaiting device selection.",
        }

    def _get_sync_status(self) -> dict:
        """Get the last sync run status and overall library statistics including how many movies are tracked and how many haven't been requested for download yet."""
        with self.db._connect() as conn:
            last_run = conn.execute(
                "SELECT started_at, status, items_seen, items_enriched, items_availability_refreshed, items_requested "
                "FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL").fetchone()[0]
            unrequested = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL AND requested_in_radarr = 0").fetchone()[0]

        result = {"total_movies": total, "unrequested": unrequested}
        if last_run:
            result["last_sync"] = last_run["started_at"][:19]
            result["status"] = last_run["status"]
            result["items_seen"] = last_run["items_seen"]
            result["items_enriched"] = last_run["items_enriched"]
        return result

    # -- Public API ------------------------------------------------------------

    def chat(self, message: str) -> tuple[str, Optional[dict]]:
        """Send a user message and get a response.

        Returns (response_text, device_picker_data).
        device_picker_data is a dict with movie info + device list when
        play_movie was called without a specific device.
        """
        self._pending_device_picker = None

        try:
            response = self.chat_session.send_message(message)
            text = response.text
        except Exception as exc:
            err_msg = str(exc)
            if "429" in err_msg or "quota" in err_msg.lower() or "RESOURCE_EXHAUSTED" in err_msg:
                text = "Gemini API quota exceeded — try again in a minute."
            else:
                logger.exception("Gemini chat failed")
                text = f"Something went wrong: {err_msg}"

        return text, self._pending_device_picker

    def reset(self) -> None:
        """Reset the conversation history."""
        self.chat_session = self.model.start_chat(
            enable_automatic_function_calling=True
        )
        self._pending_device_picker = None
