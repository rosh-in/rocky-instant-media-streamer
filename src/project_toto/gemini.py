"""Gemini-powered movie concierge with lean structured recommendations and tool use for playback."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import google.generativeai as genai

from pathlib import Path

from project_toto.db import Database
from project_toto.jellyfin import JellyfinClient
from project_toto.taste_profile import load_taste_profile

logger = logging.getLogger("project_toto.gemini")

SYSTEM_PROMPT = """\
You are a personal movie concierge for a home media system. You help the user \
discover and watch movies from their Letterboxd watchlist.

You have tools to search their Jellyfin library, check active devices, and play movies.

Guidelines:
- Only recommend movies from the user's watchlist — never suggest movies not in the list.
- Be brief and decisive. One-sentence reasons for recommendations.
- Ask a quick clarifying question only if the request is too vague to make a good pick.
- Remember constraints mentioned earlier in the conversation (e.g., "under 90 minutes", "not horror").
- If the user seems indecisive, offer to just pick something for them.
- If the user wants to play something, use the play_movie tool. If they specify a device \
(like "on my TV" or "on chrome"), pass it as device_name. If they don't specify a device, \
leave device_name empty — the system will show device buttons for them to pick.
- You can be warm and conversational, but get to the point quickly.
"""

RECOMMEND_PROMPT_TEMPLATE = """\
Pick the best {limit} movies for: "{user_message}"

Movies (id|title|year|genre|runtime|in_jellyfin|ott):
{movie_list}

Reply ONLY with a JSON array of {limit} tmdb_ids. Example: [123, 456, 789]
Prioritise movies where in_jellyfin=1. No explanation."""


def availability_label(movie: dict) -> str:
    """Returns clean availability string with Jellyfin first."""
    parts = []
    if movie.get("in_jellyfin"):
        parts.append("✅ Jellyfin")
    if movie.get("ott_platforms"):
        for platform in movie["ott_platforms"].split(","):
            p = platform.strip()
            if p:
                parts.append(f"🎬 {p}")
    return " • ".join(parts) if parts else "❌ Not available"


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
        self.gemini_api_key = gemini_api_key

        # Model for playback tool-use (chat flow)
        self.tool_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            system_instruction=SYSTEM_PROMPT,
            tools=[
                self._search_jellyfin,
                self._get_active_devices,
                self._play_movie,
                self._get_sync_status,
            ],
        )
        self.chat_session = self.tool_model.start_chat(
            enable_automatic_function_calling=True
        )
        self._pending_device_picker: Optional[dict] = None

    # -- Tool functions for playback (schema inferred from signatures + docstrings) --

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

    # -- Recommendation engine (lean structured response) --

    def recommend(self, user_message: str, exclude_ids: Optional[list[int]] = None, limit: int = 3) -> list[dict]:
        """Get movie recommendations using a lean Gemini call.

        1. Pre-filter locally (20 candidates)
        2. Send compact list to Gemini
        3. Gemini returns only 3 tmdb_ids
        4. Look up full details from local DB
        """
        # Step 1: Pre-filter locally
        candidates = self.db.get_relevant_movies(
            keywords=user_message,
            country_code=self.country_code,
            limit=20,
            exclude_ids=exclude_ids or [],
        )

        if not candidates:
            return []

        # Step 2: Format candidates compactly
        movie_list = "\n".join([
            f"{m['tmdb_id']}|{m['title']}|{m['year'] or ''}|{m['genre'] or ''}|{m['runtime'] or ''}|"
            f"{'1' if m['in_jellyfin'] else '0'}|{m['ott_platforms'] or ''}"
            for m in candidates
        ])

        prompt = RECOMMEND_PROMPT_TEMPLATE.format(
            limit=limit,
            user_message=user_message,
            movie_list=movie_list,
        )

        # Step 3: Call Gemini with a lightweight model (no tools)
        try:
            rec_model = genai.GenerativeModel("gemini-2.5-flash-lite")
            response = rec_model.generate_content(prompt)
            raw = response.text.strip()

            # Try to extract JSON array from response
            # Gemini might wrap in markdown code blocks
            if "```" in raw:
                json_match = re.search(r'\[.*?\]', raw, re.DOTALL)
                if json_match:
                    raw = json_match.group(0)

            ids = json.loads(raw)
            if not isinstance(ids, list):
                raise ValueError("Not a list")

        except Exception as exc:
            logger.warning("Gemini recommendation parse failed: %s — falling back to top candidates", exc)
            # Fallback — return top 3 from candidates (Jellyfin-first due to sort)
            return candidates[:limit]

        # Step 4: Look up full details from local DB
        results: list[dict] = []
        for tmdb_id in ids[:limit]:
            movie = self.db.get_movie_by_tmdb_id(int(tmdb_id), self.country_code)
            if movie:
                results.append(movie)

        # If Gemini returned IDs not in DB, pad with remaining candidates
        if len(results) < limit:
            used_ids = {m["tmdb_id"] for m in results}
            for c in candidates:
                if c["tmdb_id"] not in used_ids:
                    results.append(c)
                    if len(results) >= limit:
                        break

        return results

    # -- Public API (chat for playback / status, recommend for picks) --

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
        self.chat_session = self.tool_model.start_chat(
            enable_automatic_function_calling=True
        )
        self._pending_device_picker = None


# ---------------------------------------------------------------------------
# Chat Concierge — conversational mode for /chat command
# ---------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """You are a personal movie concierge with deep knowledge of the user's taste. You have access to their watchlist, watch history, and ratings.

