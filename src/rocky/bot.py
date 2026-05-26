"""Rocky — Telegram bot. Rocky find movie you want watch. Visual movie concierge with inline buttons.

Architecture:
- DIRECT_PLAY fast-path (local, no Gemini) → "play X" goes straight to device picker
- RockyBrain (Gemini conversational brain) → handles everything else naturally
- Card System (unchanged) → poster cards with navigation
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional
import requests

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, MenuButtonCommands, ReplyKeyboardRemove, Update
from telegram.error import BadRequest
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
from rocky.gemini import RockyBrain
from rocky.intent import is_direct_play, extract_play_title, is_casual_message
from rocky.jellyfin import JellyfinClient
from rocky.rocky_dialogue import get_rocky_response
from rocky.stats import generate_stats
from rocky.taste_profile import generate_taste_profile
from rocky.tmdb import TmdbClient
from rocky.adb_controller import wake_and_launch as adb_wake_and_launch

logger = logging.getLogger("rocky.bot")

# ---------------------------------------------------------------------------
# State machine constants (simplified — no GATHERING states)
# ---------------------------------------------------------------------------
STATE_IDLE = "IDLE"
STATE_SHOWING_CARDS = "SHOWING_CARDS"
STATE_DEVICE_PICKING = "DEVICE_PICKING"
STATE_PLAYING = "PLAYING"

# Play session storage for inline device buttons
_play_sessions: dict[int, dict] = {}
_play_counter: int = 0
_rate_limit_events: dict[int, deque[float]] = {}

_BLOCKING_CALL_TIMEOUT_SECONDS = 90
_FALLBACK_POSTER = "https://placehold.co/500x750/1a1a2e/eee?text=No+Poster"
_PLAY_SESSION_TTL_SECONDS = 15 * 60
_MAX_PLAY_SESSIONS = 500

# Per-chat brain instances (for conversation history + RPM tracking)
_brains: dict[int, RockyBrain] = {}
_brain_last_used: dict[int, float] = {}
_BRAIN_TTL_SECONDS = 24 * 60 * 60

# Per-chat locks
_chat_locks: dict[int, asyncio.Lock] = {}

# Reaction emoji → reaction value mapping for watch_history
REACTION_MAP = {
    "❤️": "loved",
    "🔥": "loved",
    "👍": "liked",
    "😐": "neutral",
    "👎": "disliked",
    "🤮": "abandoned",
}


# ---------------------------------------------------------------------------
# Brain management
# ---------------------------------------------------------------------------
def _get_brain(chat_id: int, settings: Settings) -> RockyBrain:
    """Get or create a RockyBrain for this chat."""
    _cleanup_brains()
    brain = _brains.get(chat_id)
    if brain is None:
        _brains[chat_id] = RockyBrain(
            gemini_api_key=settings.gemini_api_key,
            db_path=settings.sqlite_path,
            country_code=settings.justwatch_country,
        )
        brain = _brains[chat_id]
    _brain_last_used[chat_id] = time.time()
    return brain


def _cleanup_brains(now_ts: Optional[float] = None) -> None:
    """Drop stale brain instances."""
    now = now_ts if now_ts is not None else time.time()
    stale = [cid for cid, ts in _brain_last_used.items() if (now - ts) > _BRAIN_TTL_SECONDS]
    for cid in stale:
        _brains.pop(cid, None)
        _brain_last_used.pop(cid, None)


# ---------------------------------------------------------------------------
# Lock management
# ---------------------------------------------------------------------------
def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Play session management
# ---------------------------------------------------------------------------
def _cleanup_play_sessions(now_ts: Optional[float] = None) -> None:
    now = now_ts if now_ts is not None else time.time()
    expired = [k for k, d in _play_sessions.items() if (now - float(d.get("created_at", 0.0))) > _PLAY_SESSION_TTL_SECONDS]
    for k in expired:
        _play_sessions.pop(k, None)
    overflow = len(_play_sessions) - _MAX_PLAY_SESSIONS
    if overflow > 0:
        oldest = sorted(_play_sessions, key=lambda k: float(_play_sessions[k].get("created_at", 0.0)))[:overflow]
        for k in oldest:
            _play_sessions.pop(k, None)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def _cleanup_rate_limit_events(settings: Settings, now_ts: Optional[float] = None) -> None:
    window = settings.telegram_rate_limit_window_seconds
    if window <= 0 or settings.telegram_rate_limit_max_messages <= 0:
        _rate_limit_events.clear()
        return
    now = now_ts if now_ts is not None else time.time()
    cutoff = now - window
    stale: list[int] = []
    for key, timestamps in _rate_limit_events.items():
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        if not timestamps:
            stale.append(key)
    for key in stale:
        _rate_limit_events.pop(key, None)


def _cleanup_state(settings: Settings) -> None:
    _cleanup_brains()
    _cleanup_play_sessions()
    _cleanup_rate_limit_events(settings=settings)


# ---------------------------------------------------------------------------
# Auth & rate limiting
# ---------------------------------------------------------------------------
def _is_authorized(update: Update, settings: Settings) -> bool:
    if not settings.telegram_allowed_user_ids and not settings.telegram_allowed_chat_ids:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if settings.telegram_allowed_user_ids and (user_id is None or user_id not in settings.telegram_allowed_user_ids):
        return False
    if settings.telegram_allowed_chat_ids and (chat_id is None or chat_id not in settings.telegram_allowed_chat_ids):
        return False
    return True


def _rate_limit_key(update: Update) -> Optional[int]:
    if update.effective_user:
        return int(update.effective_user.id)
    if update.effective_chat:
        return int(update.effective_chat.id)
    return None


def _is_rate_limited(update: Update, settings: Settings) -> bool:
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
    if update.callback_query:
        await update.callback_query.answer("Rocky not know you. Bot private.", show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text("Rocky not know you. Bot private.")


async def _notify_rate_limited(update: Update) -> None:
    text = "Too many request. Rocky need moment. Wait, try again."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def _guard_update(update: Update, context: ContextTypes.DEFAULT_TYPE, *, apply_rate_limit: bool = True) -> bool:
    settings: Settings = context.bot_data["settings"]
    _cleanup_state(settings)
    if not _is_authorized(update, settings):
        logger.warning("Unauthorized: user_id=%s chat_id=%s",
                       update.effective_user.id if update.effective_user else None,
                       update.effective_chat.id if update.effective_chat else None)
        await _notify_denied(update)
        return False
    if apply_rate_limit and _is_rate_limited(update, settings):
        await _notify_rate_limited(update)
        return False
    return True


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def _friendly_error_message(exc: Exception, fallback: str) -> str:
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


def _init_rocky_state(user_data: dict) -> None:
    """Initialize Rocky state in user_data if not already present."""
    if "rocky_state" not in user_data:
        user_data["rocky_state"] = STATE_IDLE
    if "seen_ids" not in user_data:
        user_data["seen_ids"] = []


def _reset_rocky_state(user_data: dict, full: bool = True) -> None:
    """Reset Rocky state. Full reset clears everything; partial keeps seen_ids for shuffle."""
    user_data["rocky_state"] = STATE_IDLE
    user_data.pop("pending_play", None)
    if full:
        user_data["seen_ids"] = []
        user_data.pop("last_intent", None)
        user_data.pop("current_recommendations", None)
        user_data.pop("current_movie_index", None)


# ---------------------------------------------------------------------------
# Visual recommendation helpers (unchanged)
# ---------------------------------------------------------------------------
def _build_movie_caption(movie: dict) -> str:
    year = movie.get("year") or "—"
    genre = movie.get("genre") or "—"
    runtime = movie.get("runtime") or "—"
    rating = movie.get("vote_average")
    rating_str = f"⭐ {rating:.1f}" if rating else ""
    director = movie.get("director")
    avail = "✅ Jellyfin" if movie.get("in_jellyfin") else f"🎬 {movie.get('ott_platforms', 'Not available')}"
    parts = [f"*{movie['title']}* ({year})"]
    info_parts = [f"🎬 {genre}", f"{runtime}m"]
    if rating_str:
        info_parts.append(rating_str)
    parts.append(" • ".join(info_parts))
    if director:
        parts.append(f"🎥 {director}")
    parts.append(avail)
    return "\n".join(parts)


def _build_movie_keyboard(movie: dict, index: int, total: int) -> InlineKeyboardMarkup:
    action_row = [
        InlineKeyboardButton(
            "▶ Watch Now",
            callback_data=json.dumps({"action": "pick", "tmdb_id": movie["tmdb_id"]}),
        ),
    ]
    trailer_key = movie.get("trailer_key")
    if trailer_key:
        action_row.append(InlineKeyboardButton(
            "🎞 Trailer",
            url=f"https://www.youtube.com/watch?v={trailer_key}",
        ))
    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(
            f"← {index} of {total}",
            callback_data=json.dumps({"action": "nav", "idx": index - 1}),
        ))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton(
            f"→ {index + 2} of {total}",
            callback_data=json.dumps({"action": "nav", "idx": index + 1}),
        ))
    else:
        nav_row.append(InlineKeyboardButton(
            "🔀 Other choices",
            callback_data=json.dumps({"action": "shuffle"}),
        ))
    rows = [action_row, nav_row]
    return InlineKeyboardMarkup(rows)


async def _fetch_trailer_key(movie: dict, settings: Settings) -> Optional[str]:
    trailer_key = movie.get("trailer_key")
    if trailer_key:
        return trailer_key
    if not movie.get("tmdb_id"):
        return None
    try:
        tmdb = TmdbClient(settings.tmdb_api_key)
        trailer_key = await asyncio.wait_for(
            asyncio.to_thread(tmdb.get_trailer_key, movie["tmdb_id"]),
            timeout=10,
        )
        if trailer_key:
            db = Database(settings.sqlite_path)
            await asyncio.wait_for(
                asyncio.to_thread(db.update_trailer_key, movie["id"], trailer_key),
                timeout=5,
            )
            movie["trailer_key"] = trailer_key
        return trailer_key
    except Exception as exc:
        logger.warning("On-demand trailer fetch failed for tmdb_id=%s: %s", movie.get("tmdb_id"), exc)
        return None


async def send_first_card(update: Update, context: ContextTypes.DEFAULT_TYPE, movies: list[dict]) -> None:
    """Send the first movie card as a photo message. Navigation edits this same message."""
    if not movies:
        return
    context.user_data["current_recommendations"] = movies
    context.user_data["current_movie_index"] = 0
    context.user_data["last_shown_genres"] = [m.get("genre", "") for m in movies]
    context.user_data["rocky_state"] = STATE_SHOWING_CARDS

    movie = movies[0]
    total = len(movies)
    settings: Settings = context.bot_data["settings"]

    await _fetch_trailer_key(movie, settings)

    poster = movie.get("poster_url") or _FALLBACK_POSTER
    caption = _build_movie_caption(movie)
    keyboard = _build_movie_keyboard(movie, 0, total)

    try:
        await update.message.reply_photo(photo=poster, caption=caption, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as exc:
        logger.warning("Photo send failed, falling back to text: %s", exc)
        await update.message.reply_text(text=caption, parse_mode="Markdown", reply_markup=keyboard)


async def show_movie_card(query, context: ContextTypes.DEFAULT_TYPE, movies: list[dict], index: int) -> None:
    """Edit the existing message in-place to show a different movie card."""
    if not movies or index < 0 or index >= len(movies):
        await query.edit_message_caption(caption="Invalid selection.")
        return
    context.user_data["current_recommendations"] = movies
    context.user_data["current_movie_index"] = index

    movie = movies[index]
    total = len(movies)
    settings: Settings = context.bot_data["settings"]

    await _fetch_trailer_key(movie, settings)

    poster = movie.get("poster_url") or _FALLBACK_POSTER
    caption = _build_movie_caption(movie)
    keyboard = _build_movie_keyboard(movie, index, total)

    try:
        await query.edit_message_media(
            media=InputMediaPhoto(media=poster, caption=caption, parse_mode="Markdown"),
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.warning("edit_message_media failed, trying caption: %s", exc)
        try:
            await query.edit_message_caption(caption=caption, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as exc2:
            logger.warning("edit_message_caption also failed: %s", exc2)


async def _execute_delayed_playback(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, movie_name: str, device: dict, play_key: int) -> None:
    """Background task: countdown 10s then execute playback unless undone."""
    for remaining in range(10, 0, -1):
        await asyncio.sleep(1)
        # Check if undone
        if not context.user_data.get("pending_play"):
            return
        # Update countdown label every 2 seconds
        if remaining % 2 == 0 and remaining > 0:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            f"↩ Undo — {remaining}s",
                            callback_data=json.dumps({"action": "undo"}),
                        ),
                    ]]),
                )
            except Exception:
                pass  # Message may have been edited by undo tap

    # Countdown finished — check if still pending
    pending = context.user_data.get("pending_play")
    if not pending:
        return

    settings: Settings = context.bot_data["settings"]
    session_data = _play_sessions.get(play_key)

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
                asyncio.to_thread(client.play, session_id=device["session_id"], item_id=item_id),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=f"▶️ Playing *{movie_name}* on {device['label']}. Fist my bump.",
                parse_mode="Markdown",
            )
            logger.info("Playback triggered: %s on %s", movie_name, device["label"])
        else:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=f"*{movie_name}* not in Jellyfin library. Rocky sad.",
                parse_mode="Markdown",
            )
    except Exception as exc:
        logger.exception("Delayed playback failed")
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=_friendly_error_message(exc, f"Cannot play on {device['label']}. Jellyfin app open?"),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    finally:
        context.user_data.pop("pending_play", None)
        _play_sessions.pop(play_key, None)
        _reset_rocky_state(context.user_data, full=True)


async def _adb_pre_wake_phone(settings: Settings, chat_id: Optional[int] = None, bot = None) -> None:
    """If ADB phone is configured, wake + launch Jellyfin before listing devices.

    This ensures the phone's Jellyfin session registers so it appears in the device list.
    """
    if not settings.adb_phone_ip:
        return
    try:
        if bot and chat_id:
            await bot.send_message(chat_id, "Rocky prepare phone. Small wait...")
        adb_ok = await adb_wake_and_launch(
            settings.adb_phone_ip,
            settings.adb_phone_package,
            settings.adb_phone_activity,
            wait=4,
        )
        if adb_ok:
            logger.info("ADB pre-wake succeeded for %s", settings.adb_phone_ip)
        else:
            logger.warning("ADB pre-wake failed for %s", settings.adb_phone_ip)
    except Exception as exc:
        logger.warning("ADB pre-wake error: %s", exc)


async def send_device_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, movie: dict) -> None:
    """Show device selection buttons after user picks a movie."""
    context.user_data["selected_movie"] = movie
    context.user_data["rocky_state"] = STATE_DEVICE_PICKING
    settings: Settings = context.bot_data["settings"]
    query = update.callback_query

    # Pre-wake phone via ADB so it registers as a Jellyfin device
    await _adb_pre_wake_phone(settings)

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
        devices = [{"label": d.label, "session_id": d.session_id} for d in device_list]
    except Exception as exc:
        logger.warning("Failed to list devices for picker: %s", exc)

    # Build back button (used in both no-devices and normal paths)
    back_button_row = [
        InlineKeyboardButton(
            "← Back to movie",
            callback_data=json.dumps({"action": "back_to_card"}),
        ),
    ]

    if not devices:
        back_keyboard = InlineKeyboardMarkup([back_button_row])
        if movie.get("in_jellyfin"):
            await query.edit_message_caption(
                caption=f"*{movie['title']}* is in your library but no devices are active. Open Jellyfin on a device first.",
                parse_mode="Markdown",
                reply_markup=back_keyboard,
            )
        else:
            platforms = movie.get("ott_platforms", "")
            await query.edit_message_caption(
                caption=f"*{movie['title']}* isn't in Jellyfin.\nAvailable on: {platforms}",
                parse_mode="Markdown",
                reply_markup=back_keyboard,
            )
        return

    global _play_counter
    _play_counter += 1
    play_key = _play_counter
    _play_sessions[play_key] = {
        "movie_name": movie["title"],
        "item_id": None,
        "devices": devices,
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id if update.effective_user else None,
        "created_at": time.time(),
    }
    _cleanup_play_sessions()
    context.user_data["play_key"] = play_key

    device_emoji_map = {"tv": "📺", "phone": "📱", "iphone": "📱", "ipad": "📱",
                        "chrome": "💻", "browser": "💻", "laptop": "💻", "desktop": "💻"}
    buttons = []
    for i, dev in enumerate(devices):
        dev_lower = dev["label"].lower()
        emoji = "📺"
        for key, em in device_emoji_map.items():
            if key in dev_lower:
                emoji = em
                break
        buttons.append(InlineKeyboardButton(
            f"{emoji} {dev['label']}",
            callback_data=json.dumps({"action": "device", "play_key": play_key, "dev_idx": i}),
        ))
    button_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    # Add back button as a separate row (reuse the same row defined above)
    button_rows.append(back_button_row)
    keyboard = InlineKeyboardMarkup(button_rows)
    await query.edit_message_caption(
        caption=f"*{movie['title']}* — where you want watch?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Helper: look up movie dicts from tmdb_ids
# ---------------------------------------------------------------------------
def _lookup_movies_by_tmdb_ids(db: Database, tmdb_ids: list[int], country_code: str) -> list[dict]:
    """Look up full movie dicts from the DB by tmdb_ids, in order."""
    results = []
    seen = set()
    for tmdb_id in tmdb_ids:
        movie = db.get_movie_by_tmdb_id(int(tmdb_id), country_code)
        if movie and movie["tmdb_id"] not in seen:
            results.append(movie)
            seen.add(movie["tmdb_id"])
    return results


# ---------------------------------------------------------------------------
# Handlers — Slash commands
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context):
        return
    chat_id = update.effective_chat.id
    lock = _get_chat_lock(chat_id)
    async with lock:
        for key, session in list(_play_sessions.items()):
            if session.get("chat_id") == chat_id:
                _play_sessions.pop(key, None)
    _reset_rocky_state(context.user_data, full=True)
    # Also reset the brain's conversation history
    settings: Settings = context.bot_data["settings"]
    brain = _brains.get(chat_id)
    if brain:
        brain.reset()
    await update.message.reply_text(get_rocky_response("reset"))


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    if not settings.jellyfin_api_key or not settings.jellyfin_username:
        await update.message.reply_text("Jellyfin not configured yet. Rocky need this.")
        return
    try:
        client = JellyfinClient(base_url=settings.jellyfin_url, api_key=settings.jellyfin_api_key, username=settings.jellyfin_username)
        devices = await asyncio.wait_for(asyncio.to_thread(client.list_devices), timeout=_BLOCKING_CALL_TIMEOUT_SECONDS)
        if not devices:
            await update.message.reply_text("No active device. Open Jellyfin on device first.")
            return
        lines = [f"Active device ({len(devices)}):\n"]
        for d in devices:
            lines.append(f"  • {d.label}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Device listing failed")
        await update.message.reply_text(_friendly_error_message(exc, "Rocky cannot list device right now. Try again."))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    try:
        db = Database(settings.sqlite_path)
        with db._connect() as conn:
            last_run = conn.execute(
                "SELECT started_at, status, items_seen, items_enriched, items_availability_refreshed, items_requested "
                "FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL").fetchone()[0]
            unrequested = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL AND requested_in_radarr = 0").fetchone()[0]
        lines = ["Rocky check library status:\n"]
        if last_run:
            lines.append(f"Last sync: {last_run['started_at'][:19]}")
            lines.append(f"Status: {last_run['status']}")
        lines.append(f"Total movie: {total}")
        lines.append(f"Not yet requested: {unrequested}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Status request failed")
        await update.message.reply_text(_friendly_error_message(exc, "Rocky cannot fetch status right now. Try again."))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    try:
        stats_text = await asyncio.wait_for(
            asyncio.to_thread(generate_stats, settings.sqlite_path, settings.justwatch_country),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        await update.message.reply_text(stats_text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Stats generation failed")
        await update.message.reply_text(_friendly_error_message(exc, "Rocky cannot count things right now. Try again."))


async def send_weekly_stats(bot, settings: Settings) -> None:
    chat_id = settings.telegram_chat_id
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set — skipping weekly stats")
        return
    try:
        stats_text = await asyncio.wait_for(
            asyncio.to_thread(generate_stats, settings.sqlite_path, settings.justwatch_country),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        await bot.send_message(chat_id=chat_id, text=stats_text, parse_mode="Markdown")
        logger.info("Weekly stats card sent to chat_id=%s", chat_id)
    except Exception as exc:
        logger.exception("Weekly stats send failed")


# ---------------------------------------------------------------------------
# Main message handler — simplified with Gemini brain
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main conversational handler — routes through Gemini brain."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id
    lock = _get_chat_lock(chat_id)

    if not settings.gemini_api_key:
        await update.message.reply_text("GEMINI_API_KEY not configured. Rocky brain missing.")
        return

    user_text = update.message.text or ""
    if not user_text.strip():
        return

    _init_rocky_state(context.user_data)
    state = context.user_data["rocky_state"]

    async with lock:
        # ------------------------------------------------------------------
        # Fast path: CASUAL_MESSAGE — "hey", "hi", "yo" → respond locally
        # ------------------------------------------------------------------
        if is_casual_message(user_text) and state not in (STATE_DEVICE_PICKING, STATE_PLAYING):
            await update.message.reply_text(get_rocky_response("greeting"))
            return

        # ------------------------------------------------------------------
        # Fast path: DIRECT_PLAY — "play X", "watch X" → skip Gemini
        # ------------------------------------------------------------------
        if is_direct_play(user_text) and state not in (STATE_DEVICE_PICKING, STATE_PLAYING):
            play_title = extract_play_title(user_text)
            db = Database(settings.sqlite_path)
            matches = await asyncio.wait_for(
                asyncio.to_thread(db.fuzzy_search_title, play_title, settings.justwatch_country, 5),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            if len(matches) == 1:
                # Exactly one match — go straight to device picker
                movie = matches[0]
                context.user_data["selected_movie"] = movie
                context.user_data["rocky_state"] = STATE_DEVICE_PICKING
                await update.message.reply_text(get_rocky_response("play_direct", title=movie["title"]))
                # Send poster card
                poster = movie.get("poster_url") or _FALLBACK_POSTER
                caption = _build_movie_caption(movie)
                await update.message.reply_photo(photo=poster, caption=caption, parse_mode="Markdown")
                # Pre-wake phone via ADB so it registers as a Jellyfin device
                await _adb_pre_wake_phone(settings, chat_id, context.bot)

                # Show device picker
                try:
                    client = JellyfinClient(base_url=settings.jellyfin_url, api_key=settings.jellyfin_api_key, username=settings.jellyfin_username)
                    device_list = await asyncio.wait_for(asyncio.to_thread(client.list_devices), timeout=_BLOCKING_CALL_TIMEOUT_SECONDS)
                    devices = [{"label": d.label, "session_id": d.session_id} for d in device_list]
                except Exception:
                    devices = []
                if not devices:
                    await update.message.reply_text(get_rocky_response("no_devices"))
                    _reset_rocky_state(context.user_data, full=True)
                    return
                global _play_counter
                _play_counter += 1
                play_key = _play_counter
                _play_sessions[play_key] = {
                    "movie_name": movie["title"], "item_id": None,
                    "devices": devices, "chat_id": chat_id,
                    "user_id": update.effective_user.id if update.effective_user else None,
                    "created_at": time.time(),
                }
                _cleanup_play_sessions()
                context.user_data["play_key"] = play_key
                device_emoji_map = {"tv": "📺", "phone": "📱", "iphone": "📱", "ipad": "📱",
                                    "chrome": "💻", "browser": "💻", "laptop": "💻", "desktop": "💻"}
                buttons = []
                for i, dev in enumerate(devices):
                    dev_lower = dev["label"].lower()
                    emoji = "📺"
                    for key, em in device_emoji_map.items():
                        if key in dev_lower:
                            emoji = em
                            break
                    buttons.append(InlineKeyboardButton(
                        f"{emoji} {dev['label']}",
                        callback_data=json.dumps({"action": "device", "play_key": play_key, "dev_idx": i}),
                    ))
                button_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
                keyboard = InlineKeyboardMarkup(button_rows)
                await update.message.reply_text(
                    f"*{movie['title']}* — where you want watch?",
                    parse_mode="Markdown", reply_markup=keyboard,
                )
            elif len(matches) > 1:
                # Multiple matches — show cards
                await update.message.reply_text(get_rocky_response("play_ambiguous"))
                await send_first_card(update, context, matches[:3])
            else:
                # Not found locally — fall through to Gemini brain
                await _handle_with_brain(update, context, chat_id, settings, user_text)
            return

        # ------------------------------------------------------------------
        # STATE: DEVICE_PICKING or PLAYING — ignore new text
        # ------------------------------------------------------------------
        if state in (STATE_DEVICE_PICKING, STATE_PLAYING):
            await update.message.reply_text("Pick device first. Or type /reset to start over.")
            return

        # ------------------------------------------------------------------
        # Everything else → Gemini brain (handles IDLE and SHOWING_CARDS)
        # ------------------------------------------------------------------
        await _handle_with_brain(update, context, chat_id, settings, user_text)


async def _handle_with_brain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    settings: Settings,
    user_text: str,
) -> None:
    """Send user message to Gemini brain and render the response."""
    brain = _get_brain(chat_id, settings)
    seen_ids = context.user_data.get("seen_ids", [])

    # If we're showing cards, tell the brain about previously shown movies
    # so it doesn't re-recommend them
    current_recs = context.user_data.get("current_recommendations", [])
    if current_recs:
        shown_from_cards = [m.get("tmdb_id") for m in current_recs if m.get("tmdb_id")]
        seen_ids = list(set(seen_ids + shown_from_cards))

    # Send immediate loading indicator
    loading_msg = await update.message.reply_text("🪨 Rocky process...")

    # Call Gemini brain
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                brain.chat,
                user_text,
                shown_ids=seen_ids,
            ),
            timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.exception("Gemini brain call failed")
        try:
            await loading_msg.edit_text(_friendly_error_message(exc, "Rocky brain not working. Try again?"))
        except Exception:
            await update.message.reply_text(_friendly_error_message(exc, "Rocky brain not working. Try again?"))
        return

    reply = result.get("reply", "Rocky thinking...")
    action = result.get("action", "ask")
    tmdb_ids = result.get("tmdb_ids", [])

    # Helper to replace the loading indicator with the reply
    async def _replace_loading(text: str, **kwargs) -> None:
        try:
            await loading_msg.edit_text(text, **kwargs)
        except Exception:
            await update.message.reply_text(text, **kwargs)

    # Render the response based on action
    if action == "play" and tmdb_ids:
        # Gemini says the user wants to play a specific movie
        # Pre-wake phone via ADB so it registers as a Jellyfin device
        await _adb_pre_wake_phone(settings, chat_id, context.bot)

        # Parallelize: fetch movie + Jellyfin devices at the same time
        db = Database(settings.sqlite_path)

        async def _fetch_movie():
            return await asyncio.to_thread(db.get_movie_by_tmdb_id, int(tmdb_ids[0]), settings.justwatch_country)

        async def _fetch_devices():
            try:
                client = JellyfinClient(base_url=settings.jellyfin_url, api_key=settings.jellyfin_api_key, username=settings.jellyfin_username)
                device_list = await asyncio.wait_for(asyncio.to_thread(client.list_devices), timeout=_BLOCKING_CALL_TIMEOUT_SECONDS)
                return [{"label": d.label, "session_id": d.session_id} for d in device_list]
            except Exception:
                return []

        movie, devices = await asyncio.gather(_fetch_movie(), _fetch_devices())

        if movie:
            context.user_data["selected_movie"] = movie
            context.user_data["rocky_state"] = STATE_DEVICE_PICKING
            await _replace_loading(reply)
            # Show poster + device picker
            poster = movie.get("poster_url") or _FALLBACK_POSTER
            caption = _build_movie_caption(movie)
            await update.message.reply_photo(photo=poster, caption=caption, parse_mode="Markdown")
            if not devices:
                await update.message.reply_text(get_rocky_response("no_devices"))
                _reset_rocky_state(context.user_data, full=True)
                return
            global _play_counter
            _play_counter += 1
            play_key = _play_counter
            _play_sessions[play_key] = {
                "movie_name": movie["title"], "item_id": None,
                "devices": devices, "chat_id": chat_id,
                "user_id": update.effective_user.id if update.effective_user else None,
                "created_at": time.time(),
            }
            _cleanup_play_sessions()
            context.user_data["play_key"] = play_key
            device_emoji_map = {"tv": "📺", "phone": "📱", "iphone": "📱", "ipad": "📱",
                                "chrome": "💻", "browser": "💻", "laptop": "💻", "desktop": "💻"}
            buttons = []
            for i, dev in enumerate(devices):
                dev_lower = dev["label"].lower()
                emoji = "📺"
                for key, em in device_emoji_map.items():
                    if key in dev_lower:
                        emoji = em
                        break
                buttons.append(InlineKeyboardButton(
                    f"{emoji} {dev['label']}",
                    callback_data=json.dumps({"action": "device", "play_key": play_key, "dev_idx": i}),
                ))
            button_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            keyboard = InlineKeyboardMarkup(button_rows)
            await update.message.reply_text(
                f"*{movie['title']}* — where you want watch?",
                parse_mode="Markdown", reply_markup=keyboard,
            )
        else:
            # Movie not in DB — just send the reply
            await _replace_loading(reply)
        return

    if action == "recommend" and tmdb_ids:
        # Gemini recommends movies — show poster cards
        db = Database(settings.sqlite_path)
        movies = await asyncio.to_thread(_lookup_movies_by_tmdb_ids, db, tmdb_ids, settings.justwatch_country)

        if movies:
            context.user_data["seen_ids"] = seen_ids + [m["tmdb_id"] for m in movies]
            await _replace_loading(reply)
            await send_first_card(update, context, movies)
        else:
            # tmdb_ids didn't resolve to DB movies — just send the reply
            logger.warning("Gemini recommended tmdb_ids not found in DB: %s", tmdb_ids)
            await _replace_loading(reply)
        return

    # action == "chat" or "ask" or no tmdb_ids — just send the conversational reply
    await _replace_loading(reply)


# ---------------------------------------------------------------------------
# Callback button handler — navigation, pick, shuffle, device
# ---------------------------------------------------------------------------
async def callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context, apply_rate_limit=False):
        return
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        if "query is too old" in str(e).lower():
            logger.info("Stale callback query ignored")
            return
        raise

    try:
        data = json.loads(query.data)
    except (json.JSONDecodeError, TypeError):
        try:
            await query.edit_message_text("Invalid selection.")
        except BadRequest:
            pass
        return

    action = data.get("action")

    if action == "nav":
        movies = context.user_data.get("current_recommendations", [])
        index = data.get("idx", 0)
        await show_movie_card(query, context, movies, index)

    elif action == "pick":
        tmdb_id = data.get("tmdb_id")
        movies = context.user_data.get("current_recommendations", [])
        selected = None
        for m in movies:
            if m.get("tmdb_id") == tmdb_id:
                selected = m
                break
        if not selected:
            await query.edit_message_caption(caption="Invalid selection.")
            return
        # Store state before leaving the card so Back can restore it
        context.user_data["pre_picker_state"] = {
            "movie": selected,
            "index": context.user_data.get("current_movie_index", 0),
            "total": len(movies),
        }
        await send_device_picker(update, context, selected)

    elif action == "shuffle":
        # Re-run recommendation using Gemini brain with same context
        settings: Settings = context.bot_data["settings"]
        chat_id = update.effective_chat.id
        brain = _get_brain(chat_id, settings)
        seen_ids = context.user_data.get("seen_ids", [])

        await query.edit_message_caption(caption=get_rocky_response("shuffle"))

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    brain.chat,
                    "Show me different movies, not the ones you already recommended.",
                    shown_ids=seen_ids,
                ),
                timeout=_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.exception("Shuffle recommendation failed")
            await query.edit_message_caption(caption=_friendly_error_message(exc, "Couldn't find more. Please try again."))
            return

        tmdb_ids = result.get("tmdb_ids", [])
        if not tmdb_ids:
            await query.edit_message_caption(caption="No more match. Try different words?")
            return

        db = Database(settings.sqlite_path)
        movies = await asyncio.to_thread(_lookup_movies_by_tmdb_ids, db, tmdb_ids, settings.justwatch_country)

        if not movies:
            await query.edit_message_caption(caption="No more match. Try different words?")
            return

        context.user_data["seen_ids"] = seen_ids + [m["tmdb_id"] for m in movies]
        await show_movie_card(query, context, movies, 0)

    elif action == "back_to_card":
        pre_state = context.user_data.get("pre_picker_state")
        movies = context.user_data.get("current_recommendations", [])

        if not pre_state:
            # Fallback — state lost, go to first card
            if movies:
                await show_movie_card(query, context, movies, 0)
            else:
                await query.edit_message_caption(
                    caption="Rocky lose place. Say what you want watch — Rocky find again.",
                )
            return

        index = pre_state["index"]
        await show_movie_card(query, context, movies, index)
        context.user_data.pop("pre_picker_state", None)

    elif action == "device":
        play_key = data.get("play_key")
        dev_idx = data.get("dev_idx", 0)

        session_data = _play_sessions.get(play_key)
        if not session_data:
            await query.edit_message_caption(caption="Session expired. Try asking again.")
            return

        chat_id_check = update.effective_chat.id if update.effective_chat else None
        if session_data.get("chat_id") != chat_id_check:
            await query.answer("This selection is not valid in this chat.", show_alert=True)
            return

        devices = session_data["devices"]
        if dev_idx < 0 or dev_idx >= len(devices):
            await query.edit_message_caption(caption="Invalid device.")
            _play_sessions.pop(play_key, None)
            return

        device = devices[dev_idx]
        movie = context.user_data.get("selected_movie", {})
        movie_name = movie.get("title", session_data.get("movie_name", "Unknown"))

        # Store pending play for undo window
        context.user_data["pending_play"] = {
            "movie": movie_name,
            "device": device,
            "play_key": play_key,
            "expires": time.time() + 10,
        }
        context.user_data["rocky_state"] = STATE_PLAYING

        # Show undo button with countdown
        await query.edit_message_caption(
            caption=f"▶️ Playing *{movie_name}* on {device['label']}...\n\n_Tap undo to cancel_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "↩ Undo — 10s",
                    callback_data=json.dumps({"action": "undo"}),
                ),
            ]]),
        )

        # Clear picker state after successful play
        context.user_data.pop("pre_picker_state", None)

        # Launch background task for delayed playback
        chat_id = update.effective_chat.id
        message_id = query.message.message_id
        asyncio.create_task(
            _execute_delayed_playback(context, chat_id, message_id, movie_name, device, play_key)
        )

    elif action == "undo":
        pending = context.user_data.pop("pending_play", None)
        if not pending:
            await query.answer("Nothing to undo.", show_alert=True)
            return

        # Cancel the pending play
        play_key = pending.get("play_key")
        if play_key:
            _play_sessions.pop(play_key, None)

        movie_name = pending.get("movie", "Unknown")
        logger.info("Playback undone: %s", movie_name)

        # Return to movie card
        movies = context.user_data.get("current_recommendations", [])
        index = context.user_data.get("current_movie_index", 0)
        if movies and 0 <= index < len(movies):
            context.user_data["rocky_state"] = STATE_SHOWING_CARDS
            await show_movie_card(query, context, movies, index)
        else:
            context.user_data["rocky_state"] = STATE_IDLE
            await query.edit_message_caption(
                caption=f"↩ Cancelled *{movie_name}*.",
                parse_mode="Markdown",
            )

    else:
        await query.edit_message_text("Unknown action.")


# ---------------------------------------------------------------------------
# Reaction handler
# ---------------------------------------------------------------------------
async def _handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context, apply_rate_limit=False):
        return
    settings: Settings = context.bot_data["settings"]
    reaction_update = update.message_reaction
    if not reaction_update:
        return
    current_recs = context.user_data.get("current_recommendations", [])
    for reaction in (reaction_update.new_reaction or []):
        emoji = getattr(reaction, 'emoji', None)
        if not emoji:
            continue
        reaction_value = REACTION_MAP.get(emoji)
        if not reaction_value:
            continue
        if current_recs:
            for movie in current_recs:
                db = Database(settings.sqlite_path)
                await asyncio.to_thread(
                    db.log_watch_history,
                    tmdb_id=movie.get("tmdb_id"),
                    title=movie.get("title"),
                    reaction=reaction_value,
                    reaction_emoji=emoji,
                )
            logger.info("Logged reaction %s (%s) for %d movies", emoji, reaction_value, len(current_recs))


# ---------------------------------------------------------------------------
# /watched command
# ---------------------------------------------------------------------------
async def cmd_watched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    title = " ".join(context.args or []).strip() if context.args else ""
    if not title:
        await update.message.reply_text("Usage: /watched <movie title>\nExample: /watched Parasite")
        return
    db = Database(settings.sqlite_path)
    with db._connect() as conn:
        row = conn.execute(
            "SELECT tmdb_id, title FROM movies WHERE LOWER(title) LIKE ? AND tmdb_id IS NOT NULL LIMIT 1",
            (f"%{title.lower()}%",),
        ).fetchone()
    if row:
        await asyncio.to_thread(db.log_watch_history, tmdb_id=row["tmdb_id"], title=row["title"], reaction="liked", reaction_emoji="👍")
        await update.message.reply_text(f"Logged *{row['title']}* as watched 👍", parse_mode="Markdown")
    else:
        await asyncio.to_thread(db.log_watch_history, tmdb_id=None, title=title, reaction="liked", reaction_emoji="👍")
        await update.message.reply_text(f"Logged *{title}* as watched (not in your library)", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Bot menu & command registration
# ---------------------------------------------------------------------------
_ROCKY_COMMANDS = [
    BotCommand("start", "Start Rocky — show welcome"),
    BotCommand("devices", "List active Jellyfin devices"),
    BotCommand("status", "Show watchlist sync status"),
    BotCommand("stats", "Watchlist progress card"),
    BotCommand("reset", "Reset conversation memory"),
    BotCommand("watched", "Log a movie as watched"),
]


async def _register_bot_menu(application) -> None:
    await application.bot.set_my_commands(_ROCKY_COMMANDS)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Bot menu registered with %d commands", len(_ROCKY_COMMANDS))


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------
def run_bot() -> None:
    from rocky.logging_config import setup_logging
    setup_logging()

    settings = load_settings()

    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather.")
        return

    # Initialize DB schema once at startup instead of per-handler
    _startup_db = Database(settings.sqlite_path)
    _startup_db.init_schema()
    logger.info("Database schema initialized at startup")

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings

    app.post_init = _register_bot_menu

    # Conversational handler (all text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Slash commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Reaction handler
    app.add_handler(MessageReactionHandler(_handle_reaction))

    # /watched command
    app.add_handler(CommandHandler("watched", cmd_watched))

    # Recommendation inline button callback (JSON-based actions)
    app.add_handler(CallbackQueryHandler(callback_button, pattern=r'^\{\".*\"'))

    # Weekly stats scheduler
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
