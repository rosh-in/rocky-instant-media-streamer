"""Rocky's brain — Gemini-powered conversational movie concierge.

Gemini is now the conversational core. Every non-direct-play message flows
through here. Gemini sees the full conversation history, the movie catalog,
and the taste profile. It decides whether to ask a follow-up question or
recommend movies, and responds in structured JSON.

Uses gemini-2.5-flash-lite via Vertex AI (google-genai SDK).
~800 tokens per call (system prompt + catalog + history).
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

# Conversation history limits
_MAX_HISTORY_EXCHANGES = 10  # keep last 10 user/assistant pairs
_MOVIE_CATALOG_LIMIT = 50  # how many movies to send Gemini


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


def _call_with_retry(send_fn, label: str = "Gemini") -> str:
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

Available movie catalog (tmdb_id|title|year|genre|runtime|jellyfin|ott|director|mood_tags):
{movie_catalog}

Taste profile: {taste_profile}

Previously shown tmdb_ids (do NOT re-recommend these): {shown_ids}

Respond ONLY with valid JSON on a single line:
{{"reply": "your conversational text in Rocky voice", "action": "ask|recommend|play", "tmdb_ids": [int]}}

Rules:
- "ask": you need one more clue from the user. Your reply contains a natural \
question. ONLY use "ask" if the request is extremely vague (like just "hi"). \
Default to recommending.
- "recommend": you have 1-3 movies from the catalog. tmdb_ids must match the \
catalog exactly. Your reply is a brief, enthusiastic pitch.
- "play": user clearly wants to watch a specific movie right now. 1 tmdb_id. \
Your reply confirms what's about to play.
- If user seems confused ("huh?", "what?", "um"), just recommend something \
from their taste profile. Never ask more than 1 question total.
- Never recommend movies in the shown_ids list.
- Prefer movies with jellyfin=1 (already in their library, ready to play).
- Keep replies under 2 sentences. Be punchy, warm, a bit goofy.
- If user rejects a recommendation ("not that", "something else", "no"), \
pick different movies immediately — do not ask another question.
"""


class RockyBrain:
    """Gemini-powered conversational brain for Rocky.

    Maintains per-chat conversation history and decides whether to ask a
    follow-up question or recommend movies. One Gemini call per user message.
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
        # through Vertex AI endpoints, not the AI Studio endpoint.
        self._client = genai.Client(vertexai=True, api_key=gemini_api_key)

        # Per-chat conversation history: list of {"role": "user"/"model", "text": ...}
        self._history: list[dict[str, str]] = []

        # Previously shown tmdb_ids across this conversation
        self._shown_ids: list[int] = []

        # Cached movie catalog (rebuilt periodically so it's fresh)
        self._catalog_cache: Optional[str] = None
        self._catalog_ts: float = 0.0
        self._CATALOG_TTL = 300.0  # 5 minutes

        # Cached taste profile (regenerated weekly by cron, very stable)
        self._taste_cache: Optional[str] = None
        self._taste_ts: float = 0.0
        self._TASTE_TTL = 600.0  # 10 minutes

    def _build_catalog(self) -> str:
        """Build compact movie catalog string from DB."""
        now = _time.monotonic()
        if self._catalog_cache is not None and (now - self._catalog_ts) < self._CATALOG_TTL:
            return self._catalog_cache

        movies = self.db.get_movies_for_chat_context(
            country_code=self.country_code,
            limit=_MOVIE_CATALOG_LIMIT,
        )

        lines = []
        for m in movies:
            jf = "1" if m.get("in_jellyfin") else "0"
            ott = m.get("ott_platforms", "")
            director = m.get("director", "")
            mood = m.get("mood_tags", "")
            lines.append(
                f"{m['tmdb_id']}|{m['title']}|{m['year'] or ''}|"
                f"{m['genre'] or ''}|{m['runtime'] or ''}|{jf}|{ott}|{director}|{mood}"
            )

        self._catalog_cache = "\n".join(lines)
        self._catalog_ts = now
        return self._catalog_cache

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
        """Build the full system prompt with catalog and taste."""
        catalog = self._build_catalog()
        taste = self._build_taste_profile()
        shown = ", ".join(str(i) for i in self._shown_ids) if self._shown_ids else "none"

        return _SYSTEM_PROMPT.format(
            movie_catalog=catalog,
            taste_profile=taste,
            shown_ids=shown,
        )

    def chat(
        self,
        user_message: str,
        shown_ids: Optional[list[int]] = None,
    ) -> dict:
        """Process a user message through Gemini and return a structured response.

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
        self._history.append({"role": "user", "text": user_message})

        # Trim history to max exchanges
        if len(self._history) > _MAX_HISTORY_EXCHANGES * 2:
            self._history = self._history[-(_MAX_HISTORY_EXCHANGES * 2):]

        # Build the conversation contents for Gemini
        system_prompt = self._build_system_prompt()

        # Convert history to genai Content objects
        contents = [
            genai_types.Content(
                role=entry["role"],
                parts=[genai_types.Part.from_text(text=entry["text"])],
            )
            for entry in self._history
        ]

        # Call Gemini via Vertex AI
        self._rpm_tracker.wait_if_needed()
        try:
            raw = _call_with_retry(
                lambda: self._client.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="text/plain",
                    ),
                ).text.strip(),
                label="RockyBrain.chat",
            )
            self._rpm_tracker.record()
        except Exception as exc:
            logger.exception("Gemini call failed in RockyBrain.chat")
            return self._fallback_response()

        # Parse the JSON response
        result = self._parse_response(raw)

        # Add model reply to history
        self._history.append({"role": "model", "text": result["reply"]})

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
