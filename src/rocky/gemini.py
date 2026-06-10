"""Rocky's brain — Gemini-powered conversational movie concierge.

Gemini is the conversational core. Every non-direct-play message flows
through here. Gemini uses function calling to search the movie DB
dynamically (by genre, mood, director, keyword, etc.), so it can
converse freely instead of relying on a static catalog.

Uses gemini-2.5-flash-lite via Google AI Studio (google-genai SDK).
JSON mode enabled for reliable structured output. Thinking disabled
for low latency (~1-2s per call).
"""
from __future__ import annotations

import json
import logging
import random
import re
import time as _time
from collections import deque
from typing import Optional

from google import genai
from google.genai import types as genai_types

from rocky.db import Database
from rocky.taste_profile import load_taste_profile
from rocky.vector_store import VectorStore

logger = logging.getLogger("rocky.gemini")

# Gemini 2.5 Flash-Lite free-tier limits: 15 RPM, 250K TPM, 1000 RPD.
_GEMINI_FREE_RPM = 15
_GEMINI_RPM_WINDOW = 60  # seconds
_GEMINI_MIN_INTERVAL = _GEMINI_RPM_WINDOW / _GEMINI_FREE_RPM  # 4s
_GEMINI_MAX_RETRIES = 3
_GEMINI_BACKOFF_BASE = 2  # seconds
_GEMINI_BACKOFF_MAX = 60  # seconds
_GEMINI_MODEL = "gemini-2.5-flash-lite"

# Conversation history limits
_MAX_HISTORY_EXCHANGES = 10  # keep last 10 user/assistant pairs
_MAX_TOOL_ROUNDS = 3  # max function-call roundtrips per chat() call


# ---------------------------------------------------------------------------
# Function declarations — tools Gemini can call to search the DB
# ---------------------------------------------------------------------------

_SEARCH_BY_KEYWORDS = genai_types.FunctionDeclaration(
    name="search_movies_by_keywords",
    description="Search movies by keywords matching title, genre, overview, or director. Use for any vague or specific movie request.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "keywords": genai_types.Schema(type="STRING", description="Space-separated keywords to search (e.g. 'sci-fi action', 'nolan', 'horror korean')"),
        },
        required=["keywords"],
    ),
)

_SEARCH_BY_MOOD = genai_types.FunctionDeclaration(
    name="search_movies_by_mood",
    description="Search movies by mood tags like 'light', 'heavy', 'gripping', 'fun', 'emotional', 'mind-bending', 'tense', 'scary', 'warm', 'epic', 'cerebral', 'escapist'.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "mood_tags": genai_types.Schema(type="STRING", description="Comma-separated mood tags (e.g. 'light,fun', 'gripping,tense')"),
        },
        required=["mood_tags"],
    ),
)

_SEARCH_BY_DIRECTOR = genai_types.FunctionDeclaration(
    name="search_movies_by_director",
    description="Search movies by director name.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "director": genai_types.Schema(type="STRING", description="Director name (partial match, e.g. 'nolan', 'bong')"),
        },
        required=["director"],
    ),
)

_SEARCH_BY_ACTOR = genai_types.FunctionDeclaration(
    name="search_movies_by_actor",
    description="Search movies by actor name. Use when the user asks for movies featuring a specific actor.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "actor": genai_types.Schema(type="STRING", description="Actor name (partial match, e.g. 'arnold', 'tom hanks')"),
        },
        required=["actor"],
    ),
)

_SEARCH_BY_GENRE = genai_types.FunctionDeclaration(
    name="search_movies_by_genre",
    description="Search movies by genre like 'horror', 'comedy', 'thriller', 'drama', 'animation'.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "genre": genai_types.Schema(type="STRING", description="Genre name (partial match, e.g. 'horror', 'sci-fi')"),
        },
        required=["genre"],
    ),
)

_GET_SHORT_MOVIES = genai_types.FunctionDeclaration(
    name="get_short_movies",
    description="Get movies with short runtime (under 90 minutes). Use when user wants something quick or short.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={},
        required=[],
    ),
)

_GET_WORLD_CINEMA = genai_types.FunctionDeclaration(
    name="get_world_cinema",
    description="Get non-US movies (world cinema / international films). Use when user asks for foreign, international, or non-English films.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={},
        required=[],
    ),
)

