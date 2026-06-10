"""Watchlist progress card generator — formatted text stats from the local DB."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def generate_stats(db_path: Path, country_code: str = "IN") -> str:
    """Generate a formatted watchlist progress card from the database.

    Returns a Markdown-formatted string ready to send via Telegram.
    """
    from rocky.db import Database

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

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%b %d, %Y")
    top_genre = top_genre_row["genre"] if top_genre_row else "—"
    top_genre = top_genre.replace("/", " · ")

    # MarkdownV2 requires escaping these characters: _ * [ ] ( ) ~ ` > # + - = | { } . !
    def _esc(s: str) -> str:
        for ch in ("_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
            s = s.replace(ch, f"\\{ch}")
        return s

    date_esc = _esc(date_str)
    genre_esc = _esc(top_genre)

    return (
        f"🪨 *Rocky\\'s Weekly Report*\n"
        f"_Week of {date_esc}_\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 *{total}* movies tracked\n"
        f"✅ *{in_jellyfin}* ready to watch right now\n"
        f"🎬 *{on_ott_only}* OTT only\n"
        f"⏳ *{not_available}* not yet available\n"
        f"━━━━━━━━━━━━━━━\n\n\n"
        f"🎭 *Top genre:* {genre_esc}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"_Rocky has spoken\\._"
    )
