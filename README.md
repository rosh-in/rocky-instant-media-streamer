# Project Toto

Personal home media automation system that discovers movies from a Letterboxd watchlist, enriches them with metadata, checks streaming availability, and pushes requests to a local media stack — all hands-free.

## How It Works

```
Letterboxd Watchlist
        │
        ▼
   TMDB Enrichment ──► SQLite (source of truth)
        │
        ▼
  JustWatch Lookup ──► OTT availability stored
        │
        ▼
   Radarr Push ──► qBittorrent ──► Jellyfin
```

1. **Scrape** — Parses your Letterboxd watchlist pages.
2. **Enrich** — Looks up each movie on TMDB for structured metadata.
3. **Availability** — Queries JustWatch to record which OTT platforms carry each title.
4. **Request** — Pushes movies (that aren't already tracked) into Radarr, which hands off to qBittorrent for downloading and Jellyfin for streaming.

The full pipeline runs as a single `sync_watchlist.py` invocation and can be scheduled to run daily via macOS launchd.

## Media Stack

Eight Docker containers managed by Compose:

| Service | Purpose | Default Port |
|---|---|---|
| Gluetun | VPN tunnel (ProtonVPN WireGuard) | — |
| qBittorrent | Download client (routed through VPN) | 8080 |
| ClamAV | Post-download malware scanner | 3310 |
| Prowlarr | Indexer management | 9696 |
| Radarr | Movie monitoring & automation | 7878 |
| Jellyfin | Media server / playback | 8096 |
| Bazarr | Automatic subtitles | 6767 |
| FlareSolverr | Cloudflare bypass for Prowlarr indexers | 8191 |

## Security

The stack enforces four security rules:

### Rule 1 — Only download video file formats
qBittorrent is configured to skip dangerous file extensions automatically:

```
Session\ExcludedFileNames=*.exe, *.bat, *.cmd, *.msi, *.com, *.scr, *.zip, *.rar, *.7z, *.iso
```

Safe formats (`.mkv`, `.mp4`, `.avi`) are allowed. A movie is never an executable — if it is, it's malware. Subtitle archives aren't needed through torrents since Bazarr handles subtitles separately.

### Rule 2 — ClamAV post-download scanning
Every completed torrent is automatically scanned by ClamAV before you open it:

- A dedicated `clamav` container runs the ClamAV daemon with auto-updating signatures.
- qBittorrent's AutoRun triggers `scripts/scan_download.sh` on each completed download.
- Scan results are logged to `mediaserver/data/torrents/scan.log`.
- If a threat is detected, the entry is flagged in the log for manual review.

You can also manually scan files:

```sh
# Via ClamAV container
clamdscan --host clamav:3310 /data/torrents/complete/some-movie
```

Or upload suspicious files to [VirusTotal](https://www.virustotal.com) for a second opinion.

### Rule 3 — VPN via Gluetun (mandatory for public trackers)
All qBittorrent traffic is routed through a VPN tunnel:

- **Gluetun** (`qmcgaw/gluetun`) runs as a container with `NET_ADMIN` and the TUN device.
- qBittorrent uses `network_mode: "service:gluetun"`, meaning it shares the VPN container's network namespace and has **no independent internet access**.
- Configured for ProtonVPN WireGuard — set credentials in `mediaserver/.env`.
- Your IP is never exposed to torrent swarms.

To configure ProtonVPN WireGuard credentials:

1. Log into [account.protonvpn.com](https://account.protonvpn.com)
2. Go to Downloads → WireGuard configuration
3. Extract the private key, address, server public key, server address, and port
4. Fill these into `mediaserver/.env` (see `mediaserver/.env.example`)

### Rule 4 — Kill switch (three layers)
If the VPN tunnel drops, **all torrent traffic stops** — your real IP is never exposed:

1. **Docker-native kill switch**: qBittorrent shares gluetun's network namespace. If the VPN goes down, qBittorrent loses **all** network access — there is no fallback to the host interface.
2. **Interface binding**: qBittorrent's `Connection\NetworkInterface` is set to `tun0` (the VPN tunnel). Even with an alternative route, qBittorrent would only use the tunnel.
3. **Gluetun firewall**: Gluetun runs an iptables firewall that blocks all traffic except through the VPN tunnel. When the VPN reconnects, traffic resumes through the new tunnel automatically.

**Verify the kill switch works**: after setup, stop the gluetun container and confirm that qBittorrent's WebUI becomes unreachable and no traffic flows.

## Prerequisites

- Python 3.9+
- Docker & Docker Compose
- A Letterboxd account with a public watchlist
- A TMDB API key ([themoviedb.org](https://www.themoviedb.org/settings/api))

## Setup

### 1. Clone and configure environment

```sh
git clone <repo-url> && cd project-toto
cp .env.example .env
```

Edit `.env` with your credentials:

```
LETTERBOXD_USERNAME=your_username
TMDB_API_KEY=your_key
RADARR_ENABLED=true
RADARR_API_KEY=your_radarr_api_key
RADARR_DRY_RUN=false
```

See `.env.example` for the full list of options.

### 2. Start the media stack

```sh
cp mediaserver/.env.example mediaserver/.env   # adjust PUID/PGID/TZ if needed
docker compose -f mediaserver/docker-compose.yml up -d
```

Verify the services are healthy:
- Prowlarr: http://localhost:9696
- Radarr: http://localhost:7878
- qBittorrent: http://localhost:8080
- Jellyfin: http://localhost:8096
- Bazarr: http://localhost:6767

Wire the services together through their UIs (Prowlarr → Radarr → qBittorrent, Bazarr → Radarr). See the [Servarr wiki](https://wiki.servarr.com/) for guidance.

### 3. Set up the Python environment

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Run a full sync

```sh
python scripts/sync_watchlist.py
```

This runs the entire pipeline: scrape → enrich → availability check → Radarr push.

### Check sync status

```sh
python scripts/status.py            # last 5 runs
python scripts/status.py --last 10  # last 10 runs
```

Exits with code 1 if the most recent run failed — useful in monitoring scripts.

### Schedule daily syncs (macOS)

```sh
cp scripts/dev.toto.sync.plist ~/Library/LaunchAgents/dev.toto.sync.plist
launchctl load ~/Library/LaunchAgents/dev.toto.sync.plist
```

To uninstall:

```sh
launchctl unload ~/Library/LaunchAgents/dev.toto.sync.plist
rm ~/Library/LaunchAgents/dev.toto.sync.plist
```

See `scripts/README.md` for more scheduling details.

### Jellyfin playback control

List active devices, search the library, or play a movie on a specific device:

```sh
python scripts/play.py devices                        # list active devices
python scripts/play.py search "inception"              # search movie library
python scripts/play.py start "inception" "bravia"      # play on TV (partial device name match)
```

Requires `JELLYFIN_API_KEY` and `JELLYFIN_USERNAME` in `.env`. Generate an API key in Jellyfin → Dashboard → API Keys.

### Telegram bot

Conversational movie concierge powered by Gemini 2.5 Flash Lite, with Jellyfin playback control:

```sh
PYTHONPATH=src python -m project_toto.bot
```

You can chat naturally with prompts like:
- "Something light, under 2 hours"
- "Play Inception on my TV"
- "Just pick something for me"

Available slash commands:
- `/start` — Show onboarding tips
- `/reset` — Clear conversation memory
- `/devices` — List active Jellyfin devices
- `/status` — Show last sync run and library stats

The bot supports inline device selection — when you ask to play a movie without specifying a device, it shows a button picker. It also enforces per-user rate limiting and optional access allowlists.

Requires `TELEGRAM_BOT_TOKEN` (from @BotFather) and `GEMINI_API_KEY` in `.env`.
The current shared settings loader also requires `LETTERBOXD_USERNAME` and `TMDB_API_KEY` to be present when launching the bot.

## Configuration Reference

All configuration is done through environment variables (`.env` file):

**Core**
- `LETTERBOXD_USERNAME` — Your Letterboxd username (required).
- `TMDB_API_KEY` — TMDB API key for metadata enrichment (required).
- `SQLITE_PATH` — Path to the SQLite database (default: `data/project_toto.db`).
- `LETTERBOXD_MAX_PAGES` — Max watchlist pages to scrape (default: `5`).

**Radarr**
- `RADARR_ENABLED` — Enable Radarr integration (default: `false`).
- `RADARR_URL` — Radarr base URL (default: `http://localhost:7878`).
- `RADARR_API_KEY` — Radarr API key (required if enabled).
- `RADARR_ROOT_FOLDER` — Root folder for movie storage (default: `/data/media/movies`).
- `RADARR_QUALITY_PROFILE_ID` — Quality profile ID (default: `1`).
- `RADARR_MONITORED` — Monitor added movies (default: `true`).
- `RADARR_SEARCH_ON_ADD` — Search immediately on add (default: `false`).
- `RADARR_DRY_RUN` — Log what would be added without pushing (default: `true`).

**Jellyfin**
- `JELLYFIN_URL` — Jellyfin server URL (default: `http://localhost:8096`).
- `JELLYFIN_API_KEY` — API key from Jellyfin dashboard (required for playback control).
- `JELLYFIN_USERNAME` — Jellyfin username for session/device resolution.

**JustWatch**
- `JUSTWATCH_ENABLED` — Enable availability lookups (default: `true`).
- `JUSTWATCH_COUNTRY` — 2-letter country code (default: `IN`).
- `JUSTWATCH_LANGUAGE` — Language for results (default: `en`).
- `JUSTWATCH_REFRESH_HOURS` — Re-check availability after N hours (default: `168`).
- `JUSTWATCH_MAX_RESULTS` — Max results per lookup (default: `3`).
- `JUSTWATCH_BEST_ONLY` — Only best offers per provider (default: `true`).

**Telegram Bot / Gemini**
- `TELEGRAM_BOT_TOKEN` — Bot token from @BotFather (required for bot).
- `GEMINI_API_KEY` — Google Gemini API key (required for recommendations).
- `TELEGRAM_ALLOWED_USER_IDS` — Optional comma-separated Telegram user IDs allowed to use the bot (default: empty = allow all).
- `TELEGRAM_ALLOWED_CHAT_IDS` — Optional comma-separated chat IDs allowed to use the bot (default: empty = allow all).
- `TELEGRAM_RATE_LIMIT_WINDOW_SECONDS` — Sliding rate-limit window in seconds (default: `20`; set `0` to disable).
- `TELEGRAM_RATE_LIMIT_MAX_MESSAGES` — Max messages allowed per window (default: `8`; set `0` to disable).

## Logging

Application logs are written to `data/logs/sync.log` (rotating, 5 MB max, 3 backups) and also printed to the console. When running under launchd, stdout/stderr goes to `data/logs/launchd_sync.log`.

All external API calls (TMDB, JustWatch, Letterboxd, Radarr) include automatic retry with exponential backoff on transient failures (connection errors and timeouts).

## Project Structure

```
project toto/
├── .env.example              # env var template
├── PRD.md                    # product requirements
├── requirements.txt          # python dependencies
├── data/
│   ├── project_toto.db       # sqlite database (gitignored)
│   └── logs/                 # application logs (gitignored)
├── mediaserver/
│   ├── docker-compose.yml    # gluetun, qbittorrent, clamav, prowlarr, radarr, jellyfin, bazarr
│   ├── scripts/
│   │   └── scan_download.sh  # post-download ClamAV scanner
│   ├── config/               # per-service config volumes (gitignored)
│   └── data/                 # media + torrent data (gitignored)
├── scripts/
│   ├── sync_watchlist.py     # main sync entrypoint
│   ├── status.py             # sync run health checker
│   ├── play.py               # jellyfin playback control
│   ├── dev.toto.sync.plist   # macOS launchd schedule
│   └── README.md             # script + scheduling docs
└── src/project_toto/
    ├── __init__.py           # package marker
    ├── config.py             # settings loader (.env → dataclass)
    ├── db.py                 # sqlite schema, sync runs, movie CRUD
    ├── letterboxd.py         # watchlist scraper
    ├── tmdb.py               # TMDB metadata enrichment
    ├── justwatch.py          # OTT availability via JustWatch GraphQL
    ├── radarr.py             # Radarr API client
    ├── sync.py               # pipeline orchestration
    ├── jellyfin.py           # Jellyfin playback client (play, pause, stop)
    ├── gemini.py             # Gemini 2.5 Flash Lite concierge with tool use
    ├── bot.py                # Telegram bot (polling, inline buttons, rate limiting)
    └── logging_config.py     # structured logging setup
```

## Roadmap

- [x] Phase 0 — Media stack foundation (Docker Compose)
- [x] Phase 1 — Watchlist parsing + TMDB enrichment + Radarr sync
- [x] Phase 2 — OTT availability via JustWatch
- [x] Phase 3 — Automation & reliability (logging, retries, scheduling, status CLI)
- [x] Phase 4 — Bazarr + subtitle automation
- [x] Phase 5 — Jellyfin multi-device playback control
- [x] Phase 6 — Telegram bot + Gemini-powered recommendations
- [ ] Phase 7 — Optional Raspberry Pi migration
- [ ] Phase 8 — Polish and public release
