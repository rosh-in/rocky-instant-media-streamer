from __future__ import annotations

from typing import List, Optional, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from project_toto.db import WatchlistMovie

LETTERBOXD_BASE = "https://letterboxd.com"


def _parse_year(text: str) -> Optional[int]:
    text = (text or "").strip()
    if len(text) == 4 and text.isdigit():
        return int(text)
    return None


def fetch_watchlist(username: str, max_pages: int = 5) -> List[WatchlistMovie]:
    seen_keys: Set[str] = set()
    movies: List[WatchlistMovie] = []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
            )
        }
    )

    for page in range(1, max_pages + 1):
        url = f"{LETTERBOXD_BASE}/{username}/watchlist/"
        if page > 1:
            url = f"{LETTERBOXD_BASE}/{username}/watchlist/page/{page}/"

        response = session.get(url, timeout=20)
        if response.status_code == 404 and page == 1:
            raise ValueError(f"Letterboxd watchlist not found for user: {username}")
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        poster_items = soup.select("li.poster-container")
        if not poster_items:
            break

        for item in poster_items:
            div = item.select_one("div.film-poster")
            if not div:
                continue

            slug = div.get("data-film-slug")
            film_id = div.get("data-film-id")
            link_el = item.select_one("div.film-poster a")
            title_el = item.select_one("img.image")
            year_el = item.select_one("p.poster-viewingdata")

            title = ""
            if title_el and title_el.get("alt"):
                title = title_el.get("alt", "").strip()
            if not title and link_el and link_el.get("href"):
                title = link_el.get("href", "").strip("/").split("/")[-1].replace("-", " ").title()
            if not title:
                continue

            href = link_el.get("href") if link_el else ""
            if not href:
                continue
            letterboxd_url = urljoin(LETTERBOXD_BASE, href)

            year = _parse_year(year_el.get_text(strip=True) if year_el else "")
            dedupe_key = slug or film_id or f"{title.lower()}::{year or 'na'}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            movies.append(
                WatchlistMovie(
                    title=title,
                    year=year,
                    letterboxd_slug=slug,
                    letterboxd_url=letterboxd_url,
                )
            )

    return movies
