"""Taste profile generator for Gemini translator context.

Reads watch_history, derives patterns (loved/liked/disliked genres,
countries, directors), and caches the result as a text file.
Regenerated weekly alongside the sync cron.
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Optional

from rocky.db import Database

logger = logging.getLogger("rocky.taste_profile")

# Default path for the cached taste profile
DEFAULT_PROFILE_PATH = Path("taste_profile.txt")


def _extract_top_genres(movies: list[dict], limit: int = 3) -> str:
    """Extract the most common genres from a list of watched movie dicts."""
    counter: Counter[str] = Counter()
    for m in movies:
        genre = m.get("genre") or ""
        for part in genre.replace("/", ",").split(","):
            g = part.strip()
            if g:
                counter[g] += 1
    top = [g for g, _ in counter.most_common(limit)]
    return ", ".join(top) if top else "varied"


def _extract_top_countries(movies: list[dict], limit: int = 3) -> str:
    """Extract the most common origin countries from a list of watched movie dicts."""
    counter: Counter[str] = Counter()
    for m in movies:
        country = m.get("origin_country") or ""
        if country:
            counter[country] += 1
    top = [c for c, _ in counter.most_common(limit)]
    return ", ".join(top) if top else "varied"


def _extract_top_directors(movies: list[dict], limit: int = 3) -> str:
    """Extract the most common directors from a list of watched movie dicts."""
    counter: Counter[str] = Counter()
    for m in movies:
        director = m.get("director") or ""
        if director:
            counter[director] += 1
    top = [d for d, _ in counter.most_common(limit)]
    return ", ".join(top) if top else "varied"


def _format_titles(movies: list[dict]) -> str:
    """Format movie titles with year for the taste profile text."""
    parts = []
    for m in movies:
        title = m.get("title", "Unknown")
        year = m.get("year")
        if year:
            parts.append(f"{title} ({year})")
        else:
            parts.append(title)
    return ", ".join(parts) if parts else "none yet"


def generate_taste_profile(
    db_path: Path,
    profile_path: Optional[Path] = None,
) -> str:
    """Generate a taste profile from watch_history and cache it.

    Returns the profile text string.
    """
    db = Database(db_path)

    loved = db.get_watched_movies(reaction="loved", limit=10)
    liked = db.get_watched_movies(reaction="liked", limit=10)
    disliked = db.get_watched_movies(reaction="disliked", limit=5)

    if not loved and not liked and not disliked:
        profile = "No watch history yet. Recommend varied options."
    else:
        loved_genres = _extract_top_genres(loved)
        loved_countries = _extract_top_countries(loved)
        loved_directors = _extract_top_directors(loved)
        disliked_genres = _extract_top_genres(disliked)

        profile_lines = [
            f"Recently loved: {_format_titles(loved[:5])}",
            f"Recently liked: {_format_titles(liked[:3])}",
            f"Recently disliked: {_format_titles(disliked[:3])}",
            f"Patterns: favours {loved_genres}, {loved_countries} cinema. "
            f"Top directors: {loved_directors}. "
            f"Tends to avoid: {disliked_genres}.",
        ]
        profile = "\n".join(profile_lines)

    # Cache to file
    cache_path = profile_path or DEFAULT_PROFILE_PATH
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(profile, encoding="utf-8")
        logger.info("Taste profile cached to %s", cache_path)
    except Exception as exc:
        logger.warning("Failed to cache taste profile: %s", exc)

    return profile


def load_taste_profile(profile_path: Optional[Path] = None) -> str:
    """Load the cached taste profile. Returns placeholder if not found."""
    cache_path = profile_path or DEFAULT_PROFILE_PATH
    try:
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return "No watch history yet. Recommend varied options."
