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
