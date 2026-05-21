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

Four Docker containers managed by Compose:

| Service | Purpose | Default Port |
|---|---|---|
| Prowlarr | Indexer management | 9696 |
| Radarr | Movie monitoring & automation | 7878 |
| qBittorrent | Download client | 8080 |
| Jellyfin | Media server / playback | 8096 |

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

Wire the services together through their UIs (Prowlarr → Radarr → qBittorrent). See the [Servarr wiki](https://wiki.servarr.com/) for guidance.

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

**JustWatch**
- `JUSTWATCH_ENABLED` — Enable availability lookups (default: `true`).
- `JUSTWATCH_COUNTRY` — 2-letter country code (default: `IN`).
- `JUSTWATCH_LANGUAGE` — Language for results (default: `en`).
- `JUSTWATCH_REFRESH_HOURS` — Re-check availability after N hours (default: `168`).
- `JUSTWATCH_MAX_RESULTS` — Max results per lookup (default: `3`).
- `JUSTWATCH_BEST_ONLY` — Only best offers per provider (default: `true`).

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
│   ├── docker-compose.yml    # prowlarr, radarr, qbittorrent, jellyfin
│   ├── config/               # per-service config volumes (gitignored)
│   └── data/                 # media + torrent data (gitignored)
├── scripts/
│   ├── sync_watchlist.py     # main sync entrypoint
│   ├── status.py             # sync run health checker
│   ├── dev.toto.sync.plist   # macOS launchd schedule
│   └── README.md             # script + scheduling docs
└── src/project_toto/
    ├── config.py             # settings loader (.env → dataclass)
    ├── db.py                 # sqlite schema, sync runs, movie CRUD
    ├── letterboxd.py         # watchlist scraper
    ├── tmdb.py               # TMDB metadata enrichment
    ├── justwatch.py          # OTT availability via JustWatch GraphQL
    ├── radarr.py             # Radarr API client
    ├── sync.py               # pipeline orchestration
    └── logging_config.py     # structured logging setup
```

## Roadmap

- [x] Phase 0 — Media stack foundation (Docker Compose)
- [x] Phase 1 — Watchlist parsing + TMDB enrichment + Radarr sync
- [x] Phase 2 — OTT availability via JustWatch
- [x] Phase 3 — Automation & reliability (logging, retries, scheduling, status CLI)
- [ ] Phase 4 — Telegram bot for recommendations and playback
- [ ] Phase 5 — Optional Raspberry Pi migration
- [ ] Phase 6 — Polish and public release
