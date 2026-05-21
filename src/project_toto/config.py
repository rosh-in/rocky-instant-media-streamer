import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    letterboxd_username: str
    tmdb_api_key: str
    sqlite_path: Path
    letterboxd_max_pages: int


def load_settings() -> Settings:
    load_dotenv()

    username = os.getenv("LETTERBOXD_USERNAME", "").strip()
    tmdb_api_key = os.getenv("TMDB_API_KEY", "").strip()
    sqlite_path = Path(os.getenv("SQLITE_PATH", "data/project_toto.db"))
    max_pages_raw = os.getenv("LETTERBOXD_MAX_PAGES", "5").strip()

    if not username:
        raise ValueError("Missing LETTERBOXD_USERNAME in environment.")
    if not tmdb_api_key:
        raise ValueError("Missing TMDB_API_KEY in environment.")

    try:
        max_pages = int(max_pages_raw)
    except ValueError as exc:
        raise ValueError("LETTERBOXD_MAX_PAGES must be an integer.") from exc

    if max_pages < 1:
        raise ValueError("LETTERBOXD_MAX_PAGES must be >= 1.")

    return Settings(
        letterboxd_username=username,
        tmdb_api_key=tmdb_api_key,
        sqlite_path=sqlite_path,
        letterboxd_max_pages=max_pages,
    )