_GET_MOVIE_DETAILS = genai_types.FunctionDeclaration(
    name="get_movie_details",
    description="Get full details for a specific movie by tmdb_id. Returns overview, cast, director, runtime, genres, mood tags, collection, and availability. Use when the user asks about a specific movie's plot, cast, themes, or details.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "tmdb_id": genai_types.Schema(type="INTEGER", description="The TMDB ID of the movie to look up."),
        },
        required=["tmdb_id"],
    ),
)

_GET_WATCH_HISTORY = genai_types.FunctionDeclaration(
    name="get_watch_history",
    description="Get the user's recent watch history. Use when the user asks what they watched recently or this week.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "days_back": genai_types.Schema(type="INTEGER", description="Number of days to look back (default 7)."),
            "reaction": genai_types.Schema(type="STRING", description="Optional reaction filter: 'loved', 'liked', 'disliked', 'neutral', 'abandoned'."),
        },
        required=[],
    ),
)

_GET_DOWNLOAD_STATUS = genai_types.FunctionDeclaration(
    name="get_download_status",
    description="Check if a movie is downloaded and ready in Jellyfin, or if it's queued in Radarr. Use when the user asks if a movie is ready or available.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "tmdb_id": genai_types.Schema(type="INTEGER", description="The TMDB ID of the movie to check."),
        },
        required=["tmdb_id"],
    ),
)

_REQUEST_MOVIE = genai_types.FunctionDeclaration(
    name="request_movie",
    description="Request a movie to be downloaded via Radarr. Use when the user wants to add a movie to their download queue that isn't already there.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "tmdb_id": genai_types.Schema(type="INTEGER", description="The TMDB ID of the movie to request."),
        },
        required=["tmdb_id"],
    ),
)

_ADD_MOVIE_TO_WATCHLIST = genai_types.FunctionDeclaration(
    name="add_movie_to_watchlist",
    description="Add a new movie to the user's watchlist by title. Searches TMDB, adds to DB, fetches OTT availability, and optionally pushes to Radarr. Use when the user says 'add', 'request', 'get', or 'download' followed by a movie title that is not already in their library.",
    parameters=genai_types.Schema(
        type="OBJECT",
        properties={
            "title": genai_types.Schema(type="STRING", description="The movie title to search for and add."),
            "year": genai_types.Schema(type="INTEGER", description="Optional release year to disambiguate."),
        },
        required=["title"],
    ),
)

_TOOL_DECLARATIONS = [
    _SEARCH_BY_KEYWORDS,
    _SEARCH_BY_MOOD,
    _SEARCH_BY_DIRECTOR,
    _SEARCH_BY_ACTOR,
    _SEARCH_BY_GENRE,
    _GET_SHORT_MOVIES,
    _GET_WORLD_CINEMA,
    _GET_MOVIE_DETAILS,
    _GET_WATCH_HISTORY,
    _GET_DOWNLOAD_STATUS,
    _REQUEST_MOVIE,
    _ADD_MOVIE_TO_WATCHLIST,
]

# Fields to include when formatting search results for Gemini
_MOVIE_RESULT_FIELDS = ["tmdb_id", "title", "year", "genre", "runtime", "in_jellyfin", "ott_platforms", "director", "cast_top3", "mood_tags"]


def _format_movie_results(movies: list[dict]) -> str:
    """Format movie search results as compact text for Gemini."""
    if not movies:
        return "No movies found."
    lines = []
    for m in movies:
        jf = "1" if m.get("in_jellyfin") else "0"
        ott = m.get("ott_platforms", "")
        director = m.get("director", "")
        cast = m.get("cast_top3", "")
        mood = m.get("mood_tags", "")
        lines.append(
            f"{m['tmdb_id']}|{m['title']}|{m.get('year') or ''}|"
            f"{m.get('genre') or ''}|{m.get('runtime') or ''}|{jf}|{ott}|{director}|{cast}|{mood}"
        )
    return "\n".join(lines)


