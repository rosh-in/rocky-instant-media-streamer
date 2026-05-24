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
PYTHONPATH=src python -m project_toto.bot
```

Slash commands: `/start`, `/reset`, `/devices`, `/status`. All text messages are routed to the Gemini concierge with per-chat memory and inline device selection buttons.

## Scheduled Sync (macOS launchd)

The `dev.toto.sync.plist` file runs the sync script automatically every 24 hours.

### Install

```sh
cp scripts/dev.toto.sync.plist ~/Library/LaunchAgents/dev.toto.sync.plist
launchctl load ~/Library/LaunchAgents/dev.toto.sync.plist
```

### Verify it's loaded

```sh
launchctl list | grep dev.toto.sync
```

### Run immediately (without waiting for the interval)

```sh
launchctl start dev.toto.sync
```

### Uninstall

```sh
launchctl unload ~/Library/LaunchAgents/dev.toto.sync.plist
rm ~/Library/LaunchAgents/dev.toto.sync.plist
```

### Logs
- Application logs (structured, rotating): `data/logs/sync.log`
- launchd stdout/stderr: `data/logs/launchd_sync.log`
