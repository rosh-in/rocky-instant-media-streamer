"""Watchlist progress card generator — pure SQL stats from the local DB."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def generate_stats(db_path: Path, country_code: str = "IN") -> str:
    """Generate a formatted watchlist progress card from the database.

    Returns a Markdown-formatted string ready to send via Telegram.
    """
    from project_toto.db import Database

    db = Database(db_path)
    db.init_schema()

    with db._connect() as conn:
        # Total watchlist
        total = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL").fetchone()[0]

        # In Jellyfin (has_file = 1 means Radarr downloaded & imported the file)
        in_jellyfin = conn.execute(
            "SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL AND has_file = 1"
        ).fetchone()[0]

        # OTT only (not in Jellyfin but has availability)
        on_ott_only = conn.execute(
            """
            SELECT COUNT(DISTINCT m.id)
            FROM movies m
            WHERE m.tmdb_id IS NOT NULL
              AND m.has_file = 0
              AND EXISTS (
                  SELECT 1 FROM availability a
                  WHERE a.movie_id = m.id AND a.country_code = ?
              )
            """,
            (country_code.upper(),),
        ).fetchone()[0]

        # Not available (not in Jellyfin and no availability)
        not_available = conn.execute(
            """
            SELECT COUNT(DISTINCT m.id)
            FROM movies m
            WHERE m.tmdb_id IS NOT NULL
              AND m.has_file = 0
              AND NOT EXISTS (
                  SELECT 1 FROM availability a
                  WHERE a.movie_id = m.id AND a.country_code = ?
              )
            """,
            (country_code.upper(),),
        ).fetchone()[0]

        # Oldest unwatched
        oldest = conn.execute(
            "SELECT title, year FROM movies WHERE tmdb_id IS NOT NULL ORDER BY year ASC LIMIT 1"
        ).fetchone()

        # Recently added
        recent = conn.execute(
            "SELECT title FROM movies WHERE tmdb_id IS NOT NULL ORDER BY created_at DESC LIMIT 3"
        ).fetchall()

        # Top genre
        top_genre_row = conn.execute(
            """
            SELECT genre, COUNT(*) as c
            FROM movies
            WHERE tmdb_id IS NOT NULL AND genre IS NOT NULL
            GROUP BY genre
            ORDER BY c DESC
            LIMIT 1
            """
        ).fetchone()

    # Build the message
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%b %d, %Y")

    oldest_text = f"{oldest['title']} ({oldest['year']})" if oldest else "—"
    recent_text = ", ".join(r["title"] for r in recent) if recent else "—"
    top_genre = top_genre_row["genre"] if top_genre_row else "—"

    return (
        f"📊 *Your Watchlist — Week of {date_str}*\n\n"
        f"📋 Total: {total} films\n"
        f"✅ Ready in Jellyfin: {in_jellyfin}\n"
        f"🎬 OTT only: {on_ott_only}\n"
        f"❌ Not available yet: {not_available}\n\n"
        f"🕰 Oldest unwatched: {oldest_text}\n"
        f"➕ Recently added: {recent_text}\n"
        f"🎭 Top genre: {top_genre}\n\n"
        f"_{in_jellyfin} films ready to watch right now._"
    )