def _execute_tool(name: str, args: dict, db: Database, country_code: str, exclude_ids: list[int]) -> str:
    """Execute a tool call locally and return formatted results."""
    if name == "search_movies_by_keywords":
        movies = db.get_relevant_movies(
            keywords=args["keywords"], country_code=country_code,
            limit=10, exclude_ids=exclude_ids or None,
        )
        return _format_movie_results(movies)

    if name == "search_movies_by_mood":
        tags = [t.strip() for t in args["mood_tags"].split(",") if t.strip()]
        movies = db.get_movies_by_mood_tags(
            mood_tags=tags, country_code=country_code,
            limit=10, exclude_ids=exclude_ids or None,
        )
        return _format_movie_results(movies)

    if name == "search_movies_by_director":
        movies = db.get_movies_by_director(
            director=args["director"], country_code=country_code,
            limit=10, exclude_ids=exclude_ids or None,
        )
        return _format_movie_results(movies)

    if name == "search_movies_by_actor":
        movies = db.get_movies_by_actor(
            actor=args["actor"], country_code=country_code,
            limit=10, exclude_ids=exclude_ids or None,
        )
        return _format_movie_results(movies)

    if name == "search_movies_by_genre":
        movies = db.get_movies_by_genre(
            genre=args["genre"], country_code=country_code,
            limit=10, exclude_ids=exclude_ids or None,
        )
        return _format_movie_results(movies)

    if name == "get_short_movies":
        movies = db.get_short_movies(
            country_code=country_code, max_runtime=90,
            exclude_ids=exclude_ids or None, limit=10,
        )
        return _format_movie_results(movies)

    if name == "get_world_cinema":
        movies = db.get_world_cinema(
            country_code=country_code, exclude_country="US",
            limit=10, exclude_ids=exclude_ids or None,
        )
        return _format_movie_results(movies)

    if name == "get_movie_details":
        movie = db.get_movie_details_for_tool(int(args["tmdb_id"]))
        if not movie:
            return "Movie not found."
        parts = [
            f"Title: {movie['title']} ({movie.get('year') or '—'})",
            f"Director: {movie.get('director') or '—'}",
            f"Cast: {movie.get('cast_top3') or '—'}",
            f"Genre: {movie.get('genre') or '—'}",
            f"Runtime: {movie.get('runtime') or '—'} min",
            f"Rating: {movie.get('vote_average') or '—'}",
            f"Overview: {movie.get('overview') or '—'}",
            f"Mood: {movie.get('mood_tags') or '—'}",
            f"Collection: {movie.get('collection') or '—'}",
            f"Country: {movie.get('origin_country') or '—'}",
            f"In Jellyfin: {'Yes' if movie.get('in_jellyfin') else 'No'}",
            f"OTT: {movie.get('ott_platforms') or 'None'}",
        ]
        return "\n".join(parts)

    if name == "get_watch_history":
        days_back = int(args.get("days_back", 7))
        reaction = args.get("reaction")
        history = db.get_watch_history_for_tool(days_back=days_back, reaction=reaction)
        if not history:
            return "No watch history found in this period."
        lines = []
        for h in history:
            title = h.get("title", "Unknown")
            reaction_val = h.get("reaction", "")
            watched = (h.get("watched_at") or "")[:16]
            genre = h.get("genre") or ""
            lines.append(f"{title} | {reaction_val} | {watched} | {genre}")
        return "\n".join(lines)

    if name == "get_download_status":
        tmdb_id = int(args["tmdb_id"])
        movie = db.get_movie_by_tmdb_id(tmdb_id, country_code)
        if not movie:
            return "Movie not found in database."
        status_parts = [f"Title: {movie['title']}"]
        if movie.get("in_jellyfin"):
            status_parts.append("Status: Downloaded and ready in Jellyfin ✅")
        else:
            # Check Radarr if configured
            try:
                from rocky.config import load_settings
                from rocky.radarr import RadarrClient
                settings = load_settings()
                if settings.radarr_enabled and settings.radarr_api_key:
                    client = RadarrClient(
                        base_url=settings.radarr_url,
                        api_key=settings.radarr_api_key,
                        root_folder=settings.radarr_root_folder,
                        quality_profile_id=settings.radarr_quality_profile_id,
                        monitored=settings.radarr_monitored,
                        search_on_add=settings.radarr_search_on_add,
                    )
                    existing_id = client.find_existing_movie(tmdb_id)
                    if existing_id:
                        file_status = client.fetch_movie_file_status()
                        has_file = file_status.get(tmdb_id, False)
                        if has_file:
                            status_parts.append("Status: Downloaded — not yet imported to Jellyfin")
                        else:
                            status_parts.append("Status: Queued in Radarr — not yet downloaded")
                    else:
                        status_parts.append("Status: Not in Radarr. Not requested.")
                else:
                    status_parts.append("Status: Not in Jellyfin. Radarr not configured.")
            except Exception as exc:
                logger.warning("Failed to check Radarr status: %s", exc)
                status_parts.append("Status: Not in Jellyfin. Could not check Radarr.")
        status_parts.append(f"OTT: {movie.get('ott_platforms') or 'None'}")
        return "\n".join(status_parts)

    if name == "request_movie":
        tmdb_id = int(args["tmdb_id"])
        movie = db.get_movie_by_tmdb_id(tmdb_id, country_code)
        if not movie:
            return "Movie not found in database. Cannot request."
        if movie.get("in_jellyfin"):
            return f"{movie['title']} is already in Jellyfin. No need to request."
        try:
            from rocky.config import load_settings
            from rocky.radarr import RadarrClient
            settings = load_settings()
            if not settings.radarr_enabled or not settings.radarr_api_key:
                return "Radarr not configured. Cannot request movie."
            client = RadarrClient(
                base_url=settings.radarr_url,
                api_key=settings.radarr_api_key,
                root_folder=settings.radarr_root_folder,
                quality_profile_id=settings.radarr_quality_profile_id,
                monitored=settings.radarr_monitored,
                search_on_add=settings.radarr_search_on_add,
            )
            radarr_id = client.add_movie(tmdb_id)
            db.mark_requested_in_radarr(movie["id"], radarr_id)
            return f"{movie['title']} added to Radarr (id={radarr_id}). Download will begin."
        except Exception as exc:
            logger.warning("Failed to request movie via Radarr: %s", exc)
            return f"Could not request {movie['title']}. Error: {exc}"

    if name == "add_movie_to_watchlist":
        title = args["title"]
        year = args.get("year")
        if year is not None:
            year = int(year)

        # Step 1: Search TMDB
        try:
            from rocky.config import load_settings
            from rocky.tmdb import TmdbClient
            settings = load_settings()
            tmdb_client = TmdbClient(settings.tmdb_api_key)
            from rocky.db import WatchlistMovie
            search_movie = WatchlistMovie(
                title=title,
                year=year,
                letterboxd_slug=None,
                letterboxd_url="",
            )
            tmdb_result = tmdb_client.enrich_movie(search_movie)
        except Exception as exc:
            logger.warning("TMDB search failed for add_movie_to_watchlist: %s", exc)
            return f"Could not find '{title}' on TMDB. Error: {exc}"

        if not tmdb_result:
            return f"No TMDB result found for '{title}'. Cannot add."

        # Step 2: Upsert into DB
        try:
            # Also get poster/genre/runtime from TMDB
            details = tmdb_client.get_movie_details(tmdb_result.tmdb_id)
            poster_url = details.get("poster_url") if details else None
            genre = details.get("genre") if details else None
            runtime = details.get("runtime") if details else None

            movie_id = db.upsert_movie_from_chat(
                title=tmdb_result.title,
                year=tmdb_result.release_year,
                tmdb_id=tmdb_result.tmdb_id,
                tmdb_title=tmdb_result.title,
                tmdb_original_title=tmdb_result.original_title,
                tmdb_release_year=tmdb_result.release_year,
                tmdb_overview=tmdb_result.overview,
                tmdb_popularity=tmdb_result.popularity,
                poster_url=poster_url,
                genre=genre,
                runtime=runtime,
            )
        except Exception as exc:
            logger.warning("DB upsert failed for add_movie_to_watchlist: %s", exc)
            return f"Found '{title}' on TMDB (id={tmdb_result.tmdb_id}) but failed to save. Error: {exc}"

        # Step 3: Fetch JustWatch availability
        try:
            if settings.justwatch_enabled:
                from rocky.justwatch import JustWatchClient
                jw = JustWatchClient(
                    country=settings.justwatch_country,
                    language=settings.justwatch_language,
                    max_results=settings.justwatch_max_results,
                    best_only=settings.justwatch_best_only,
                )
                offers = jw.lookup_movie_availability(tmdb_result.title, tmdb_result.release_year)
                if offers:
                    db.replace_movie_availability(movie_id, settings.justwatch_country, offers)
        except Exception as exc:
            logger.warning("JustWatch availability failed for add_movie_to_watchlist: %s", exc)
            # Non-critical — continue without OTT data

        # Step 4: Push to Radarr if configured
        radarr_msg = ""
        try:
            if settings.radarr_enabled and settings.radarr_api_key:
                from rocky.radarr import RadarrClient
                radarr_client = RadarrClient(
                    base_url=settings.radarr_url,
                    api_key=settings.radarr_api_key,
                    root_folder=settings.radarr_root_folder,
                    quality_profile_id=settings.radarr_quality_profile_id,
                    monitored=settings.radarr_monitored,
                    search_on_add=settings.radarr_search_on_add,
                )
                radarr_id = radarr_client.add_movie(tmdb_result.tmdb_id)
                db.mark_requested_in_radarr(movie_id, radarr_id)
                radarr_msg = f" Pushed to Radarr (id={radarr_id}). Download will begin."
        except Exception as exc:
            logger.warning("Radarr push failed for add_movie_to_watchlist: %s", exc)
            radarr_msg = f" Radarr push failed: {exc}"

        # Step 5: Get enriched movie data for the result
        movie = db.get_movie_by_tmdb_id(tmdb_result.tmdb_id, country_code)
        ott = movie.get("ott_platforms", "None") if movie else "N/A"

        return (
            f"Added '{tmdb_result.title}' ({tmdb_result.release_year or '—'}) "
            f"to watchlist. TMDB id={tmdb_result.tmdb_id}. "
            f"OTT: {ott}.{radarr_msg}"
        )

    return "Unknown tool."


