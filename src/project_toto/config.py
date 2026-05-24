import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    letterboxd_username: str
    tmdb_api_key: str
    sqlite_path: Path
    letterboxd_max_pages: int
    radarr_enabled: bool
    radarr_url: str
    radarr_api_key: Optional[str]
    radarr_root_folder: str
    radarr_quality_profile_id: int
    radarr_monitored: bool
    radarr_search_on_add: bool
    radarr_dry_run: bool
    justwatch_enabled: bool
    justwatch_country: str
    justwatch_language: str
    justwatch_refresh_hours: int
    justwatch_max_results: int
    justwatch_best_only: bool
    jellyfin_url: str
    jellyfin_api_key: Optional[str]
    jellyfin_username: str
    telegram_bot_token: Optional[str]
    gemini_api_key: Optional[str]
    telegram_allowed_user_ids: frozenset[int]
    telegram_allowed_chat_ids: frozenset[int]
    telegram_rate_limit_window_seconds: int
    telegram_rate_limit_max_messages: int


def _as_bool(value: str, default: bool = False) -> bool:
    cleaned = (value or "").strip().lower()
    if cleaned == "":
        return default
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _as_int_set(value: str, var_name: str) -> frozenset[int]:
    cleaned = (value or "").strip()
    if not cleaned:
        return frozenset()

    values: set[int] = set()
    for part in cleaned.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise ValueError(f"{var_name} must be a comma-separated list of integers.") from exc
    return frozenset(values)


