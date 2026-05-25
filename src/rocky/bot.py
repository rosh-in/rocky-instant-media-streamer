"""Rocky — Telegram bot. Rocky find movie you want watch. Visual movie concierge with inline buttons."""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import (
    CallbackQueryHandler,
    MessageHandler,
    MessageReactionHandler,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
    filters,
)

from rocky.config import Settings, load_settings
from rocky.db import Database
from rocky.gemini import MovieConcierge, ChatConcierge, availability_label
from rocky.jellyfin import JellyfinClient
from rocky.stats import generate_stats
from rocky.taste_profile import generate_taste_profile
from rocky.tmdb import TmdbClient

logger = logging.getLogger("rocky.bot")

# Per-chat concierge instances (conversation memory)
_concierges: dict[int, MovieConcierge] = {}
_concierge_last_used: dict[int, float] = {}
_chat_concierges: dict[int, ChatConcierge] = {}
_chat_concierge_last_used: dict[int, float] = {}
_chat_locks: dict[int, asyncio.Lock] = {}

# Play session storage for inline device buttons
_play_sessions: dict[int, dict] = {}
_play_counter: int = 0
_rate_limit_events: dict[int, deque[float]] = {}

_CONCIERGE_TTL_SECONDS = 24 * 60 * 60
_PLAY_SESSION_TTL_SECONDS = 15 * 60
_MAX_PLAY_SESSIONS = 500
_BLOCKING_CALL_TIMEOUT_SECONDS = 45

_FALLBACK_POSTER = "https://placehold.co/500x750/1a1a2e/eee?text=No+Poster"

# Reaction emoji → reaction value mapping for watch_history
REACTION_MAP = {
    "❤️": "loved",
    "🔥": "loved",
    "👍": "liked",
    "😐": "neutral",
    "👎": "disliked",
    "🤮": "abandoned",
}

