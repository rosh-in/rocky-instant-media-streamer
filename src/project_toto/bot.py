"""Telegram bot for Project Toto — Gemini recommendations + Jellyfin playback."""

from __future__ import annotations

import logging
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
)

from project_toto.config import Settings, load_settings
from project_toto.db import Database
from project_toto.gemini import recommend, format_recommendations
from project_toto.jellyfin import JellyfinClient

logger = logging.getLogger("project_toto.bot")

# Session-scoped storage for play command data (avoids 64-byte callback limit)
_play_sessions: dict[int, dict] = {}
_play_counter: int = 0


def _md_escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(settings: Settings) -> Database:
    db = Database(settings.sqlite_path)
    db.init_schema()
    return db


def _jellyfin(settings: Settings) -> JellyfinClient:
    return JellyfinClient(
        base_url=settings.jellyfin_url,
        api_key=settings.jellyfin_api_key,
        username=settings.jellyfin_username,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with command list."""
    text = (
        "🎬 Project Toto Bot\n\n"
        "Commands:\n"
        "/recommend <mood> — Get mood-based movie picks\n"
        "/play <title> — Play a movie on a device\n"
        "/devices — List active Jellyfin devices\n"
        "/status — Sync status & library stats\n"
    )
    await update.message.reply_text(text)


async def cmd_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mood-based movie recommendations via Gemini."""
    settings: Settings = context.bot_data["settings"]

    if not context.args:
        await update.message.reply_text("Usage: /recommend <mood>\nExample: /recommend feeling dark and gritty")
        return

    mood = " ".join(context.args)

    if not settings.gemini_api_key:
        await update.message.reply_text("GEMINI_API_KEY is not configured.")
        return

    await update.message.reply_text(f"Thinking about movies for mood: {mood} …")

    try:
        db = _db(settings)
        recs = recommend(
            api_key=settings.gemini_api_key,
            db=db,
            mood=mood,
            country_code=settings.justwatch_country,
        )
        text = format_recommendations(recs)
    except Exception as exc:
        logger.exception("Gemini recommendation failed")
        err_msg = str(exc)
        if "429" in err_msg or "quota" in err_msg.lower():
            text = "Gemini API quota exceeded. The free tier has rate limits — try again in a minute."
        else:
            text = f"Recommendation failed: {err_msg}"

    await update.message.reply_text(text)


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search Jellyfin and present inline device picker."""
    settings: Settings = context.bot_data["settings"]

    if not context.args:
        await update.message.reply_text("Usage: /play <title>\nExample: /play inception")
        return

    title = " ".join(context.args)

    if not settings.jellyfin_api_key:
        await update.message.reply_text("JELLYFIN_API_KEY is not configured.")
        return

    try:
        client = _jellyfin(settings)

        # Search movie
        movies = client.search_movies(title, limit=5)
        if not movies:
            await update.message.reply_text(f"No movie found for '{title}'.")
            return
        movie = movies[0]

        # Get active devices
        devices = client.list_devices()
        if not devices:
            await update.message.reply_text("No active devices. Open Jellyfin on a device first.")
            return

        # Build inline keyboard with device options
        # Use integer key to stay well under 64-byte callback limit
        global _play_counter
        _play_counter += 1
        play_key = _play_counter
        _play_sessions[play_key] = {
            "movie_name": movie.name,
            "movie_year": movie.year,
            "item_id": movie.item_id,
            "devices": {d.session_id: d.label for d in devices},
        }

        buttons = []
        for i, d in enumerate(devices):
            callback_data = f"p|{play_key}|{i}"
            buttons.append([InlineKeyboardButton(d.label, callback_data=callback_data)])

        keyboard = InlineKeyboardMarkup(buttons)
        year_str = f" ({movie.year})" if movie.year else ""
        await update.message.reply_text(
            f"🎬 {movie.name}{year_str}\nPick a device to play on:",
            reply_markup=keyboard,
        )

    except Exception as exc:
        logger.exception("Play command failed")
        await update.message.reply_text(f"Error: {exc}")


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active Jellyfin devices."""
    settings: Settings = context.bot_data["settings"]

    if not settings.jellyfin_api_key:
        await update.message.reply_text("JELLYFIN_API_KEY is not configured.")
        return

    try:
        client = _jellyfin(settings)
        devices = client.list_devices()
        if not devices:
            await update.message.reply_text("No active devices. Open Jellyfin on a device first.")
            return

        lines = [f"Active devices ({len(devices)}):\n"]
        for d in devices:
            lines.append(f"  • {d.label}")

        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Devices command failed")
        await update.message.reply_text(f"Error: {exc}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show sync status and library stats."""
    settings: Settings = context.bot_data["settings"]

    try:
        db = _db(settings)
        with db._connect() as conn:
            last_run = conn.execute(
                "SELECT started_at, status, items_seen, items_enriched, items_availability_refreshed, items_requested "
                "FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

            total_movies = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL").fetchone()[0]
            unrequested = conn.execute("SELECT COUNT(*) FROM movies WHERE tmdb_id IS NOT NULL AND requested_in_radarr = 0").fetchone()[0]

        lines = ["📊 Project Toto Status\n"]

        if last_run:
            lines.append(f"Last sync: {last_run['started_at'][:19]}")
            lines.append(f"Status: {last_run['status']}")
            lines.append(f"Seen: {last_run['items_seen']} | Enriched: {last_run['items_enriched']}")
            lines.append(f"Availability refreshed: {last_run['items_availability_refreshed']}")
            lines.append(f"Requested: {last_run['items_requested']}")
        else:
            lines.append("No sync runs recorded yet.")

        lines.append(f"\nTotal movies: {total_movies}")
        lines.append(f"Unrequested: {unrequested}")

        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.exception("Status command failed")
        await update.message.reply_text(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Callback handler for inline device picker
# ---------------------------------------------------------------------------

async def callback_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard press: trigger Jellyfin playback."""
    query = update.callback_query
    await query.answer()

    # Parse callback data: p|<play_key>|<device_index>
    parts = query.data.split("|")
    if len(parts) != 3 or parts[0] != "p":
        await query.edit_message_text("Invalid callback data.")
        return

    _, play_key_str, dev_idx_str = parts
    play_key = int(play_key_str)
    dev_idx = int(dev_idx_str)
    session_data = _play_sessions.get(play_key)
    if not session_data:
        await query.edit_message_text("Session expired. Try /play again.")
        return

    # Look up device by index from the stored order
    device_entries = list(session_data["devices"].items())
    if dev_idx >= len(device_entries):
        await query.edit_message_text("Invalid device selection.")
        return
    session_id, device_label = device_entries[dev_idx]

    movie_name = session_data["movie_name"]
    item_id = session_data["item_id"]
    settings: Settings = context.bot_data["settings"]

    try:
        client = _jellyfin(settings)
        client.play(session_id=session_id, item_id=item_id)
        await query.edit_message_text(f"▶️ Playing {movie_name} on {device_label}!")
        logger.info("Playback triggered via bot: %s on session %s", movie_name, session_id)
    except Exception as exc:
        logger.exception("Callback play failed")
        await query.edit_message_text(f"Playback failed: {exc}")


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

    # Store settings for handlers to access
    app.bot_data["settings"] = settings

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("recommend", cmd_recommend))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_play, pattern=r"^p\|"))

    logger.info("Starting Project Toto Telegram bot…")
    print("🎬 Project Toto bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