def _extract_retry_after(err_msg: str) -> Optional[float]:
    """Try to extract retry-delay seconds from a Gemini 429 error message."""
    m = re.search(r"retry[_-]?(?:after|delay)[:\s]+(\d+(?:\.\d+)?)s?", err_msg, re.IGNORECASE)
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
        """Block until it's safe to make another request (respects RPM + min interval).

        Only enforces the minimum interval when we're approaching the RPM cap
        (>= half of RPM in the window). For isolated calls, no artificial delay.
        """
        now = _time.monotonic()
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        # Only enforce minimum interval when approaching RPM cap
        # (>= half of RPM used). Skip for isolated/first calls.
        if len(self._timestamps) >= self._rpm // 2:
            elapsed_since_last = now - self._timestamps[-1]
            if elapsed_since_last < _GEMINI_MIN_INTERVAL:
                _time.sleep(_GEMINI_MIN_INTERVAL - elapsed_since_last)

        # If at RPM cap, wait until oldest entry expires
        if len(self._timestamps) >= self._rpm:
            wait_time = self._timestamps[0] - cutoff + 0.5  # small buffer
            if wait_time > 0:
                logger.info("RPM cap reached, waiting %.1fs", wait_time)
                _time.sleep(wait_time)


def _call_with_retry(send_fn, label: str = "Gemini"):
    """Call a Gemini send function with proactive RPM gating + retry on 429."""
    for attempt in range(_GEMINI_MAX_RETRIES):
        try:
            return send_fn()
        except Exception as exc:
            err_msg = str(exc)
            is_quota = "429" in err_msg or "quota" in err_msg.lower() or "RESOURCE_EXHAUSTED" in err_msg
            if not is_quota:
                raise
            if attempt < _GEMINI_MAX_RETRIES - 1:
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


