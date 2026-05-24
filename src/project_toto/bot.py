"""Telegram bot for Project Toto — visual movie concierge with inline buttons."""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import (
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
    filters,
)

from project_toto.config import Settings, load_settings
from project_toto.db import Database
from project_toto.gemini import MovieConcierge, availability_label
from project_toto.jellyfin import JellyfinClient

logger = logging.getLogger("project_toto.bot")

# Per-chat concierge instances (conversation memory)
_concierges: dict[int, MovieConcierge] = {}
_concierge_last_used: dict[int, float] = {}
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
        await update.callback_query.answer("This bot is private.", show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text("This bot is private.")


async def _notify_rate_limited(update: Update) -> None:
    """Send a short message when request rate exceeds the configured threshold."""
    text = "Too many requests right now. Please wait a few seconds and try again."
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


def _friendly_error_message(exc: Exception, fallback: str) -> str:
    """Map internal exceptions to safe user-facing messages."""
    msg = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError):
        return "That took too long. Please try again."
    if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
        return "I'm out of recommendation capacity right now. Please try again shortly."
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in msg:
        return "One of your services timed out. Please try again."
    if isinstance(exc, requests.exceptions.ConnectionError) or "connection" in msg:
        return "I couldn't reach one of your services. Please try again in a moment."
    if "jellyfin user" in msg and "not found" in msg:
        return "I couldn't find your Jellyfin user. Check JELLYFIN_USERNAME."
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
    if len(movies) >= 3:
        keyboard.append([
            InlineKeyboardButton("🔀 Show others", callback_data=json.dumps({"action": "shuffle"})),
        ])

    await context.bot.send_message(
        chat_id=chat_id,
        text="Pick one or shuffle for more:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Store current recommendations in context for callback use
    context.user_data["current_recommendations"] = movies


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
        text=f"*{movie['title']}* — where do you want to watch?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message."""
    if not await _guard_update(update, context):
        return
    text = (
        "Hey! I'm your movie concierge. Just tell me what you're in the mood for "
        "and I'll find something from your watchlist.\n\n"
        "Some things you can say:\n"
        "\"Something comforting, not too long\"\n"
        "\"Play Inception on my TV\"\n"
        "\"What's new on my watchlist?\"\n"
        "\"Just pick something for me\"\n\n"
        "Slash commands still work too: /devices, /status, /reset"
    )
    await update.message.reply_text(text)


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
    await update.message.reply_text("Conversation reset. Fresh start!")


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active Jellyfin devices (power-user shortcut)."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    if not settings.jellyfin_api_key or not settings.jellyfin_username:
        await update.message.reply_text("Jellyfin is not fully configured yet.")
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
            await update.message.reply_text("No active devices. Open Jellyfin on a device first.")
            return
        lines = [f"Active devices ({len(devices)}):\n"]
        for d in devices:
            lines.append(f"  • {d.label}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Device listing failed")
        await update.message.reply_text(
            _friendly_error_message(exc, "I couldn't list devices right now. Please try again.")
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
        lines = ["Library status:\n"]
        if "last_sync" in status:
            lines.append(f"Last sync: {status['last_sync']}")
            lines.append(f"Status: {status['status']}")
        lines.append(f"Total movies: {status['total_movies']}")
        lines.append(f"Unrequested: {status['unrequested']}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Status request failed")
        await update.message.reply_text(
            _friendly_error_message(exc, "I couldn't fetch status right now. Please try again.")
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main conversational handler — routes to recommendations or playback."""
    if not await _guard_update(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id
    lock = _get_chat_lock(chat_id)

    if not settings.gemini_api_key:
        await update.message.reply_text("GEMINI_API_KEY not configured.")
        return

    user_text = update.message.text or ""

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
            await update.message.reply_text("🎬 Finding and playing...")

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
            await update.message.reply_text("🎬 Finding something for you...")

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
                    "Nothing matching that in your watchlist right now. Try different words?"
                )
                return

            # Track shown movies to avoid reshowing on shuffle
            context.user_data["seen_ids"].extend([m["tmdb_id"] for m in movies])

            await send_recommendations(update, context, movies)


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
            await query.edit_message_text("No more matches found. Try different words?")
            return

        # Track shown movies
        context.user_data["seen_ids"].extend([m["tmdb_id"] for m in movies])

        # Send new recommendations (edit current "Pick one" message + send new media)
        await query.edit_message_text("🎬 Here are some other options:")
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
                    f"▶️ Playing *{movie_name}* on {device['label']}.",
                    parse_mode="Markdown",
                )
                logger.info("Playback triggered via bot: %s on %s", movie_name, device["label"])
            else:
                await query.edit_message_text(
                    f"*{movie_name}* not found in your Jellyfin library.",
                    parse_mode="Markdown",
                )
        except Exception as exc:
            logger.exception("Device playback failed")
            await query.edit_message_text(
                _friendly_error_message(exc, f"Couldn't play on {device['label']}. Is the Jellyfin app open?")
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
        await query.edit_message_text(f"▶ Playing {movie_name} on {device['label']}!")
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

    await query.edit_message_text("Okay, what else are you in the mood for?")


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """Start the Telegram bot (blocking)."""
    from project_toto.logging_config import setup_logging
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

    # Recommendation inline button callback (JSON-based actions)
    app.add_handler(CallbackQueryHandler(callback_button, pattern=r'^\{".*"'))

    # Legacy play/skip button callbacks
    app.add_handler(CallbackQueryHandler(callback_play, pattern=r"^p\|"))
    app.add_handler(CallbackQueryHandler(callback_skip, pattern=r"^skip(?:\|\d+)?$"))

    logger.info("Starting Project Toto Telegram bot...")
    print("Project Toto bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
