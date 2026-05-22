"""Gemini-powered movie recommendation engine with SQLite context injection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import google.generativeai as genai

from project_toto.db import Database

logger = logging.getLogger("project_toto.gemini")

SYSTEM_PROMPT = """\
You are a movie recommendation assistant for a personal home media system.

You have access to the user's Letterboxd watchlist (movies they want to watch) \
and OTT availability data (which streaming platforms carry each title). Your \
job is to suggest movies from their watchlist that match their current mood.

Rules:
- Recommend 2-3 movies from the provided watchlist that best match the mood.
- For each recommendation, give a 1-2 sentence reason tied to the mood.
- If a movie is available on an OTT platform, mention it (e.g., "Streaming on Netflix").
- If no watchlist movies match the mood well, say so honestly and suggest \
broadening the mood or picking something adjacent.
- Keep responses concise and formatted for a Telegram chat.
"""


@dataclass(frozen=True)
class Recommendation:
    title: str
    year: Optional[int]
    reason: str
    ott_platform: Optional[str]


def _build_context(db: Database, country_code: str = "IN") -> str:
    """Build a context string from SQLite with watchlist + OTT availability."""
    with db._connect() as conn:
        movies = conn.execute(
            """
            SELECT m.title, m.year, m.tmdb_overview, m.tmdb_popularity,
                   GROUP_CONCAT(
                       a.provider_name || '(' || a.monetization_type || ')',
                       ', '
                   ) AS availability
            FROM movies m
            LEFT JOIN availability a
                ON a.movie_id = m.id AND a.country_code = ?
            WHERE m.tmdb_id IS NOT NULL
            GROUP BY m.id
            ORDER BY m.tmdb_popularity DESC
            LIMIT 50
            """,
            (country_code.upper(),),
        ).fetchall()

    if not movies:
        return "No movies in the watchlist yet."

    lines = ["## Watchlist Movies:\n"]
    for row in movies:
        year_str = f" ({row['year']})" if row["year"] else ""
        overview = (row["tmdb_overview"] or "")[:120]
        avail = row["availability"] or "Not on OTT"
        lines.append(f"- **{row['title']}{year_str}** — {overview} | OTT: {avail}")

    return "\n".join(lines)


def recommend(
    api_key: str,
    db: Database,
    mood: str,
    country_code: str = "IN",
) -> list[Recommendation]:
    """Get mood-based movie recommendations from Gemini.

    Returns a list of Recommendation objects parsed from the Gemini response.
    """
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
model_name="gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT,
    )

    context = _build_context(db, country_code)

    prompt = f"""\
{context}

---

The user's mood right now: **{mood}**

Based on the watchlist above, recommend 2-3 movies that match this mood. \
For each movie, provide:
- Title and year
- A brief reason (1-2 sentences) tied to the mood
- OTT availability if listed

Format each recommendation as:
TITLE: <title> (<year>)
REASON: <reason>
OTT: <platform or "Local library">
---
"""
    response = model.generate_content(prompt)
    text = response.text

    # Parse structured recommendations from the response
    recommendations: list[Recommendation] = []
    blocks = text.split("---")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        title = None
        year = None
        reason = None
        ott = None
        for line in block.splitlines():
            line = line.strip()
            if line.upper().startswith("TITLE:"):
                raw = line[len("TITLE:"):].strip()
                # Extract year from parentheses, e.g. "Inception (2010)"
                if "(" in raw and ")" in raw:
                    year_str = raw[raw.rindex("(") + 1 : raw.rindex(")")]
                    try:
                        year = int(year_str)
                    except ValueError:
                        pass
                    raw = raw[: raw.rindex("(")].strip()
                title = raw
            elif line.upper().startswith("REASON:"):
                reason = line[len("REASON:"):].strip()
            elif line.upper().startswith("OTT:"):
                ott = line[len("OTT:"):].strip()
        if title:
            recommendations.append(
                Recommendation(
                    title=title,
                    year=year,
                    reason=reason or "Matches your mood.",
                    ott_platform=ott,
                )
            )

    # If parsing failed, return a single raw-text recommendation
    if not recommendations and text.strip():
        recommendations.append(
            Recommendation(title="(see full response)", year=None, reason=text.strip()[:500], ott_platform=None)
        )

    logger.info("Gemini returned %d recommendations for mood '%s'", len(recommendations), mood)
    return recommendations


def format_recommendations(recs: list[Recommendation]) -> str:
    """Format recommendations for Telegram display."""
    if not recs:
        return "No recommendations found. Try a different mood!"

    lines = []
    for i, r in enumerate(recs, 1):
        year_str = f" ({r.year})" if r.year else ""
        ott_str = f"\n  📺 {r.ott_platform}" if r.ott_platform else ""
        lines.append(f"{i}. 🎬 *{r.title}{year_str}*\n  💡 {r.reason}{ott_str}")

    return "\n\n".join(lines)
