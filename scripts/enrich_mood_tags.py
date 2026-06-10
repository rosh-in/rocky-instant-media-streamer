#!/usr/bin/env python3
"""Enrich mood tags using Gemini.

Batch script that uses Gemini to generate mood tags for movies that have
empty or genre-derived-only mood_tags. After enrichment, triggers a
ChromaDB resync so the new tags are available for semantic search.

Usage:
    PYTHONPATH=src python scripts/enrich_mood_tags.py [--batch-size 10] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

# Add src to path so rocky modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rocky.config import load_settings
from rocky.db import Database, MOOD_MAP, derive_mood_tags
from rocky.logging_config import setup_logging

from google import genai
from google.genai import types as genai_types

logger = logging.getLogger("rocky.enrich_mood_tags")

_GEMINI_MODEL = "gemini-2.5-flash-lite"


def _get_genre_derived_tags(genre: str) -> str:
    """Return the tags that would be derived mechanically from a genre string."""
    return derive_mood_tags(genre)


def _needs_enrichment(movie: dict) -> bool:
    """Check if a movie needs mood tag enrichment.

    Needs enrichment if:
    - mood_tags is empty/None
    - mood_tags exactly matches the genre-derived tags (no LLM enrichment yet)
    """
    current_tags = movie.get("mood_tags") or ""
    if not current_tags.strip():
        return True

    genre = movie.get("genre") or ""
    derived = _get_genre_derived_tags(genre)
    # If tags are identical to genre-derived, they haven't been LLM-enriched
    if current_tags == derived and current_tags:
        return True

    return False


def _enrich_tags_for_movie(
    client: genai.Client,
    movie: dict,
) -> str | None:
    """Use Gemini to generate mood tags for a single movie.

    Returns comma-separated mood tags, or None on failure.
    """
    title = movie.get("title", "Unknown")
    genre = movie.get("genre") or ""
    overview = movie.get("tmdb_overview") or movie.get("overview") or ""

    prompt = (
        f"Generate 3-5 mood tags for this movie. Mood tags describe the emotional "
        f"experience of watching it, not the genre.\n\n"
        f"Title: {title}\n"
        f"Genre: {genre}\n"
        f"Overview: {overview[:500]}\n\n"
        f"Respond with ONLY the mood tags as a comma-separated list, nothing else. "
        f"Examples: gripping,tense,edge-of-seat | light,fun,escapist | "
        f"emotional,heavy,thought-provoking | mind-bending,cerebral,futuristic | "
        f"heartwarming,tender,bittersweet | dark,disturbing,atmospheric | "
        f"thrilling,fast-paced,adrenaline | warm,feel-good,uplifting | "
        f"suspenseful,twisty,engaging | melancholic,poetic,contemplative"
    )

    try:
        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="text/plain",
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = response.text.strip() if response.text else ""

        # Clean up: remove any markdown formatting, extract just the tags
        if "```" in raw:
            match = re.search(r'```(?:\w*\n)?(.+?)```', raw, re.DOTALL)
            if match:
                raw = match.group(1).strip()

        # Validate: should be comma-separated words/phrases
        tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
        if not tags:
            return None

        # Filter out obviously bad responses
        valid_tags = []
        for tag in tags:
            # Each tag should be 1-3 words, no special chars except hyphens
            if re.match(r'^[a-z]+(-[a-z]+)*$', tag) and len(tag) <= 30:
                valid_tags.append(tag)

        if not valid_tags:
            return None

        return ",".join(valid_tags)

    except Exception as exc:
        logger.warning("Failed to enrich tags for '%s': %s", title, exc)
        return None


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Enrich mood tags using Gemini")
    parser.add_argument("--batch-size", type=int, default=10, help="Movies per batch (default: 10)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB, just show what would change")
    args = parser.parse_args()

    settings = load_settings()

    if not settings.gemini_api_key:
        print("GEMINI_API_KEY not set. Required for mood tag enrichment.")
        sys.exit(1)

    db = Database(settings.sqlite_path)
    db.init_schema()

    # Get all movies with tmdb_id (enrichment needs the movie in the DB)
    all_movies = db.get_all_movies()

    # Filter to movies needing enrichment
    movies_to_enrich = [m for m in all_movies if _needs_enrichment(m)]

    if not movies_to_enrich:
        print("All movies already have enriched mood tags. Nothing to do.")
        return

    print(f"Found {len(movies_to_enrich)} movies needing mood tag enrichment.")

    client = genai.Client(vertexai=True, api_key=settings.gemini_api_key)

    enriched_count = 0
    failed_count = 0

    for i, movie in enumerate(movies_to_enrich):
        title = movie.get("title", "Unknown")
        current_tags = movie.get("mood_tags") or ""

        print(f"[{i + 1}/{len(movies_to_enrich)}] Enriching: {title} (current: {current_tags or 'empty'})")

        new_tags = _enrich_tags_for_movie(client, movie)

        if new_tags is None:
            print(f"  ❌ Failed to generate tags")
            failed_count += 1
            continue

        print(f"  ✅ New tags: {new_tags}")

        if not args.dry_run:
            # Get the movie's DB id from tmdb_id
            movie_detail = db.get_movie_by_tmdb_id(movie["tmdb_id"], settings.justwatch_country)
            if movie_detail:
                db.update_movie_enrichment(movie_detail["id"], mood_tags=new_tags)
                enriched_count += 1
            else:
                print(f"  ⚠️ Movie not found by tmdb_id, skipping DB update")
                failed_count += 1
        else:
            print(f"  (dry-run, not writing to DB)")
            enriched_count += 1

        # Rate limit: ~4s between requests to stay within 15 RPM
        if (i + 1) % args.batch_size == 0:
            print(f"  Pausing for rate limit after {args.batch_size} movies...")
            time.sleep(5)
        else:
            time.sleep(1)  # Small delay between individual requests

    print(f"\nEnrichment complete: {enriched_count} enriched, {failed_count} failed")

    if enriched_count > 0 and not args.dry_run:
        print("\nTriggering ChromaDB resync with enriched tags...")
        try:
            from rocky.vector_store import VectorStore
            store = VectorStore(
                gemini_api_key=settings.gemini_api_key,
                db_path=settings.sqlite_path,
            )
            new_count = store.sync()
            print(f"ChromaDB resync complete: {new_count} new/updated embeddings")
        except Exception as exc:
            logger.warning("ChromaDB resync failed: %s", exc)
            print(f"ChromaDB resync failed: {exc}")
            print("You can resync manually by restarting the bot.")


if __name__ == "__main__":
    main()
