from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

logger = logging.getLogger("rocky.radarr")


class RadarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        root_folder: str,
        quality_profile_id: int,
        monitored: bool = True,
        search_on_add: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.root_folder = root_folder
        self.quality_profile_id = quality_profile_id
        self.monitored = monitored
        self.search_on_add = search_on_add

        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            }
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.get(f"{self.base_url}{path}", timeout=30, **kwargs)
        response.raise_for_status()
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _post(self, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.post(f"{self.base_url}{path}", timeout=30, **kwargs)
        response.raise_for_status()
        return response

    def find_existing_movie(self, tmdb_id: int) -> Optional[int]:
        response = self._get("/api/v3/movie", params={"tmdbId": tmdb_id})
        payload = response.json()
        if isinstance(payload, list) and payload:
            movie_id = payload[0].get("id")
            if isinstance(movie_id, int):
                return movie_id
        return None

    def _lookup_tmdb(self, tmdb_id: int) -> dict[str, Any]:
        response = self._get("/api/v3/movie/lookup/tmdb", params={"tmdbId": tmdb_id})
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected Radarr lookup response for tmdbId={tmdb_id}")
        return payload

    def add_movie(self, tmdb_id: int) -> int:
        existing_id = self.find_existing_movie(tmdb_id)
        if existing_id is not None:
            return existing_id

        lookup = self._lookup_tmdb(tmdb_id)
        lookup["rootFolderPath"] = self.root_folder
        lookup["qualityProfileId"] = self.quality_profile_id
        lookup["monitored"] = self.monitored
        lookup["addOptions"] = {"searchForMovie": self.search_on_add}

        created = self._post("/api/v3/movie", json=lookup).json()
        movie_id = created.get("id")
        if not isinstance(movie_id, int):
            raise ValueError(f"Radarr did not return a valid movie id for tmdbId={tmdb_id}")
        return movie_id

    def fetch_movie_file_status(self) -> dict[int, bool]:
        """Fetch all movies from Radarr and return a mapping of tmdbId -> hasFile.

        hasFile=True means Radarr has downloaded and imported a file for that movie,
        i.e. it is on disk and ready for Jellyfin to pick up.
        """
        response = self._get("/api/v3/movie")
        payload = response.json()
        if not isinstance(payload, list):
            logger.warning("Unexpected Radarr movie list response")
            return {}
        status: dict[int, bool] = {}
        for movie in payload:
            tmdb_id = movie.get("tmdbId")
            if isinstance(tmdb_id, int):
                status[tmdb_id] = bool(movie.get("hasFile", False))
        logger.info("Fetched file status for %d movies from Radarr", len(status))
        return status
