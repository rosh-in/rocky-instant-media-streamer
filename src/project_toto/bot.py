"""Telegram bot for Project Toto — conversational movie concierge."""

from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
    filters,
)

from project_toto.config import Settings, load_settings
from project_toto.gemini import MovieConcierge
from project_toto.jellyfin import JellyfinClient

logger = logging.getLogger("project_toto.bot")

# Per-chat concierge instances (conversation memory)
_concierges: dict[int, MovieConcierge] = {}

# Play session storage for inline device buttons
_play_sessions: dict[int, dict] = {}
_play_counter: int = 0


def _get_concierge(chat_id: int, settings: Settings) -> MovieConcierge:
    """Get or create a concierge for this chat (preserves conversation memory)."""
    if chat_id not in _concierges:
        _concierges[chat_id] = MovieConcierge(
            gemini_api_key=settings.gemini_api_key,
            db_path=str(settings.sqlite_path),
            jellyfin_url=settings.jellyfin_url,
            jellyfin_api_key=settings.jellyfin_api_key,
            jellyfin_username=settings.jellyfin_username,
            country_code=settings.justwatch_country,
        )
    return _concierges[chat_id]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message."""
    text = (
        "Hey! I'm your movie concierge. Just tell me what you're in the mood for "
        "and I'll find something from your watchlist.\n\n"
        "Some things you can say:\n"
        "\"Something comforting, not too long\"\n"
        "\"Play Inception on my TV\"\n"
        "\"What's new on my watchlist?\"\n"
        "\"Just pick something for me\"\n\n"
        "Slash commands still work too: /play, /devices, /status, /reset"
    )
    await update.message.reply_text(text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset conversation memory."""
    chat_id = update.effective_chat.id
    if chat_id in _concierges:
        _concierges[chat_id].reset()
        del _concierges[chat_id]
    await update.message.reply_text("Conversation reset. Fresh start!")


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active Jellyfin devices (power-user shortcut)."""
    settings: Settings = context.bot_data["settings"]
    try:
        client = JellyfinClient(
            base_url=settings.jellyfin_url,
            api_key=settings.jellyfin_api_key,
            username=settings.jellyfin_username,
        )
        devices = client.list_devices()
        if not devices:
            await update.message.reply_text("No active devices. Open Jellyfin on a device first.")
            return
        lines = [f"Active devices ({len(devices)}):\n"]
        for d in devices:
            lines.append(f"  • {d.label}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show sync status (power-user shortcut)."""
    settings: Settings = context.bot_data["settings"]
    try:
        concierge = _get_concierge(update.effective_chat.id, settings)
        status = concierge._get_sync_status()
        lines = ["Library status:\n"]
        if "last_sync" in status:
            lines.append(f"Last sync: {status['last_sync']}")
            lines.append(f"Status: {status['status']}")
        lines.append(f"Total movies: {status['total_movies']}")
        lines.append(f"Unrequested: {status['unrequested']}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main conversational handler — sends everything to the concierge."""
    settings: Settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id

    if not settings.gemini_api_key:
        await update.message.reply_text("GEMINI_API_KEY not configured.")
        return

    user_text = update.message.text or ""
    await update.message.reply_text("Thinking...")

    try:
        concierge = _get_concierge(chat_id, settings)
        response_text, device_picker = concierge.chat(user_text)
    except Exception as exc:
        logger.exception("Concierge chat failed")
        await update.message.reply_text(f"Something went wrong: {exc}")
        return

    # If Gemini wants a device picker, show inline buttons
    if device_picker:
        global _play_counter
        _play_counter += 1
        play_key = _play_counter
        _play_sessions[play_key] = device_picker

        buttons = []
        for i, dev in enumerate(device_picker["devices"]):
            callback_data = f"p|{play_key}|{i}"
            buttons.append([InlineKeyboardButton(
                f"▶ {dev['label']}", callback_data=callback_data
            )])
        # Add a "not this one" button
        buttons.append([InlineKeyboardButton("Not this one", callback_data="skip")])

        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(response_text, reply_markup=keyboard)
    else:
        await update.message.reply_text(response_text)


async def callback_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    if query.data == "skip":
        await query.edit_message_text("Okay, what else are you in the mood for?")
        return

    # Parse: p|<play_key>|<device_index>
    parts = query.data.split("|")
    if len(parts) != 3 or parts[0] != "p":
        await query.edit_message_text("Invalid selection.")
        return

    _, play_key_str, dev_idx_str = parts
    play_key = int(play_key_str)
    dev_idx = int(dev_idx_str)

    session_data = _play_sessions.get(play_key)
    if not session_data:
        await query.edit_message_text("Session expired. Try asking again.")
        return

    devices = session_data["devices"]
    if dev_idx >= len(devices):
        await query.edit_message_text("Invalid device.")
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
        client.play(session_id=device["session_id"], item_id=item_id)
        await query.edit_message_text(f"▶ Playing {movie_name} on {device['label']}!")
        logger.info("Playback triggered via bot: %s on %s", movie_name, device["label"])
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
    app.bot_data["settings"] = settings

    # Conversational handler (all text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Slash commands (power-user shortcuts)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline button callback
    app.add_handler(CallbackQueryHandler(callback_play, pattern=r"^p\|"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: u.callback_query.answer() or u.callback_query.edit_message_text("Okay, what else are you in the mood for?"),
        pattern=r"^skip$"
    ))

    logger.info("Starting Project Toto Telegram bot...")
    print("Project Toto bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