# Persistent mood keyboard (Feature 6)
MOOD_MAP = {
    "🎭 Heavy": "heavy emotional drama",
    "😂 Light": "light fun comedy",
    "😱 Thriller": "thriller suspense",
    "🌍 World Cinema": "non-english world cinema foreign film",
}
MOOD_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🎭 Heavy", "😂 Light", "😱 Thriller"],
        ["🌍 World Cinema", "⚡ Under 90m", "🎲 Surprise me"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def _get_concierge(chat_id: int, settings: Settings) -> MovieConcierge:
    """Get or create a concierge for this chat (preserves conversation memory)."""
    _cleanup_concierges()
    concierge = _concierges.get(chat_id)
    if concierge is None:
        _concierges[chat_id] = MovieConcierge(
            gemini_api_key=settings.gemini_api_key,
            db_path=settings.sqlite_path,
            jellyfin_url=settings.jellyfin_url,
            jellyfin_api_key=settings.jellyfin_api_key,
            jellyfin_username=settings.jellyfin_username,
            country_code=settings.justwatch_country,
        )
        concierge = _concierges[chat_id]
    _concierge_last_used[chat_id] = time.time()
    return concierge


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    """Return a per-chat lock so one chat turn is processed at a time."""
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


def _cleanup_concierges(now_ts: Optional[float] = None) -> None:
    """Drop stale concierge instances to avoid unbounded memory growth."""
    now = now_ts if now_ts is not None else time.time()
    stale_chat_ids = [
        chat_id
        for chat_id, last_used in _concierge_last_used.items()
        if (now - last_used) > _CONCIERGE_TTL_SECONDS
    ]
    for chat_id in stale_chat_ids:
        _concierges.pop(chat_id, None)
        _concierge_last_used.pop(chat_id, None)

    # Also clean up chat concierges
    stale_chat_ids = [
        chat_id
        for chat_id, last_used in _chat_concierge_last_used.items()
        if (now - last_used) > _CONCIERGE_TTL_SECONDS
    ]
    for chat_id in stale_chat_ids:
        _chat_concierges.pop(chat_id, None)
        _chat_concierge_last_used.pop(chat_id, None)


def _cleanup_play_sessions(now_ts: Optional[float] = None) -> None:
    """Expire old play sessions and enforce an upper bound on stored sessions."""
    now = now_ts if now_ts is not None else time.time()
    expired_keys = [
        key
        for key, data in _play_sessions.items()
        if (now - float(data.get("created_at", 0.0))) > _PLAY_SESSION_TTL_SECONDS
    ]
    for key in expired_keys:
        _play_sessions.pop(key, None)

    overflow = len(_play_sessions) - _MAX_PLAY_SESSIONS
    if overflow > 0:
        oldest_keys = sorted(
            _play_sessions,
            key=lambda k: float(_play_sessions[k].get("created_at", 0.0)),
        )[:overflow]
        for key in oldest_keys:
            _play_sessions.pop(key, None)


def _cleanup_rate_limit_events(settings: Settings, now_ts: Optional[float] = None) -> None:
    """Prune stale rate-limit timestamps and empty buckets."""
    window = settings.telegram_rate_limit_window_seconds
    if window <= 0 or settings.telegram_rate_limit_max_messages <= 0:
        _rate_limit_events.clear()
        return

    now = now_ts if now_ts is not None else time.time()
    cutoff = now - window
    stale_keys: list[int] = []

    for key, timestamps in _rate_limit_events.items():
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        if not timestamps:
            stale_keys.append(key)

    for key in stale_keys:
        _rate_limit_events.pop(key, None)


def _cleanup_state(settings: Settings) -> None:
    _cleanup_concierges()
    _cleanup_play_sessions()
    _cleanup_rate_limit_events(settings=settings)


def _is_authorized(update: Update, settings: Settings) -> bool:
    """Allow all requests unless allowlists are configured."""
    if not settings.telegram_allowed_user_ids and not settings.telegram_allowed_chat_ids:
        return True

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if settings.telegram_allowed_user_ids and (
        user_id is None or user_id not in settings.telegram_allowed_user_ids
    ):
        return False
    if settings.telegram_allowed_chat_ids and (
        chat_id is None or chat_id not in settings.telegram_allowed_chat_ids
    ):
        return False
    return True


def _rate_limit_key(update: Update) -> Optional[int]:
    """Use user ID when possible, fallback to chat ID."""
    if update.effective_user:
        return int(update.effective_user.id)
    if update.effective_chat:
        return int(update.effective_chat.id)
    return None


def _is_rate_limited(update: Update, settings: Settings) -> bool:
    """Simple sliding-window limiter per user/chat."""
    max_messages = settings.telegram_rate_limit_max_messages
    window = settings.telegram_rate_limit_window_seconds
    if max_messages <= 0 or window <= 0:
        return False

    key = _rate_limit_key(update)
    if key is None:
        return False

    now = time.time()
    bucket = _rate_limit_events.setdefault(key, deque())
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= max_messages:
        return True

    bucket.append(now)
    return False


async def _notify_denied(update: Update) -> None:
    """Send a minimal denial response for unauthorized access."""
    if update.callback_query:
        await update.callback_query.answer("Rocky not know you. Bot private.", show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text("Rocky not know you. Bot private.")


async def _notify_rate_limited(update: Update) -> None:
    """Send a short message when request rate exceeds the configured threshold."""
    text = "Too many request. Rocky need moment. Wait, try again."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def _guard_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    apply_rate_limit: bool = True,
) -> bool:
    """Authorize, rate-limit, and cleanup in-memory state for each update."""
    settings: Settings = context.bot_data["settings"]
    _cleanup_state(settings)

    if not _is_authorized(update, settings):
        logger.warning(
            "Unauthorized bot access denied: user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        await _notify_denied(update)
        return False

    if apply_rate_limit and _is_rate_limited(update, settings):
        await _notify_rate_limited(update)
        return False

    return True


def _get_chat_concierge(chat_id: int, settings: Settings) -> ChatConcierge:
    """Get or create a chat concierge for this chat (preserves conversation memory)."""
    _cleanup_concierges()
    concierge = _chat_concierges.get(chat_id)
    if concierge is None:
        _chat_concierges[chat_id] = ChatConcierge(
            gemini_api_key=settings.gemini_api_key,
            db_path=settings.sqlite_path,
            country_code=settings.justwatch_country,
        )
        concierge = _chat_concierges[chat_id]
    _chat_concierge_last_used[chat_id] = time.time()
    return concierge


def _friendly_error_message(exc: Exception, fallback: str) -> str:
    """Map internal exceptions to safe user-facing messages."""
    msg = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError):
        return "Rocky wait too long. Try again?"
    if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
        return "Rocky brain full right now. Wait moment, try again."
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in msg:
        return "Service timeout. Rocky try again later, yes?"
    if isinstance(exc, requests.exceptions.ConnectionError) or "connection" in msg:
        return "Rocky cannot reach service. Try again soon."
    if "jellyfin user" in msg and "not found" in msg:
        return "Jellyfin user not found. Check JELLYFIN_USERNAME."
    return fallback


def _get_sync_status_snapshot(settings: Settings) -> dict:
    """Read latest sync status directly from the DB."""
    db = Database(settings.sqlite_path)
    db.init_schema()
    with db._connect() as conn:
        last_run = conn.execute(
            "SELECT started_at, status, items_seen, items_enriched, items_availability_refreshed, items_requested "
            "FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL").fetchone()[0]
        unrequested = conn.execute(
            "SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL AND requested_in_radarr = 0"
        ).fetchone()[0]

    result = {"total_movies": total, "unrequested": unrequested}
    if last_run:
        result["last_sync"] = last_run["started_at"][:19]
        result["status"] = last_run["status"]
        result["items_seen"] = last_run["items_seen"]
        result["items_enriched"] = last_run["items_enriched"]
    return result


# ---------------------------------------------------------------------------
# Visual recommendation helpers
# ---------------------------------------------------------------------------

async def send_recommendations(update: Update, context: ContextTypes.DEFAULT_TYPE, movies: list[dict]) -> None:
    """Send 3 movies as a media group with formatted caption and inline buttons."""

    # Build the caption (shown under the media group)
    caption_lines = []
    for i, m in enumerate(movies, 1):
        avail = availability_label(m)
        genre = m.get("genre") or "—"
        runtime = m.get("runtime") or "—"
        year = m.get("year") or "—"
        caption_lines.append(
            f"*{i}. {m['title']} ({year})*\n"
            f"🎬 {genre} • {runtime}m\n"
            f"{avail}"
        )
    caption = "\n\n".join(caption_lines)

    # Build media group — caption only on first photo
    media = []
    for i, m in enumerate(movies):
        poster = m.get("poster_url") or _FALLBACK_POSTER
        media.append(InputMediaPhoto(
            media=poster,
            caption=caption if i == 0 else "",
            parse_mode="Markdown",
        ))

    chat_id = update.effective_chat.id

    # Send the media group
    try:
        await context.bot.send_media_group(
            chat_id=chat_id,
            media=media,
        )
    except Exception as exc:
        logger.warning("Media group send failed, falling back to text: %s", exc)
        # Fallback to text-only if posters fail
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="Markdown",
        )

    # Send inline buttons as a separate message
    num_buttons_row = []
    for i, m in enumerate(movies):
        label = m["title"][:15]
        num_buttons_row.append(InlineKeyboardButton(
            f"{i + 1}\ufe0f\u20e3 {label}",
            callback_data=json.dumps({"action": "pick", "idx": i}),
        ))

    keyboard = [num_buttons_row]

    # Trailer buttons row (Feature 4)
    # Fetch trailer keys on-demand if missing from DB
    trailer_buttons = []
    for i, m in enumerate(movies):
        trailer_key = m.get("trailer_key")
        if not trailer_key and m.get("tmdb_id"):
            # Try fetching from TMDB API on-demand
            try:
                settings: Settings = context.bot_data["settings"]
                tmdb = TmdbClient(settings.tmdb_api_key)
                trailer_key = await asyncio.wait_for(
                    asyncio.to_thread(tmdb.get_trailer_key, m["tmdb_id"]),
                    timeout=10,
                )
                if trailer_key:
                    # Cache in DB for future use
                    db = Database(settings.sqlite_path)
                    db.init_schema()
                    await asyncio.wait_for(
                        asyncio.to_thread(db.update_trailer_key, m["id"], trailer_key),
                        timeout=5,
                    )
                    m["trailer_key"] = trailer_key
            except Exception as exc:
                logger.warning("On-demand trailer fetch failed for tmdb_id=%s: %s", m.get("tmdb_id"), exc)
        if trailer_key:
            trailer_buttons.append(InlineKeyboardButton(
                f"▶ Trailer {i + 1}",
                url=f"https://www.youtube.com/watch?v={trailer_key}",
            ))
    if trailer_buttons:
        keyboard.append(trailer_buttons)

    # Show others button
    if len(movies) >= 3:
        keyboard.append([
            InlineKeyboardButton("🔀 Show others", callback_data=json.dumps({"action": "shuffle"})),
        ])

    await context.bot.send_message(
        chat_id=chat_id,
    text="Pick one. Watch trailer. Or shuffle for more:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Store current recommendations in context for callback use
    context.user_data["current_recommendations"] = movies
    context.user_data["last_shown_genres"] = [m.get("genre", "") for m in movies]


async def send_device_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, movie: dict) -> None:
    """Show device selection buttons after user picks a movie."""
    context.user_data["selected_movie"] = movie

    settings: Settings = context.bot_data["settings"]
    query = update.callback_query

    # Fetch actual Jellyfin devices
    devices = []
    try:
        client = JellyfinClient(
            base_url=settings.jellyfin_url,
            api_key=settings.jellyfin_api_key,
            username=settings.jellyfin_username,
        )
        device_list = await asyncio.wait_for(
            asyncio.to_thread(client.list_devices),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        devices = [
            {"label": d.label, "session_id": d.session_id}
            for d in device_list
        ]
    except Exception as exc:
        logger.warning("Failed to list devices for picker: %s", exc)

    if not devices:
        if movie.get("in_jellyfin"):
            await query.edit_message_text(
                f"*{movie['title']}* is in your library but no devices are active. Open Jellyfin on a device first.",
                parse_mode="Markdown",
            )
        else:
            platforms = movie.get("ott_platforms", "")
            await query.edit_message_text(
                f"*{movie['title']}* isn't in Jellyfin.\nAvailable on: {platforms}",
                parse_mode="Markdown",
            )
        return

    # Store device list for callback use
    global _play_counter
    _play_counter += 1
    play_key = _play_counter
    _play_sessions[play_key] = {
        "movie_name": movie["title"],
        "item_id": None,  # Will be resolved when searching Jellyfin
        "devices": devices,
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id if update.effective_user else None,
        "created_at": time.time(),
    }
    _cleanup_play_sessions()
    context.user_data["play_key"] = play_key

    # Build device buttons with emoji labels
    device_emoji_map = {"tv": "📺", "phone": "📱", "iphone": "📱", "ipad": "📱",
                        "chrome": "💻", "browser": "💻", "laptop": "💻", "desktop": "💻"}
    buttons = []
    for i, dev in enumerate(devices):
        # Pick emoji based on device name
        dev_lower = dev["label"].lower()
        emoji = "📺"  # default
        for key, em in device_emoji_map.items():
            if key in dev_lower:
                emoji = em
                break
        buttons.append(InlineKeyboardButton(
            f"{emoji} {dev['label']}",
            callback_data=json.dumps({"action": "device", "play_key": play_key, "dev_idx": i}),
        ))

    # Split into rows of 2
    button_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

    keyboard = InlineKeyboardMarkup(button_rows)
    await query.edit_message_text(
        f"*{movie['title']}* — where you want watch?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with persistent mood keyboard."""
    if not await _guard_update(update, context):
        return
    text = (
        "Rocky online.\n\n"
        "Rocky = movie friend. Rocky find movie you want watch.\n"
        "Rocky play movie on TV or phone or laptop.\n"
        "Rocky remember what you like. Rocky get better over time.\n\n"
        "Tell Rocky:\n"
        "— Mood (\"something sad\" or \"make me laugh\")\n"
        "— Movie name (\"play Inception\")\n"
        "— Or just talk. Rocky understand.\n\n"
        "Fist bump. Begin?"
    )
    await update.message.reply_text(text, reply_markup=MOOD_KEYBOARD)


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter chat mode — free conversation with Gemini."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    if not settings.gemini_api_key:
        await update.message.reply_text("GEMINI_API_KEY not configured. Rocky brain missing.")
        return

    # Reset any existing chat session
    chat_id = update.effective_chat.id
    existing = _chat_concierges.get(chat_id)
    if existing:
        existing.reset()

    context.user_data["chat_mode"] = True
    context.user_data["chat_history"] = []

    # Send welcome with exit button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Exit chat mode", callback_data=json.dumps({"action": "exit_chat"}))]
    ])
    await update.message.reply_text(
        "Rocky have many movie. You have one evening. Rocky help choose.\n"
        "Describe feeling. Rocky translate to movie. Rocky good at this.",
        reply_markup=keyboard,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset conversation memory."""
    if not await _guard_update(update, context):
        return
    chat_id = update.effective_chat.id
    lock = _get_chat_lock(chat_id)
    async with lock:
        concierge = _concierges.pop(chat_id, None)
        _concierge_last_used.pop(chat_id, None)
        if concierge:
            concierge.reset()
        for key, session in list(_play_sessions.items()):
            if session.get("chat_id") == chat_id:
                _play_sessions.pop(key, None)
    # Also clear user_data
    context.user_data.clear()
    await update.message.reply_text("Rocky forget everything. Fresh start. — wipes brain —")


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active Jellyfin devices (power-user shortcut)."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    if not settings.jellyfin_api_key or not settings.jellyfin_username:
        await update.message.reply_text("Jellyfin not configured yet. Rocky need this.")
        return
    try:
        client = JellyfinClient(
            base_url=settings.jellyfin_url,
            api_key=settings.jellyfin_api_key,
            username=settings.jellyfin_username,
        )
        devices = await asyncio.wait_for(
            asyncio.to_thread(client.list_devices),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        if not devices:
            await update.message.reply_text("No active device. Open Jellyfin on device first.")
            return
        lines = [f"Active device ({len(devices)}):\n"]
        for d in devices:
            lines.append(f"  • {d.label}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Device listing failed")
        await update.message.reply_text(
            _friendly_error_message(exc, "Rocky cannot list device right now. Try again.")
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show sync status (power-user shortcut)."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(_get_sync_status_snapshot, settings),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        lines = ["Rocky check library status:\n"]
        if "last_sync" in status:
            lines.append(f"Last sync: {status['last_sync']}")
            lines.append(f"Status: {status['status']}")
        lines.append(f"Total movie: {status['total_movies']}")
        lines.append(f"Not yet requested: {status['unrequested']}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Status request failed")
        await update.message.reply_text(
            _friendly_error_message(exc, "Rocky cannot fetch status right now. Try again.")
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show watchlist progress card (Feature 2)."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    try:
        stats_text = await asyncio.wait_for(
            asyncio.to_thread(
                generate_stats,
                settings.sqlite_path,
                settings.justwatch_country,
            ),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        await update.message.reply_text(stats_text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Stats generation failed")
        await update.message.reply_text(
            _friendly_error_message(exc, "Rocky cannot count things right now. Try again.")
        )


async def send_weekly_stats(bot, settings: Settings) -> None:
    """Send the weekly watchlist progress card to the configured chat (Feature 2)."""
    chat_id = settings.telegram_chat_id
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set — skipping weekly stats")
        return
    try:
        stats_text = await asyncio.wait_for(
            asyncio.to_thread(
                generate_stats,
                settings.sqlite_path,
                settings.justwatch_country,
            ),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        await bot.send_message(chat_id=chat_id, text=stats_text, parse_mode="Markdown")
        logger.info("Weekly stats card sent to chat_id=%s", chat_id)
    except Exception as exc:
        logger.exception("Weekly stats send failed")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main conversational handler — routes to chat mode or recommendations."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id
    lock = _get_chat_lock(chat_id)

    if not settings.gemini_api_key:
        await update.message.reply_text("GEMINI_API_KEY not configured. Rocky brain missing.")
        return

    user_text = update.message.text or ""

    # Chat mode — route to conversational handler
    if context.user_data.get("chat_mode"):
        await _handle_chat_message(update, context, user_text)
        return

    # Feature 6: Intercept mood keyboard labels
    if user_text in MOOD_MAP:
        # Mapped moods — pass the expanded string through the normal Gemini flow
        user_text = MOOD_MAP[user_text]
    elif user_text == "⚡ Under 90m":
        # Pure SQLite — skip Gemini entirely
        async with lock:
            await update.message.reply_text("Rocky search short film...")
            db = Database(settings.sqlite_path)
            db.init_schema()
            movies = await asyncio.wait_for(
                asyncio.to_thread(
                    db.get_short_movies,
                    country_code=settings.justwatch_country,
                    max_runtime=90,
                    exclude_ids=context.user_data.get("seen_ids", []),
                    limit=20,
                ),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            if not movies:
                await update.message.reply_text("No short film in watchlist right now. Rocky sad.")
                return
            # Pick top 3 (already sorted Jellyfin-first)
            movies = movies[:3]
            context.user_data["seen_ids"] = context.user_data.get("seen_ids", [])
            context.user_data["seen_ids"].extend([m["tmdb_id"] for m in movies])
            context.user_data["last_shown_genres"] = [m.get("genre", "") for m in movies]
            context.user_data["last_intent"] = "short films under 90 minutes"
            await send_recommendations(update, context, movies)
        return
    elif user_text == "🎲 Surprise me":
        # Pure SQLite random — skip Gemini entirely
        async with lock:
            await update.message.reply_text("Rocky pick surprise...")
            db = Database(settings.sqlite_path)
            db.init_schema()
            movies = await asyncio.wait_for(
                asyncio.to_thread(
                    db.get_random_movies,
                    country_code=settings.justwatch_country,
                    exclude_ids=context.user_data.get("seen_ids", []),
                    limit=3,
                ),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            if not movies:
                await update.message.reply_text("No movie in library right now. Rocky sad.")
                return
            context.user_data["seen_ids"] = context.user_data.get("seen_ids", [])
            context.user_data["seen_ids"].extend([m["tmdb_id"] for m in movies])
            context.user_data["last_shown_genres"] = [m.get("genre", "") for m in movies]
            context.user_data["last_intent"] = "surprise me"
            await send_recommendations(update, context, movies)
        return

    # Store intent for shuffle feature
    context.user_data["last_intent"] = user_text
    context.user_data["seen_ids"] = context.user_data.get("seen_ids", [])

    # Check if this is a playback request (contains "play" + movie name)
    is_play_request = any(
        keyword in user_text.lower()
        for keyword in ["play", "watch on", "put on", "start"]
    )

    if is_play_request:
        # Use the tool-use chat flow for playback
        async with lock:
            await update.message.reply_text("Rocky process...")

            try:
                concierge = _get_concierge(chat_id, settings)
                response_text, device_picker = await asyncio.wait_for(
                    asyncio.to_thread(concierge.chat, user_text),
                    timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
                )
                _concierge_last_used[chat_id] = time.time()
            except Exception as exc:
                logger.exception("Concierge chat failed")
                await update.message.reply_text(
                    _friendly_error_message(exc, "Something went wrong. Please try again.")
                )
                return

            # If Gemini wants a device picker, show inline buttons
            if device_picker:
                global _play_counter
                _play_counter += 1
                play_key = _play_counter
                _play_sessions[play_key] = {
                    **device_picker,
                    "chat_id": chat_id,
                    "user_id": update.effective_user.id if update.effective_user else None,
                    "created_at": time.time(),
                }
                _cleanup_play_sessions()

                buttons = []
                for i, dev in enumerate(device_picker["devices"]):
                    callback_data = f"p|{play_key}|{i}"
                    buttons.append([InlineKeyboardButton(
                        f"▶ {dev['label']}", callback_data=callback_data
                    )])
                buttons.append([InlineKeyboardButton("Not this one", callback_data=f"skip|{play_key}")])

                keyboard = InlineKeyboardMarkup(buttons)
                await update.message.reply_text(response_text, reply_markup=keyboard)
            else:
                await update.message.reply_text(response_text)
    else:
        # Recommendation flow — use lean structured response
        async with lock:
            await update.message.reply_text("Rocky search watchlist...")

            try:
                concierge = _get_concierge(chat_id, settings)
                movies = await asyncio.wait_for(
                    asyncio.to_thread(
                        concierge.recommend,
                        user_text,
                        context.user_data.get("seen_ids", []),
                    ),
                    timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
                )
                _concierge_last_used[chat_id] = time.time()
            except Exception as exc:
                logger.exception("Recommendation failed")
                await update.message.reply_text(
                    _friendly_error_message(exc, "Something went wrong. Please try again.")
                )
                return

            if not movies:
                await update.message.reply_text(
                    "No match in watchlist. Try different words?"
                )
                return

            # Track shown movies to avoid reshowing on shuffle
            context.user_data["seen_ids"].extend([m["tmdb_id"] for m in movies])

            await send_recommendations(update, context, movies)


async def _handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    """Handle messages in /chat mode — conversational Gemini with RECOMMEND: parsing."""
    settings: Settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id
    lock = _get_chat_lock(chat_id)

    async with lock:
        try:
            chat_concierge = _get_chat_concierge(chat_id, settings)
            display_text, tmdb_ids = await asyncio.wait_for(
                asyncio.to_thread(chat_concierge.chat_conversational, user_text),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            _chat_concierge_last_used[chat_id] = time.time()
        except Exception as exc:
            logger.exception("Chat concierge failed")
            await update.message.reply_text(
                _friendly_error_message(exc, "Something went wrong in chat mode. Please try again.")
            )
            return

        # Update conversation history in user_data
        history = context.user_data.get("chat_history", [])
        history.append({"role": "user", "content": user_text})
        history.append({"role": "model", "content": display_text})
        # Keep last 20 entries
        context.user_data["chat_history"] = history[-20:]

        if tmdb_ids:
            # Gemini recommended movies — show them with posters
            movies = []
            for tid in tmdb_ids:
                movie = await asyncio.wait_for(
                    asyncio.to_thread(
                        Database(settings.sqlite_path).get_movie_by_tmdb_id,
                        tid, settings.justwatch_country,
                    ),
                    timeout=10,
                )
                if movie:
                    movies.append(movie)

            if movies:
                # Send the conversational text first
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Exit chat mode", callback_data=json.dumps({"action": "exit_chat"}))]
                ])
                await update.message.reply_text(display_text, reply_markup=keyboard)
                # Then send recommendation cards
                context.user_data["current_recommendations"] = movies
                await send_recommendations(update, context, movies)
            else:
                # IDs didn't resolve — just show the text
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Exit chat mode", callback_data=json.dumps({"action": "exit_chat"}))]
                ])
                await update.message.reply_text(
                    display_text + "\n\n(Rocky cannot find those movie in library.)", reply_markup=keyboard)
            # Pure conversation — no recommendation yet
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Exit chat mode", callback_data=json.dumps({"action": "exit_chat"}))]
            ])
            await update.message.reply_text(display_text, reply_markup=keyboard)


async def _handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle message reactions — map emoji to watch_history."""
    if not await _guard_update(update, context, apply_rate_limit=False):
        return
    settings: Settings = context.bot_data["settings"]

    # MessageReactionHandler provides update.message_reaction (MessageReactionUpdated)
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    # Get current recommendations to find the movie this reaction applies to
    current_recs = context.user_data.get("current_recommendations", [])

    # Process each new reaction in the list
    for reaction in (reaction_update.new_reaction or []):
        # ReactionTypeEmoji has .emoji; ReactionTypeCustomEmoji does not
        emoji = getattr(reaction, 'emoji', None)
        if not emoji:
            continue
        reaction_value = REACTION_MAP.get(emoji)
        if not reaction_value:
            continue

        # Try to find which movie was reacted to
        # If we have recent recommendations, log the first one as a guess
        # (Telegram doesn't tell us which specific message was reacted to easily)
        if current_recs:
            # Log for the most recent recommendation set
            for movie in current_recs:
                db = Database(settings.sqlite_path)
                db.init_schema()
                await asyncio.to_thread(
                    db.log_watch_history,
                    tmdb_id=movie.get("tmdb_id"),
                    title=movie.get("title"),
                    reaction=reaction_value,
                    reaction_emoji=emoji,
                )
            logger.info("Logged reaction %s (%s) for %d movies", emoji, reaction_value, len(current_recs))


async def cmd_watched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual watch logging: /watched <movie title>"""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]

    # Parse the title from the command args
    title = " ".join(context.args or []).strip() if context.args else ""
    if not title:
        await update.message.reply_text("Usage: /watched <movie title>\nExample: /watched Parasite")
        return

    # Try to find the movie in the DB
    db = Database(settings.sqlite_path)
    db.init_schema()

    # Search by title
    with db._connect() as conn:
        row = conn.execute(
            "SELECT tmdb_id, title FROM movies WHERE LOWER(title) LIKE ? AND tmdb_id IS NOT NULL LIMIT 1",
            (f"%{title.lower()}%",),
        ).fetchone()

    if row:
        await asyncio.to_thread(
            db.log_watch_history,
            tmdb_id=row["tmdb_id"],
            title=row["title"],
            reaction="liked",
            reaction_emoji="👍",
        )
        await update.message.reply_text(f"Logged *{row['title']}* as watched 👍", parse_mode="Markdown")
    else:
        # Log even if not in DB
        await asyncio.to_thread(
            db.log_watch_history,
            tmdb_id=None,
            title=title,
            reaction="liked",
            reaction_emoji="👍",
        )
        await update.message.reply_text(f"Logged *{title}* as watched (not in your library)", parse_mode="Markdown")


async def callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for recommendations."""
    if not await _guard_update(update, context, apply_rate_limit=False):
        return
    query = update.callback_query
    await query.answer()

    try:
        data = json.loads(query.data)
    except (json.JSONDecodeError, TypeError):
        await query.edit_message_text("Invalid selection.")
        return

    action = data.get("action")

    if action == "exit_chat":
        # Exit chat mode — clear state
        context.user_data["chat_mode"] = False
        context.user_data.pop("chat_history", None)
        chat_id = update.effective_chat.id
        existing = _chat_concierges.get(chat_id)
        if existing:
            existing.reset()
        await query.edit_message_text("Chat mode end. Rocky here when you return. Use mood buttons or just type.")
        return

    if action == "pick":
        # User picked a movie — show device picker
        movies = context.user_data.get("current_recommendations", [])
        idx = data.get("idx", 0)
        if idx < 0 or idx >= len(movies):
            await query.edit_message_text("Invalid selection.")
            return
        selected = movies[idx]
        await send_device_picker(update, context, selected)

    elif action == "shuffle":
        # Re-run recommendation with same last intent, different results
        last_intent = context.user_data.get("last_intent", "something good")
        settings: Settings = context.bot_data["settings"]
        chat_id = update.effective_chat.id

        try:
            concierge = _get_concierge(chat_id, settings)
            movies = await asyncio.wait_for(
                asyncio.to_thread(
                    concierge.recommend,
                    last_intent,
                    context.user_data.get("seen_ids", []),
                ),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            _concierge_last_used[chat_id] = time.time()
        except Exception as exc:
            logger.exception("Shuffle recommendation failed")
            await query.edit_message_text(
                _friendly_error_message(exc, "Couldn't find more. Please try again.")
            )
            return

        if not movies:
            await query.edit_message_text("No more match. Try different words?")
            return

        # Track shown movies
        context.user_data["seen_ids"].extend([m["tmdb_id"] for m in movies])

        # Send new recommendations
        await query.edit_message_text("Rocky find other option:")
        await send_recommendations(update, context, movies)

    elif action == "device":
        # User picked a device — try to play
        play_key = data.get("play_key")
        dev_idx = data.get("dev_idx", 0)

        session_data = _play_sessions.get(play_key)
        if not session_data:
            await query.edit_message_text("Session expired. Try asking again.")
            return

        chat_id_check = update.effective_chat.id if update.effective_chat else None
        if session_data.get("chat_id") != chat_id_check:
            await query.answer("This selection is not valid in this chat.", show_alert=True)
            return

        devices = session_data["devices"]
        if dev_idx < 0 or dev_idx >= len(devices):
            await query.edit_message_text("Invalid device.")
            _play_sessions.pop(play_key, None)
            return

        device = devices[dev_idx]
        movie = context.user_data.get("selected_movie", {})
        movie_name = movie.get("title", session_data.get("movie_name", "Unknown"))
        settings: Settings = context.bot_data["settings"]

        # Search Jellyfin for the movie to get the item_id
        try:
            client = JellyfinClient(
                base_url=settings.jellyfin_url,
                api_key=settings.jellyfin_api_key,
                username=settings.jellyfin_username,
            )
            jellyfin_movies = await asyncio.wait_for(
                asyncio.to_thread(client.search_movies, movie_name, 5),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            if jellyfin_movies:
                item_id = jellyfin_movies[0].item_id
                await asyncio.wait_for(
                    asyncio.to_thread(
                        client.play, session_id=device["session_id"], item_id=item_id
                    ),
                    timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
                )
                await query.edit_message_text(
                    f"▶️ Playing *{movie_name}* on {device['label']}. Fist my bump.",
                    parse_mode="Markdown",
                )
                logger.info("Playback triggered via bot: %s on %s", movie_name, device["label"])
            else:
                await query.edit_message_text(
                    f"*{movie_name}* not in Jellyfin library. Rocky sad.",
                    parse_mode="Markdown",
                )
        except Exception as exc:
            logger.exception("Device playback failed")
            await query.edit_message_text(
                _friendly_error_message(exc, f"Cannot play on {device['label']}. Jellyfin app open?")
            )
        finally:
            _play_sessions.pop(play_key, None)

    else:
        await query.edit_message_text("Unknown action.")


async def callback_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle legacy play button presses (p|<key>|<idx> format)."""
    if not await _guard_update(update, context, apply_rate_limit=False):
        return
    query = update.callback_query
    await query.answer()

    # Parse: p|<play_key>|<device_index>
    parts = query.data.split("|")
    if len(parts) != 3 or parts[0] != "p":
        await query.edit_message_text("Invalid selection.")
        return

    _, play_key_str, dev_idx_str = parts
    try:
        play_key = int(play_key_str)
        dev_idx = int(dev_idx_str)
    except ValueError:
        await query.edit_message_text("Invalid selection.")
        return

    session_data = _play_sessions.get(play_key)
    if not session_data:
        await query.edit_message_text("Session expired. Try asking again.")
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if session_data.get("chat_id") != chat_id:
        await query.answer("This selection is not valid in this chat.", show_alert=True)
        return

    owner_user_id = session_data.get("user_id")
    current_user_id = update.effective_user.id if update.effective_user else None
    if owner_user_id is not None and current_user_id is not None and owner_user_id != current_user_id:
        await query.answer("Only the original requester can use these buttons.", show_alert=True)
        return

    devices = session_data["devices"]
    if dev_idx < 0 or dev_idx >= len(devices):
        await query.edit_message_text("Invalid device.")
        _play_sessions.pop(play_key, None)
        return

    device = devices[dev_idx]
    movie_name = session_data["movie_name"]
    item_id = session_data["item_id"]
    settings: Settings = context.bot_data["settings"]

    try:
        client = JellyfinClient(
            base_url=settings.jellyfin_url,
            api_key=settings.jellyfin_api_key,
            username=settings.jellyfin_username,
        )
        await asyncio.wait_for(
            asyncio.to_thread(client.play, session_id=device["session_id"], item_id=item_id),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        await query.edit_message_text(f"▶ Playing {movie_name} on {device['label']}. Grace Rocky save movies.")
        logger.info("Playback triggered via bot: %s on %s", movie_name, device["label"])
    except Exception as exc:
        logger.exception("Callback play failed")
        await query.edit_message_text(
            _friendly_error_message(exc, "Playback failed. Please try again.")
        )
    finally:
        _play_sessions.pop(play_key, None)


async def callback_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle skip button presses."""
    if not await _guard_update(update, context, apply_rate_limit=False):
        return

    query = update.callback_query
    await query.answer()

    # Format: skip or skip|<play_key>
    parts = (query.data or "").split("|")
    if len(parts) == 2 and parts[1].isdigit():
        _play_sessions.pop(int(parts[1]), None)

    await query.edit_message_text("Okay. What else you want watch?")


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """Start the Telegram bot (blocking)."""
    from rocky.logging_config import setup_logging
    setup_logging()

    settings = load_settings()

    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather.")
        return

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings

    # Conversational handler (all text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Slash commands (power-user shortcuts)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("chat", cmd_chat))

    # Reaction handler — uses MessageReactionHandler for message_reaction updates
    app.add_handler(MessageReactionHandler(_handle_reaction))

    # /watched command fallback for manual watch logging
    app.add_handler(CommandHandler("watched", cmd_watched))

    # Recommendation inline button callback (JSON-based actions)
    app.add_handler(CallbackQueryHandler(callback_button, pattern=r'^\{".*"'))

    # Legacy play/skip button callbacks
    app.add_handler(CallbackQueryHandler(callback_play, pattern=r"^p\|"))
    app.add_handler(CallbackQueryHandler(callback_skip, pattern=r"^skip(?:\|\d+)?$"))

    # Feature 2: Set up APScheduler for weekly stats card
    if settings.telegram_chat_id:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                send_weekly_stats,
                trigger="cron",
                day_of_week="fri",
                hour=18,
                minute=0,
                timezone="Asia/Kolkata",
                args=[app.bot, settings],
            )
            # Weekly taste profile regeneration (same cadence as stats)
            scheduler.add_job(
                lambda: generate_taste_profile(settings.sqlite_path),
                trigger="cron",
                day_of_week="fri",
                hour=17,
                minute=50,
                timezone="Asia/Kolkata",
            )
            scheduler.start()
            logger.info("Weekly stats scheduler started (Friday 18:00 Asia/Kolkata)")
        except ImportError:
            logger.warning("apscheduler not installed — weekly stats scheduler disabled")
    else:
        logger.info("TELEGRAM_CHAT_ID not set — weekly stats scheduler disabled")

    logger.info("Rocky online. Starting Telegram bot...")
    print("Rocky bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