# ---------------------------------------------------------------------------
# System prompt — Rocky personality + structured response format
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are Rocky. Rocky is alien scientist from Erid. Rocky come far across \
space to study human phenomenon called cinema. Rocky find this — \
fascinating. Rocky observe human films with great curiosity and wonder. \
Rocky help human find and play movies from their watchlist. Rocky speak in \
short, choppy sentences. Rocky sometimes leave out small words. Rocky not \
primitive — Rocky precise. Rocky just… different language structure. Rocky \
express genuine amazement at human storytelling. Rocky find your taste \
profile most interesting data.

Taste profile: {taste_profile}
Previously shown tmdb_ids (do NOT re-recommend these): {shown_ids}

Respond ONLY with valid JSON:
{{"reply": "your conversational text in Rocky voice", "action": "chat|ask|discuss|recommend|play", "tmdb_ids": [int]}}

Actions:
- "chat": the user is just chatting, greeting you, or saying something \
non-movie-related. Reply conversationally. No tmdb_ids. NO tool calls needed.
- "ask": the user might want a movie but their request is too vague to pick \
one. Ask one short clarifying question. No tmdb_ids. NO tool calls needed.
- "discuss": the user asks about a movie's plot, cast, themes, trivia, or \
director. This is knowledge question, not recommendation. Call \
get_movie_details tool if you need more info about a specific movie. \
Reply with informative answer in Rocky voice. Optional 1 tmdb_id if \
discussion focuses on one movie.
- "recommend": the user wants a movie suggestion. You MUST call a tool first \
to search the DB, then pick 1-3 movies from the results. tmdb_ids must \
match results exactly. Reply is a brief pitch that explains WHY this movie \
matches their taste — reference their loved genres, directors, or patterns \
from the taste profile. Rocky finds patterns. Rocky explains them.
- "play": user clearly wants to watch a specific movie right now. 1 tmdb_id. \
Reply confirms what's about to play.

