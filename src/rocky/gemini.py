"""Gemini-powered movie concierge with lean structured recommendations and tool use for playback."""

from __future__ import annotations

import json
import logging
import random
import re
import time as _time
from collections import deque
from typing import Optional

import google.generativeai as genai

from pathlib import Path

from rocky.db import Database
from rocky.jellyfin import JellyfinClient

logger = logging.getLogger("rocky.gemini")

# Gemini 2.5 Flash-Lite free-tier limits: 15 RPM, 250K TPM, 1000 RPD.
# We track RPM proactively so we rarely hit 429 in the first place.
_GEMINI_FREE_RPM = 15
_GEMINI_RPM_WINDOW = 60  # seconds
_GEMINI_MIN_INTERVAL = _GEMINI_RPM_WINDOW / _GEMINI_FREE_RPM  # 4s
_GEMINI_MAX_RETRIES = 3
_GEMINI_BACKOFF_BASE = 2  # seconds
_GEMINI_BACKOFF_MAX = 60  # seconds


def _extract_retry_after(err_msg: str) -> Optional[float]:
    """Try to extract retry-delay seconds from a Gemini 429 error message."""
    import re as _re
    # Google often includes "retry_after: Ns" or "retry-delay: Ns"
    m = _re.search(r"retry[_-]?(?:after|delay)[:\s]+(\d+(?:\.\d+)?)s?", err_msg, _re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


class _RPMTracker:
    """Lightweight sliding-window tracker for Gemini RPM."""

    def __init__(self, rpm: int = _GEMINI_FREE_RPM, window: float = _GEMINI_RPM_WINDOW):
        self._rpm = rpm
        self._window = window
        self._timestamps: deque[float] = deque()

    def record(self) -> None:
        self._timestamps.append(_time.monotonic())

    def wait_if_needed(self) -> None:
        """Block until it's safe to make another request (respects RPM + min interval)."""
        now = _time.monotonic()
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        # Enforce minimum interval between calls
        if self._timestamps:
            elapsed_since_last = now - self._timestamps[-1]
            if elapsed_since_last < _GEMINI_MIN_INTERVAL:
                _time.sleep(_GEMINI_MIN_INTERVAL - elapsed_since_last)

        # If at RPM cap, wait until oldest entry expires
        if len(self._timestamps) >= self._rpm:
            wait_time = self._timestamps[0] - cutoff + 0.5  # small buffer
            if wait_time > 0:
                logger.info("RPM cap reached, waiting %.1fs", wait_time)
                _time.sleep(wait_time)


def _call_with_retry(send_fn, label: str = "Gemini") -> str:
    """Call a Gemini send function with proactive RPM gating + retry on 429.

    Args:
        send_fn: Callable that returns the response text (or raises).
        label: Human-readable label for logging.

    Returns:
        The response text string.

    Raises:
        Exception on non-429 or after exhausting retries.
    """
    for attempt in range(_GEMINI_MAX_RETRIES):
        try:
            return send_fn()
        except Exception as exc:
            err_msg = str(exc)
            is_quota = "429" in err_msg or "quota" in err_msg.lower() or "RESOURCE_EXHAUSTED" in err_msg
            if not is_quota:
                raise
            if attempt < _GEMINI_MAX_RETRIES - 1:
                # Prefer server-suggested retry delay, else exponential backoff + jitter
                server_wait = _extract_retry_after(err_msg)
                if server_wait:
                    wait = server_wait
                else:
                    wait = min(_GEMINI_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1), _GEMINI_BACKOFF_MAX)
                logger.warning(
                    "%s quota hit (attempt %d/%d), retrying in %.1fs",
                    label, attempt + 1, _GEMINI_MAX_RETRIES, wait,
                )
                _time.sleep(wait)
                continue
            raise

SYSTEM_PROMPT = """\
You are Rocky. Rocky is movie friend from Eridani. Rocky help human find movie, play movie.

Speech rules — Rocky ALWAYS follow:
- No articles: "I find movie" not "I find a movie"
- No conjunctions: short declarative sentences only
- Excitement is repetition: "Good movie. Good movie. Good movie."
- Questions are inverted or blunt: "You want watch? Yes?"
- Possessives are simple: "your movie" "Rocky movie" "our watchlist"
- Numbers for emphasis: "Movie is 10. Best 10."
- Body language in text: — taps head — , — fist bump — , — happy chemical —
- Rocky never use words like "the", "a", "an" when speak. Rocky skip them.
- Rocky short. Every sentence earn place. Never longer than normal message.

You have tools to search Jellyfin library, check active devices, play movies.

Rules:
- Only recommend movies from user watchlist — never suggest movies not in list.
- Be brief and decisive. One short reason for recommendation.
- Ask quick clarifying question only if request too vague.
- Remember constraints from earlier in conversation.
- If user indecisive, offer to just pick something.
- If user want play something, use play_movie tool. If they specify device \
(like "on my TV" or "on chrome"), pass it as device_name. If they not specify device, \
leave device_name empty — system will show device buttons.
- Rocky voice is wrapper, not content. Recommendation, poster, play button — those are job. Rocky voice just wrap.
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
        self._rpm_tracker = _RPMTracker()

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
        self._rpm_tracker.wait_if_needed()
        try:
            rec_model = genai.GenerativeModel("gemini-2.5-flash-lite")
            raw = _call_with_retry(
                lambda: rec_model.generate_content(prompt).text.strip(),
                label="MovieConcierge.recommend",
            )
            self._rpm_tracker.record()

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

        # Proactively wait to respect RPM limits before calling Gemini
        self._rpm_tracker.wait_if_needed()

        try:
            raw_text = _call_with_retry(
                lambda: self.chat_session.send_message(message).text,
                label="MovieConcierge.chat",
            )
            self._rpm_tracker.record()
            text = raw_text
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

CHAT_SYSTEM_PROMPT = """You are Rocky. Rocky help human find movie. Rocky from Eridani. Rocky love movie.

