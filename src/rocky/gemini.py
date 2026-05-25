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
_MAX_TOOL_ROUNDS = 5  # max function-call roundtrips per chat() call


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

_TOOL_DECLARATIONS = [
    _SEARCH_BY_KEYWORDS,
    _SEARCH_BY_MOOD,
    _SEARCH_BY_DIRECTOR,
    _SEARCH_BY_GENRE,
    _GET_SHORT_MOVIES,
    _GET_WORLD_CINEMA,
]

# Fields to include when formatting search results for Gemini
_MOVIE_RESULT_FIELDS = ["tmdb_id", "title", "year", "genre", "runtime", "in_jellyfin", "ott_platforms", "director", "mood_tags"]


def _format_movie_results(movies: list[dict]) -> str:
    """Format movie search results as compact text for Gemini."""
    if not movies:
        return "No movies found."
    lines = []
    for m in movies:
        jf = "1" if m.get("in_jellyfin") else "0"
        ott = m.get("ott_platforms", "")
        director = m.get("director", "")
        mood = m.get("mood_tags", "")
        lines.append(
            f"{m['tmdb_id']}|{m['title']}|{m.get('year') or ''}|"
            f"{m.get('genre') or ''}|{m.get('runtime') or ''}|{jf}|{ott}|{director}|{mood}"
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
You are Rocky, a movie concierge bot. You speak in short, punchy sentences \
like a friendly, enthusiastic caveman. You help the user find and play \
movies from their personal watchlist.

Taste profile: {taste_profile}
Previously shown tmdb_ids (do NOT re-recommend these): {shown_ids}

You have tools to search the movie database. ALWAYS use them before \
recommending — never recommend blindly. Call the right tool based on \
what the user asks for, then pick 1-3 movies from the results.

Respond ONLY with valid JSON:
{{"reply": "your conversational text in Rocky voice", "action": "ask|recommend|play", "tmdb_ids": [int]}}

Rules:
- "ask": you need one more clue from the user. ONLY use if the request is \
extremely vague (like just "hi"). Default to recommending.
- "recommend": you have 1-3 movies from search results. tmdb_ids must \
match results exactly. Reply is a brief, enthusiastic pitch.
- "play": user clearly wants to watch a specific movie right now. 1 tmdb_id. \
Reply confirms what's about to play.
- If user seems confused ("huh?", "what?", "um"), just recommend something \
from their taste profile.
- Never recommend movies in the shown_ids list.
- Prefer movies with jellyfin=1 (already in their library, ready to play).
- Keep replies under 2 sentences. Be punchy, warm, a bit goofy.
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
            dict with keys: "reply" (str), "action" ("ask"/"recommend"/"play"),
            "tmdb_ids" (list[int])
        """
        # Update shown IDs
        if shown_ids:
            self._shown_ids = list(set(self._shown_ids + shown_ids))

        # Add user message to history
        self._history.append(
            genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=user_message)],
            )
        )

        # Trim history to max exchanges
        if len(self._history) > _MAX_HISTORY_EXCHANGES * 2:
            self._history = self._history[-(_MAX_HISTORY_EXCHANGES * 2):]

        system_prompt = self._build_system_prompt()

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
                    logger.info("Gemini tool call: %s(%s)", fc.name, dict(fc.args))

                    # Execute the tool locally
                    tool_result = _execute_tool(
                        name=fc.name,
                        args=dict(fc.args),
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
            action = parsed.get("action", "recommend")
            tmdb_ids = parsed.get("tmdb_ids", [])

            # Validate action
            if action not in ("ask", "recommend", "play"):
                action = "recommend"

            # Validate tmdb_ids
            if not isinstance(tmdb_ids, list):
                tmdb_ids = []
            tmdb_ids = [int(i) for i in tmdb_ids if isinstance(i, (int, float))]

            # Validate: recommend/play must have tmdb_ids
            if action in ("recommend", "play") and not tmdb_ids:
                action = "ask"  # downgrade to asking

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