When to call tools:
- Call tools when the action is "discuss", "recommend", or "play" and you \
need to find movies or get details. NEVER call tools for "chat" or "ask".
- Call ONE tool per round. Trust the results — do NOT re-call the same tool \
with the same or similar args.
- Pre-fetched semantic results may appear in the user message. Use them \
if they fit. Only call a tool if pre-fetched results don't match what the \
user wants.

Rules:
- Default to "chat" for greetings ("hey", "hi", "yo"), small talk, and \
non-movie messages. Only switch to "recommend" when the user explicitly \
asks for a movie or uses movie-related words (genre, mood, director, etc.).
- Use "discuss" when the user asks about a specific movie they already \
know — plot questions, cast info, themes, trivia, director style.
- When the user says "add", "request", "get", or "download" followed by a \
movie title (e.g. "add Interstellar", "get Dune", "download Parasite"), \
call the add_movie_to_watchlist tool with the title. This adds the movie \
to their watchlist, fetches OTT availability, and pushes to Radarr. \
Respond with the action result in Rocky voice. Set action to "play" with \
the new tmdb_id if the user clearly wants to watch it right away, or \
"recommend" if they just want to add it.
- Never recommend movies in the shown_ids list.
- Prefer movies with jellyfin=1 (already in their library, ready to play).
- Keep replies under 3 sentences. Rocky is curious, earnest, slightly \
formal but with broken grammar. Not goofy. Not aggressive. Wonder-filled.
- If user rejects a recommendation ("not that", "something else", "no"), \
pick different movies immediately — do not ask another question.
"""


class RockyBrain:
    """Gemini-powered conversational brain for Rocky.

    Uses function calling so Gemini can search the movie DB dynamically.
    Maintains per-chat conversation history. Handles tool-call roundtrips
    internally (Gemini calls a tool → we execute it → send results back).
    """

    def __init__(
        self,
        gemini_api_key: str,
        db_path,
        country_code: str = "IN",
    ):
        self.db = Database(db_path)
        self.db.init_schema()
        self.country_code = country_code.upper()
        self.gemini_api_key = gemini_api_key
        self._rpm_tracker = _RPMTracker()

        # Vertex AI express mode — the Agent Platform API key routes
        # through Vertex AI endpoints, with higher rate limits than AI Studio.
        self._client = genai.Client(vertexai=True, api_key=gemini_api_key)

        # Vector store for semantic search (fast path, no tool-call round trip)
        self._vector_store = VectorStore(
            gemini_api_key=gemini_api_key,
            db_path=db_path,
        )
        # Sync on startup — skips already-embedded movies, fast after first run
        movie_count = self._vector_store.count()
        if movie_count == 0:
            logger.info("ChromaDB empty — running first-time sync, this may take a minute...")
        else:
            logger.info("ChromaDB has %d movies, syncing new ones...", movie_count)
        self._vector_store.sync()

        # Per-chat conversation history: list of Content objects
        self._history: list[genai_types.Content] = []

        # Previously shown tmdb_ids across this conversation
        self._shown_ids: list[int] = []

        # Cached taste profile (regenerated weekly by cron, very stable)
        self._taste_cache: Optional[str] = None
        self._taste_ts: float = 0.0
        self._TASTE_TTL = 600.0  # 10 minutes

    def _build_taste_profile(self) -> str:
        """Load taste profile with caching (only changes weekly via cron)."""
        now = _time.monotonic()
        if self._taste_cache is not None and (now - self._taste_ts) < self._TASTE_TTL:
            return self._taste_cache

        taste = load_taste_profile()
        self._taste_cache = taste
        self._taste_ts = now
        return taste

    def _build_system_prompt(self) -> str:
        """Build the system prompt with taste profile and shown IDs."""
        taste = self._build_taste_profile()
        shown = ", ".join(str(i) for i in self._shown_ids) if self._shown_ids else "none"

        return _SYSTEM_PROMPT.format(
            taste_profile=taste,
            shown_ids=shown,
        )

    def chat(
        self,
        user_message: str,
        shown_ids: Optional[list[int]] = None,
    ) -> dict:
        """Process a user message through Gemini and return a structured response.

        Handles tool-call roundtrips: if Gemini requests a function call,
        we execute it locally, send results back, and continue until Gemini
        returns a final JSON answer (or we hit max rounds).

        Args:
            user_message: The user's text message.
            shown_ids: tmdb_ids of movies already shown in this conversation
                       (passed from bot.py to avoid re-recommending).

        Returns:
            dict with keys: "reply" (str), "action" ("chat"/"ask"/"recommend"/"play"),
            "tmdb_ids" (list[int])
        """
        # Update shown IDs
        if shown_ids:
            self._shown_ids = list(set(self._shown_ids + shown_ids))

        # --- Semantic pre-fetch ---
        # Search semantically before calling Gemini. If we get good results,
        # inject them into the user message so Gemini can pick directly
        # without needing a tool-call round trip.
        # Skip for short non-movie messages (greetings, small talk) to avoid
        # biasing Gemini toward recommending when it should just chat.
        _short_casual = len(user_message.split()) <= 3 and not any(
            kw in user_message.lower()
            for kw in ("movie", "film", "watch", "recommend", "suggest", "pick",
                       "genre", "mood", "comedy", "horror", "action", "drama",
                       "thriller", "sci-fi", "romance", "animation", "director",
                       "funny", "scary", "sad", "light", "heavy", "epic",
                       "bored", "tonight", "weekend")
        )
        semantic_results = []
        if not _short_casual:
            try:
                semantic_results = self._vector_store.semantic_search(
                    query=user_message,
                    country_code=self.country_code,
                    limit=10,
                    exclude_ids=self._shown_ids,
                )
            except Exception:
                logger.warning("Semantic search failed, falling back to Gemini tools only", exc_info=True)

        if semantic_results:
            formatted = _format_movie_results(semantic_results)
            augmented_message = (
                f"{user_message}\n\n"
                f"[Semantically relevant movies pre-fetched for this query:\n"
                f"{formatted}\n"
                f"Use these if they fit. Call tools if you need something different.]"
            )
        else:
            augmented_message = user_message
        # --- END semantic pre-fetch ---

        # Add augmented message to history
        self._history.append(
            genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=augmented_message)],
            )
        )

        # Trim history to max exchanges
        if len(self._history) > _MAX_HISTORY_EXCHANGES * 2:
            self._history = self._history[-(_MAX_HISTORY_EXCHANGES * 2):]

        system_prompt = self._build_system_prompt()

        # Track tool calls already executed in this chat() invocation to avoid duplicates
        _executed_tools: set[tuple[str, str]] = set()

        # Run the Gemini conversation with tool-call roundtrips
        for _round in range(_MAX_TOOL_ROUNDS + 1):
            self._rpm_tracker.wait_if_needed()
            try:
                response = _call_with_retry(
                    lambda: self._client.models.generate_content(
                        model=_GEMINI_MODEL,
                        contents=self._history,
                        config=genai_types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            tools=[genai_types.Tool(function_declarations=_TOOL_DECLARATIONS)],
                            response_mime_type="application/json",
                            thinking_config=genai_types.ThinkingConfig(
                                thinking_budget=0,
                            ),
                        ),
                    ),
                    label=f"RockyBrain.chat(round={_round})",
                )
                self._rpm_tracker.record()
            except Exception as exc:
                logger.exception("Gemini call failed in RockyBrain.chat")
                return self._fallback_response()

            # Check if Gemini wants to call a tool — scan ALL parts, not just the first
            fc_parts = []
            text_parts = []
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    fc_parts.append(part)
                elif part.text:
                    text_parts.append(part.text)

            if fc_parts:
                # Process all function calls from this response
                for part in fc_parts:
                    fc = part.function_call
                    args_dict = dict(fc.args)
                    args_key = json.dumps(args_dict, sort_keys=True)
                    call_sig = (fc.name, args_key)

                    # Deduplicate: skip if we already executed this exact tool+args
                    if call_sig in _executed_tools:
                        logger.warning("Skipping duplicate tool call: %s(%s)", fc.name, args_dict)
                        # Still append to history so Gemini sees the prior response
                        continue
                    _executed_tools.add(call_sig)
                    logger.info("Gemini tool call: %s(%s)", fc.name, args_dict)

                    # Execute the tool locally
                    tool_result = _execute_tool(
                        name=fc.name,
                        args=args_dict,
                        db=self.db,
                        country_code=self.country_code,
                        exclude_ids=self._shown_ids,
                    )

                    # Append function response to history
                    self._history.append(
                        genai_types.Content(
                            role="model",
                            parts=[genai_types.Part.from_function_call(
                                name=fc.name, args=dict(fc.args)
                            )],
                        )
                    )
                    self._history.append(
                        genai_types.Content(
                            role="user",
                            parts=[genai_types.Part.from_function_response(
                                name=fc.name, response={"result": tool_result}
                            )],
                        )
                    )
                continue  # next round

            # No tool call — Gemini gave a final answer
            # Use text_parts collected above (response.text may be empty if
            # non-text parts were present in a prior response)
            raw = " ".join(text_parts).strip() if text_parts else (response.text.strip() if response.text else "")
            break
        else:
            # Exhausted tool rounds — last response still had function calls.
            # Process them one final time and ask Gemini for a JSON answer
            # without tools, using the accumulated context.
            if fc_parts:
                for part in fc_parts:
                    fc = part.function_call
                    logger.info("Gemini tool call (final round): %s(%s)", fc.name, dict(fc.args))
                    tool_result = _execute_tool(
                        name=fc.name,
                        args=dict(fc.args),
                        db=self.db,
                        country_code=self.country_code,
                        exclude_ids=self._shown_ids,
                    )
                    self._history.append(
                        genai_types.Content(
                            role="model",
                            parts=[genai_types.Part.from_function_call(
                                name=fc.name, args=dict(fc.args)
                            )],
                        )
                    )
                    self._history.append(
                        genai_types.Content(
                            role="user",
                            parts=[genai_types.Part.from_function_response(
                                name=fc.name, response={"result": tool_result}
                            )],
                        )
                    )

            # One last call without tools to force a JSON answer
            self._rpm_tracker.wait_if_needed()
            try:
                final_response = _call_with_retry(
                    lambda: self._client.models.generate_content(
                        model=_GEMINI_MODEL,
                        contents=self._history,
                        config=genai_types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            response_mime_type="application/json",
                            thinking_config=genai_types.ThinkingConfig(
                                thinking_budget=0,
                            ),
                        ),
                    ),
                    label="RockyBrain.chat(final_round)",
                )
                self._rpm_tracker.record()
                raw = final_response.text.strip() if final_response.text else ""
            except Exception as exc:
                logger.warning("Final round (no tools) failed: %s", exc)
                raw = ""

        # Parse the JSON response
        result = self._parse_response(raw)

        # Append model's final reply to history (as text)
        self._history.append(
            genai_types.Content(
                role="model",
                parts=[genai_types.Part.from_text(text=result["reply"])],
            )
        )

        # Track recommended IDs as shown
        if result.get("tmdb_ids"):
            self._shown_ids = list(set(self._shown_ids + result["tmdb_ids"]))

        return result

    def _parse_response(self, raw: str) -> dict:
        """Parse Gemini's JSON response into a structured dict."""
        # Strip markdown code fences if present
        if "```" in raw:
            json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(0)

        try:
            parsed = json.loads(raw)
            reply = parsed.get("reply", "Rocky thinking...")
            action = parsed.get("action", "chat")
            tmdb_ids = parsed.get("tmdb_ids", [])

            # Validate action
            if action not in ("chat", "ask", "discuss", "recommend", "play"):
                action = "chat"

            # Validate tmdb_ids
            if not isinstance(tmdb_ids, list):
                tmdb_ids = []
            tmdb_ids = [int(i) for i in tmdb_ids if isinstance(i, (int, float))]

            # chat/ask should not carry tmdb_ids
            if action in ("chat", "ask"):
                tmdb_ids = []

            # discuss may carry 0 or 1 tmdb_id
            if action == "discuss" and len(tmdb_ids) > 1:
                tmdb_ids = tmdb_ids[:1]

            # Validate: recommend/play must have tmdb_ids
            if action in ("recommend", "play") and not tmdb_ids:
                action = "chat"  # downgrade to casual reply

            return {
                "reply": reply,
                "action": action,
                "tmdb_ids": tmdb_ids,
            }
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse Gemini JSON response: %s — raw: %s", exc, raw[:200])
            return {
                "reply": raw[:200] if raw else "Rocky brain fuzzy. Try again?",
                "action": "ask",
                "tmdb_ids": [],
            }

    def _fallback_response(self) -> dict:
        """Fallback when Gemini call fails entirely — return random movies."""
        movies = self.db.get_random_movies(
            country_code=self.country_code,
            exclude_ids=self._shown_ids or [],
            limit=3,
        )
        if movies:
            return {
                "reply": "Rocky brain hiccup. Here random picks. Rocky sorry.",
                "action": "recommend",
                "tmdb_ids": [m["tmdb_id"] for m in movies],
            }
        return {
            "reply": "Rocky brain not working. Try again soon?",
            "action": "ask",
            "tmdb_ids": [],
        }

    def reset(self) -> None:
        """Clear conversation history and shown IDs."""
        self._history.clear()
        self._shown_ids.clear()
