from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

from project_toto.db import TmdbMovie, WatchlistMovie

logger = logging.getLogger("project_toto.tmdb")


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