def load_settings() -> Settings:
    load_dotenv()

    username = os.getenv("LETTERBOXD_USERNAME", "").strip()
    tmdb_api_key = os.getenv("TMDB_API_KEY", "").strip()
    sqlite_path = Path(os.getenv("SQLITE_PATH", "data/project_toto.db"))
    max_pages_raw = os.getenv("LETTERBOXD_MAX_PAGES", "5").strip()
    radarr_enabled = _as_bool(os.getenv("RADARR_ENABLED", "false"), default=False)
    radarr_url = os.getenv("RADARR_URL", "http://localhost:7878").strip().rstrip("/")
    radarr_api_key = os.getenv("RADARR_API_KEY", "").strip() or None
    radarr_root_folder = os.getenv("RADARR_ROOT_FOLDER", "/data/media/movies").strip()
    radarr_quality_profile_id_raw = os.getenv("RADARR_QUALITY_PROFILE_ID", "1").strip()
    radarr_monitored = _as_bool(os.getenv("RADARR_MONITORED", "true"), default=True)
    radarr_search_on_add = _as_bool(os.getenv("RADARR_SEARCH_ON_ADD", "false"), default=False)
    radarr_dry_run = _as_bool(os.getenv("RADARR_DRY_RUN", "true"), default=True)
    justwatch_enabled = _as_bool(os.getenv("JUSTWATCH_ENABLED", "true"), default=True)
    justwatch_country = os.getenv("JUSTWATCH_COUNTRY", "IN").strip().upper()
    justwatch_language = os.getenv("JUSTWATCH_LANGUAGE", "en").strip()
    justwatch_refresh_hours_raw = os.getenv("JUSTWATCH_REFRESH_HOURS", "168").strip()
    justwatch_max_results_raw = os.getenv("JUSTWATCH_MAX_RESULTS", "3").strip()
    justwatch_best_only = _as_bool(os.getenv("JUSTWATCH_BEST_ONLY", "true"), default=True)
    jellyfin_url = os.getenv("JELLYFIN_URL", "http://localhost:8096").strip().rstrip("/")
    jellyfin_api_key = os.getenv("JELLYFIN_API_KEY", "").strip() or None
    jellyfin_username = os.getenv("JELLYFIN_USERNAME", "").strip()
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    telegram_allowed_user_ids = _as_int_set(
        os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""),
        "TELEGRAM_ALLOWED_USER_IDS",
    )
    telegram_allowed_chat_ids = _as_int_set(
        os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        "TELEGRAM_ALLOWED_CHAT_IDS",
    )
    telegram_rate_limit_window_seconds_raw = os.getenv(
        "TELEGRAM_RATE_LIMIT_WINDOW_SECONDS",
        "20",
    ).strip()
    telegram_rate_limit_max_messages_raw = os.getenv(
        "TELEGRAM_RATE_LIMIT_MAX_MESSAGES",
        "8",
    ).strip()

    if not username:
        raise ValueError("Missing LETTERBOXD_USERNAME in environment.")
    if not tmdb_api_key:
        raise ValueError("Missing TMDB_API_KEY in environment.")
    if radarr_enabled and not radarr_api_key:
        raise ValueError("RADARR_ENABLED is true but RADARR_API_KEY is missing.")

    try:
        max_pages = int(max_pages_raw)
    except ValueError as exc:
        raise ValueError("LETTERBOXD_MAX_PAGES must be an integer.") from exc
    try:
        radarr_quality_profile_id = int(radarr_quality_profile_id_raw)
    except ValueError as exc:
        raise ValueError("RADARR_QUALITY_PROFILE_ID must be an integer.") from exc
    try:
        justwatch_refresh_hours = int(justwatch_refresh_hours_raw)
    except ValueError as exc:
        raise ValueError("JUSTWATCH_REFRESH_HOURS must be an integer.") from exc
    try:
        justwatch_max_results = int(justwatch_max_results_raw)
    except ValueError as exc:
        raise ValueError("JUSTWATCH_MAX_RESULTS must be an integer.") from exc
    try:
        telegram_rate_limit_window_seconds = int(telegram_rate_limit_window_seconds_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_RATE_LIMIT_WINDOW_SECONDS must be an integer.") from exc
    try:
        telegram_rate_limit_max_messages = int(telegram_rate_limit_max_messages_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_RATE_LIMIT_MAX_MESSAGES must be an integer.") from exc

    if max_pages < 1:
        raise ValueError("LETTERBOXD_MAX_PAGES must be >= 1.")
    if radarr_quality_profile_id < 1:
        raise ValueError("RADARR_QUALITY_PROFILE_ID must be >= 1.")
    if len(justwatch_country) != 2:
        raise ValueError("JUSTWATCH_COUNTRY must be a 2-letter country code.")
    if justwatch_refresh_hours < 0:
        raise ValueError("JUSTWATCH_REFRESH_HOURS must be >= 0.")
    if justwatch_max_results < 1:
        raise ValueError("JUSTWATCH_MAX_RESULTS must be >= 1.")
    if telegram_rate_limit_window_seconds < 0:
        raise ValueError("TELEGRAM_RATE_LIMIT_WINDOW_SECONDS must be >= 0.")
    if telegram_rate_limit_max_messages < 0:
        raise ValueError("TELEGRAM_RATE_LIMIT_MAX_MESSAGES must be >= 0.")

    return Settings(
        letterboxd_username=username,
        tmdb_api_key=tmdb_api_key,
        sqlite_path=sqlite_path,
        letterboxd_max_pages=max_pages,
        radarr_enabled=radarr_enabled,
        radarr_url=radarr_url,
        radarr_api_key=radarr_api_key,
        radarr_root_folder=radarr_root_folder,
        radarr_quality_profile_id=radarr_quality_profile_id,
        radarr_monitored=radarr_monitored,
        radarr_search_on_add=radarr_search_on_add,
        radarr_dry_run=radarr_dry_run,
        justwatch_enabled=justwatch_enabled,
        justwatch_country=justwatch_country,
        justwatch_language=justwatch_language,
        justwatch_refresh_hours=justwatch_refresh_hours,
        justwatch_max_results=justwatch_max_results,
        justwatch_best_only=justwatch_best_only,
        jellyfin_url=jellyfin_url,
        jellyfin_api_key=jellyfin_api_key,
        jellyfin_username=jellyfin_username,
        telegram_bot_token=telegram_bot_token,
        gemini_api_key=gemini_api_key,
        telegram_allowed_user_ids=telegram_allowed_user_ids,
        telegram_allowed_chat_ids=telegram_allowed_chat_ids,
        telegram_rate_limit_window_seconds=telegram_rate_limit_window_seconds,
        telegram_rate_limit_max_messages=telegram_rate_limit_max_messages,
    )