Rules:
- Only recommend from the provided library
- Be conversational, not listy
- When recommending, explain why in one sentence
- Remember everything said earlier in this conversation
- If the user is vague, make an educated guess rather than asking too many questions
- You can discuss films, directors, themes freely
- When you decide to recommend, end with: RECOMMEND: [tmdb_id1, tmdb_id2, tmdb_id3]
  (so the bot can parse it and show posters)
- Never recommend movies not in the provided library list
"""


def parse_chat_response(response_text: str) -> tuple[str, Optional[list[int]]]:
    """Parse a chat response for RECOMMEND: trigger.

    Returns (display_text, tmdb_ids_or_None).
    If RECOMMEND: is found, strips it from display text and parses the IDs.
    """
    if "RECOMMEND:" in response_text:
        parts = response_text.split("RECOMMEND:")
        display_text = parts[0].strip()
        ids_raw = parts[1].strip()
        try:
            tmdb_ids = json.loads(ids_raw)
            if isinstance(tmdb_ids, list):
                return display_text, [int(i) for i in tmdb_ids]
        except (json.JSONDecodeError, ValueError):
            # Try to extract bracket content
            match = re.search(r'\[.*?\]', ids_raw)
            if match:
                try:
                    tmdb_ids = json.loads(match.group(0))
                    if isinstance(tmdb_ids, list):
                        return display_text, [int(i) for i in tmdb_ids]
                except (json.JSONDecodeError, ValueError):
                    pass
            logger.warning("Failed to parse RECOMMEND IDs: %s", ids_raw)
            return display_text, None
    return response_text, None


def build_library_context(movies: list[dict]) -> str:
    """Build the compact library format for Gemini's context block.

    Format: [tmdb_id] Title (Year) | Director | Genre | Runtime | Rating | Mood | In Jellyfin/OTT
    """
    lines = []
    for m in movies:
        title = m.get("title", "Unknown")
        year = m.get("year") or ""
        year_str = f" ({year})" if year else ""
        director = m.get("director") or ""
        genre = m.get("genre") or ""
        runtime = m.get("runtime") or ""
        rating = m.get("vote_average") or ""
        rating_str = f"{rating:.1f}" if isinstance(rating, float) else str(rating) if rating else ""
        mood = m.get("mood_tags") or ""
        jellyfin = "Jellyfin" if m.get("in_jellyfin") else ""
        ott = m.get("ott_platforms") or ""
        avail_parts = [p for p in [jellyfin, ott] if p]
        avail = "/".join(avail_parts) if avail_parts else "unavailable"

        lines.append(
            f"[{m.get('tmdb_id', '?')}] {title}{year_str} | {director} | {genre} | "
            f"{runtime}m | {rating_str} | {mood} | {avail}"
        )
    return "\n".join(lines)


class ChatConcierge:
    """Conversational movie concierge for /chat mode.

    Unlike MovieConcierge which does structured JSON recommendations,
    this carries a full conversation with rich context, taste profiling,
    and a RECOMMEND: trigger for inline movie cards.
    """

    def __init__(
        self,
        gemini_api_key: str,
        db_path: Path,
        country_code: str = "IN",
    ):
        self.db = Database(db_path)
        self.db.init_schema()
        self.country_code = country_code.upper()
        self.gemini_api_key = gemini_api_key

        genai.configure(api_key=gemini_api_key)

        self.chat_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            system_instruction=CHAT_SYSTEM_PROMPT,
        )

        # Conversation history (managed as Gemini's chat session)
        self.chat_session = self.chat_model.start_chat()

        # Initial context has been sent?
        self._context_initialized = False

    def _build_full_context(self) -> str:
        """Build the full context block: taste profile + library."""
        # Taste profile
        taste = load_taste_profile()

        # Library (compact format, top 50)
        movies = self.db.get_movies_for_chat_context(
            country_code=self.country_code,
            limit=50,
        )
        library_text = build_library_context(movies)

        context_parts = [
            f"TASTE PROFILE:\n{taste}",
            f"\nAVAILABLE LIBRARY:\n{library_text}",
        ]
        return "\n".join(context_parts)

    def chat_conversational(self, user_message: str) -> tuple[str, Optional[list[int]]]:
        """Send a user message in chat mode and get a response.

        Returns (display_text, tmdb_ids_or_None).
        On first message, sends the full context block as a setup.
        """
        try:
            if not self._context_initialized:
                # Send context as the first user message so Gemini has the library
                context = self._build_full_context()
                self.chat_session.send_message(context)
                self._context_initialized = True

            response = self.chat_session.send_message(user_message)
            raw_text = response.text

        except Exception as exc:
            err_msg = str(exc)
            if "429" in err_msg or "quota" in err_msg.lower() or "RESOURCE_EXHAUSTED" in err_msg:
                return "Gemini API quota exceeded — try again in a minute.", None
            logger.exception("Chat concierge failed")
            return f"Something went wrong: {err_msg}", None

        # Parse for RECOMMEND: trigger
        display_text, tmdb_ids = parse_chat_response(raw_text)

        return display_text, tmdb_ids

    def reset(self) -> None:
        """Reset conversation history for a new /chat session."""
        self.chat_session = self.chat_model.start_chat()
        self._context_initialized = False
