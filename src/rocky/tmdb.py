from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

from rocky.db import TmdbMovie, WatchlistMovie

logger = logging.getLogger("rocky.tmdb")

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


class TmdbClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.base_url = "https://api.themoviedb.org/3"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _search_movie(self, title: str, year: Optional[int]) -> Dict[str, Any]:
        params = {
            "api_key": self.api_key,
            "query": title,
            "include_adult": "false",
            "language": "en-US",
            "page": 1,
        }
        if year:
            params["year"] = year

        response = self.session.get(f"{self.base_url}/search/movie", params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def enrich_movie(self, movie: WatchlistMovie) -> Optional[TmdbMovie]:
        payload = self._search_movie(movie.title, movie.year)
        results = payload.get("results", [])
        if not results:
            if movie.year:
                payload = self._search_movie(movie.title, None)
                results = payload.get("results", [])
            if not results:
                return None

        best = results[0]
        release_year = None
        release_date = (best.get("release_date") or "").strip()
        if len(release_date) >= 4 and release_date[:4].isdigit():
            release_year = int(release_date[:4])

        return TmdbMovie(
            tmdb_id=int(best["id"]),
            title=(best.get("title") or "").strip() or movie.title,
            original_title=(best.get("original_title") or "").strip() or movie.title,
            release_year=release_year,
            overview=(best.get("overview") or "").strip(),
            popularity=float(best.get("popularity") or 0.0),
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get_movie_details(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch full movie details from /movie/{id} endpoint."""
        params = {
            "api_key": self.api_key,
            "language": "en-US",
        }
        response = self.session.get(
            f"{self.base_url}/movie/{tmdb_id}", params=params, timeout=20
        )
        response.raise_for_status()
        return response.json()

    def get_movie_details(self, tmdb_id: int) -> Optional[dict]:
        """Fetch poster_url, genre, and runtime for a movie by TMDB ID.

        Returns a dict with keys: poster_url, genre, runtime.
        Returns None if the API call fails entirely.
        """
        try:
            data = self._get_movie_details(tmdb_id)
        except Exception:
            logger.warning("Failed to fetch TMDB details for id=%s", tmdb_id)
            return None

        # Poster URL
        poster_path = data.get("poster_path")
        poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None

        # Genre names joined by slash
        genre_names = "/".join(
            g["name"] for g in data.get("genres", []) if g.get("name")
        ) or None

        # Runtime in minutes
        runtime = data.get("runtime")
        if runtime is not None:
            try:
                runtime = int(runtime)
            except (ValueError, TypeError):
                runtime = None

        return {
            "poster_url": poster_url,
            "genre": genre_names,
            "runtime": runtime,
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get_movie_videos(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch videos for a movie from /movie/{id}/videos endpoint."""
        params = {
            "api_key": self.api_key,
            "language": "en-US",
        }
        response = self.session.get(
            f"{self.base_url}/movie/{tmdb_id}/videos", params=params, timeout=20
        )
        response.raise_for_status()
        return response.json()

    def get_trailer_key(self, tmdb_id: int) -> Optional[str]:
        """Fetch the YouTube trailer key for a movie by TMDB ID.

        Returns the YouTube video key (e.g. 'dQw4w9WgXcQ') or None.
        """
        try:
            data = self._get_movie_videos(tmdb_id)
        except Exception:
            logger.warning("Failed to fetch TMDB videos for id=%s", tmdb_id)
            return None

        for video in data.get("results", []):
            if video.get("type") == "Trailer" and video.get("site") == "YouTube":
                key = video.get("key")
                if key:
                    return key
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get_movie_keywords(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch keywords for a movie from /movie/{id}/keywords endpoint."""
        params = {
            "api_key": self.api_key,
        }
        response = self.session.get(
            f"{self.base_url}/movie/{tmdb_id}/keywords", params=params, timeout=20
        )
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get_movie_credits(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch credits for a movie from /movie/{id}/credits endpoint."""
        params = {
            "api_key": self.api_key,
            "language": "en-US",
        }
        response = self.session.get(
            f"{self.base_url}/movie/{tmdb_id}/credits", params=params, timeout=20
        )
        response.raise_for_status()
        return response.json()

    def get_movie_enrichment(self, tmdb_id: int) -> Optional[dict]:
        """Fetch enriched metadata for a movie by TMDB ID.

        Returns a dict with keys: keywords, vote_average, director, cast_top3,
        collection, origin_country.
        Returns None if the API call fails entirely.
        """
        try:
            # Get extended details (includes vote_average, collection, origin_country)
            data = self._get_movie_details(tmdb_id)
        except Exception:
            logger.warning("Failed to fetch TMDB details for enrichment id=%s", tmdb_id)
            return None

        # Keywords
        keywords_str = None
        try:
            kw_data = self._get_movie_keywords(tmdb_id)
            kw_names = [k["name"] for k in kw_data.get("keywords", []) if k.get("name")]
            keywords_str = ",".join(kw_names[:5]) if kw_names else None
        except Exception:
            logger.warning("Failed to fetch keywords for id=%s", tmdb_id)

        # Credits (director + top 3 cast)
        director = None
        cast_top3 = None
        try:
            credits = self._get_movie_credits(tmdb_id)
            # Director from crew
            for member in credits.get("crew", []):
                if member.get("job") == "Director" and member.get("name"):
                    director = member["name"]
                    break
            # Top 3 cast
            cast_names = [
                c["name"] for c in credits.get("cast", [])[:3] if c.get("name")
            ]
            cast_top3 = ",".join(cast_names) if cast_names else None
        except Exception:
            logger.warning("Failed to fetch credits for id=%s", tmdb_id)

        # Vote average
        vote_average = data.get("vote_average")
        if vote_average is not None:
            try:
                vote_average = float(vote_average)
            except (ValueError, TypeError):
                vote_average = None

        # Collection (franchise)
        collection = None
        coll_data = data.get("belongs_to_collection")
        if coll_data and coll_data.get("name"):
            collection = coll_data["name"]

        # Origin country
        origin_country = None
        countries = data.get("origin_country", [])
        if countries:
            origin_country = countries[0]

        return {
            "keywords": keywords_str,
            "vote_average": vote_average,
            "director": director,
            "cast_top3": cast_top3,
            "collection": collection,
            "origin_country": origin_country,
        }
