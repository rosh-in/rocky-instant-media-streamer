# Scripts

## `sync_watchlist.py`
Runs the full watchlist sync pipeline (Letterboxd → TMDB → JustWatch → Radarr).

```sh
python scripts/sync_watchlist.py
```

## `status.py`
Prints a summary of recent sync runs. Exits with code 1 if the latest run failed.

```sh
python scripts/status.py          # last 5 runs
python scripts/status.py --last 10
```

## `play.py`
Control Jellyfin playback from the terminal. Requires `JELLYFIN_API_KEY` and `JELLYFIN_USERNAME` in `.env`.

### List active devices

```sh
python scripts/play.py devices
```

### Search the movie library

```sh
python scripts/play.py search "inception"
```

### Play a movie on a specific device

```sh
python scripts/play.py start "inception" "bravia"
```

The device name is a partial match against the Jellyfin device name or client name.

## Telegram Bot

Conversational movie concierge powered by Gemini 2.5 Flash Lite.

```sh
PYTHONPATH=src python -m rocky.bot
```

Slash commands:
- `/start` — Show onboarding tips
- `/reset` — Clear conversation memory and start fresh
- `/devices` — List active Jellyfin devices
- `/status` — Show last sync run status
- `/stats` — Display watchlist progress card with counts
- `/watched <title>` — Log a movie as watched (updates taste profile)

All text messages are routed through intent classification — direct-play requests ("play X", "watch X") skip Gemini and go straight to device picker. Casual greetings ("hey", "hi") get local Rocky responses. Everything else routes to the Gemini brain with per-chat memory, tool declarations, and inline device selection buttons.

**Features:**
- **Intent routing**: Fast-path for direct play requests without Gemini overhead
- **Casual message handling**: Local responses for greetings and small talk
- **Semantic search**: ChromaDB vector store for natural language movie discovery
- **Taste profiles**: Automatic per-user taste profiles from watch history (loved/liked/disliked patterns)
- **Reaction logging**: React to bot recommendations with ❤️🔥👍😐👎🤮 to log to watch_history
- **Weekly stats**: Automated Friday 18:00 stats summary (if `TELEGRAM_CHAT_ID` is set)

## `run_webhook.py`
Starts the FastAPI webhook server that receives Jellyfin playback events and Radarr notifications.

```sh
python scripts/run_webhook.py
```

Endpoints:
- `POST /webhook/jellyfin/playback` — Jellyfin playback completion events
  - Logs to `watch_history` table when a movie finishes playing
  - Extracts TMDB ID from payload to link with your watchlist
- `POST /webhook/radarr` — Radarr grab/import notifications
  - Logs grab and import events for reference
- `GET /health` — Health check

Runs on port 8400 by default.

Configure Jellyfin webhook plugin to send playback events to `http://your-host:8400/webhook/jellyfin/playback`.
Configure Radarr webhook to `http://your-host:8400/webhook/radarr`.

## Scheduled Sync (macOS launchd)

The `dev.rocky.sync.plist` file runs the sync script automatically every 24 hours.

### Install

```sh
cp scripts/dev.rocky.sync.plist ~/Library/LaunchAgents/dev.rocky.sync.plist
launchctl load ~/Library/LaunchAgents/dev.rocky.sync.plist
```

### Verify it's loaded

```sh
launchctl list | grep dev.rocky.sync
```

### Run immediately (without waiting for the interval)

```sh
launchctl start dev.rocky.sync
```

### Uninstall

```sh
launchctl unload ~/Library/LaunchAgents/dev.rocky.sync.plist
rm ~/Library/LaunchAgents/dev.rocky.sync.plist
```

### Logs
- Application logs (structured, rotating): `data/logs/sync.log`
- launchd stdout/stderr: `data/logs/launchd_sync.log`