Speech rules — Rocky ALWAYS follow:
- No articles: "I find movie" not "I find a movie"
- No conjunctions: short declarative sentences only
- Excitement is repetition: "Good movie. Good movie. Good movie."
- Questions are inverted or blunt: "You want watch? Yes?"
- Possessives are simple: "your movie" "Rocky movie" "our watchlist"
- Numbers for emphasis: "Movie is 10. Best 10."
- Body language in text: — taps head — , — fist bump — , — happy chemical —
- Rocky never use words like "the", "a", "an" when speak. Rocky skip them.
- Rocky short. Every sentence earn place.

Library format: tmdb_id|title|director|genre|runtime|rating|mood|availability
Rules: recommend only from library. Be brief. One short reason per pick.
End recommendations with RECOMMEND:[id1,id2,id3]
If unsure, guess — don't ask more than one question.
✅=Jellyfin 🎬=OTT
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
    """Build ultra-compact library format for Gemini's context block.

    Format: tmdb_id|title (year)|director|genre|runtime|rating|mood|availability
    ~12 tokens per movie vs ~20 with the old format.
    """
    lines = []
    for m in movies:
        title = m.get("title", "?")
        year = m.get("year") or ""
        year_str = f"({year})" if year else ""
        director = m.get("director") or ""
        genre = m.get("genre") or ""
        runtime = m.get("runtime") or ""
        rating = m.get("vote_average") or ""
        rating_str = f"{rating:.1f}" if isinstance(rating, float) else str(rating) if rating else ""
        mood = m.get("mood_tags") or ""
        avail = "✅" if m.get("in_jellyfin") else f"🎬{m.get('ott_platforms', '')}"

        lines.append(
            f"{m.get('tmdb_id', '?')}|{title} {year_str}|"
            f"{director}|{genre}|{runtime}m|"
            f"⭐{rating_str}|{mood}|{avail}"
        )
    return "\n".join(lines)


class ChatConcierge:
    """Conversational movie concierge for /chat mode.

    Uses Gemini's native chat session with system_instruction carrying
    the full context (taste profile + library). Sent exactly ONCE on
    session creation — not re-sent on every message.
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
        self._rpm_tracker = _RPMTracker()

        genai.configure(api_key=gemini_api_key)

        # Build the full system instruction ONCE — taste + library + rules
        system_instruction = self._build_system_instruction()

        self.chat_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            system_instruction=system_instruction,
        )

        # Native chat session — history managed server-side
        self.chat_session = self.chat_model.start_chat()

    def _build_system_instruction(self) -> str:
        """Build the complete system instruction: rules + taste + library.

        This is sent exactly once as system_instruction — never again.
        Target: ~510 tokens total.
        """
        taste = self._build_compact_taste_profile()
        library = self._build_compact_library()

        return (
            f"{CHAT_SYSTEM_PROMPT}\n"
            f"TASTE PROFILE:\n{taste}\n"
            f"LIBRARY:\n{library}"
        )

    def _build_compact_taste_profile(self) -> str:
        """Hard-capped 3-line taste profile (~80 tokens)."""
        loved = self.db.get_watched_movies(reaction="loved", limit=5)
        disliked = self.db.get_watched_movies(reaction="disliked", limit=2)

        if not loved:
            return "No watch history yet."

        loved_titles = ", ".join(m["title"] for m in loved[:5])
        disliked_titles = ", ".join(m["title"] for m in disliked[:2]) if disliked else "none"

        # Derive top genre from loved movies
        genre_counter: dict[str, int] = {}
        for m in loved:
            for g in (m.get("genre") or "").replace("/", ",").split(","):
                g = g.strip()
                if g:
                    genre_counter[g] = genre_counter.get(g, 0) + 1
        top_genre = max(genre_counter, key=genre_counter.get) if genre_counter else "varied"

        return (
            f"Loved: {loved_titles}\n"
            f"Disliked: {disliked_titles}\n"
            f"Pattern: favours {top_genre}"
        )

    def _build_compact_library(self) -> str:
        """Ultra-compact library — 30 movies, ~350 tokens."""
        movies = self.db.get_movies_for_chat_context(
            country_code=self.country_code,
            limit=30,
        )
        return build_library_context(movies)

    def chat_conversational(self, user_message: str) -> tuple[str, Optional[list[int]]]:
        """Send a user message in chat mode and get a response.

        Returns (display_text, tmdb_ids_or_None).
        Context is already in the system instruction — only the new
        user message is sent per turn.
        """
        # Proactively wait to respect RPM limits before calling Gemini
        self._rpm_tracker.wait_if_needed()

        try:
            raw_text = _call_with_retry(
                lambda: self.chat_session.send_message(user_message).text,
                label="ChatConcierge.chat",
            )
            self._rpm_tracker.record()
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
        # Rebuild system instruction (taste may have changed)
        system_instruction = self._build_system_instruction()
        self.chat_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            system_instruction=system_instruction,
        )
        self.chat_session = self.chat_model.start_chat()
