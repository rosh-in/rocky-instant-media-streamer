"""Jellyfin API client for device discovery and playback control."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("rocky.jellyfin")


@dataclass(frozen=True)
class JellyfinDevice:
    session_id: str
    device_name: str
    device_id: str
    client: str
    supports_remote_control: bool

    @property
    def label(self) -> str:
        return f"{self.device_name} ({self.client})"


@dataclass(frozen=True)
class JellyfinMovie:
    item_id: str
    name: str
    year: Optional[int]
    overview: str


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, username: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username

        self.session = requests.Session()
        self.session.headers.update({"Authorization": f'MediaBrowser Token="{api_key}"'})

        self._user_id: Optional[str] = None

    # -- HTTP helpers with retry ------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(
            (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        response = self.session.get(
            f"{self.base_url}/{path.lstrip('/')}", params=params, timeout=30
        )
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(
            (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _post(self, path: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
        response = self.session.post(
            f"{self.base_url}/{path.lstrip('/')}", params=params, timeout=30
        )
        response.raise_for_status()
        return response

    # -- User resolution --------------------------------------------------------

    def _resolve_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        users = self._get("Users")
        for user in users:
            if user.get("Name", "").lower() == self.username.lower():
                self._user_id = user["Id"]
                return self._user_id
        raise ValueError(f"Jellyfin user '{self.username}' not found.")

    # -- Public API -------------------------------------------------------------

    def list_devices(self) -> list[JellyfinDevice]:
        """Return active sessions that support remote control."""
        user_id = self._resolve_user_id()
        sessions = self._get("Sessions", params={"ControllableByUserId": user_id})
        devices: list[JellyfinDevice] = []
        for s in sessions:
            if not s.get("SupportsRemoteControl", False):
                continue
            devices.append(
                JellyfinDevice(
                    session_id=s["Id"],
                    device_name=s.get("DeviceName", "Unknown"),
                    device_id=s.get("DeviceId", ""),
                    client=s.get("Client", ""),
                    supports_remote_control=True,
                )
            )
        return devices

    def search_movies(self, query: str, limit: int = 10) -> list[JellyfinMovie]:
        """Search the Jellyfin library for movies matching *query*."""
        user_id = self._resolve_user_id()
        data = self._get(
            f"Users/{user_id}/Items",
            params={
                "searchTerm": query,
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Limit": limit,
                "Fields": "Overview",
            },
        )
        movies: list[JellyfinMovie] = []
        for item in data.get("Items", []):
            year = item.get("ProductionYear")
            movies.append(
                JellyfinMovie(
                    item_id=item["Id"],
                    name=item.get("Name", ""),
                    year=int(year) if year else None,
                    overview=(item.get("Overview") or "")[:200],
                )
            )
        return movies

    def play(self, session_id: str, item_id: str) -> None:
        """Instruct a session to play a specific item immediately."""
        self._post(
            f"Sessions/{session_id}/Playing",
            params={"playCommand": "PlayNow", "itemIds": item_id},
        )
        logger.info("Play command sent to session %s for item %s", session_id, item_id)

    def pause(self, session_id: str) -> None:
        """Pause playback on a session."""
        self._post(f"Sessions/{session_id}/Playing/Pause")

    def stop(self, session_id: str) -> None:
        """Stop playback on a session."""
        self._post(f"Sessions/{session_id}/Playing/Stop")
