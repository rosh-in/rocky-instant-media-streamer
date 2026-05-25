# Rocky

A zero-friction home media system — from watchlist to watching in one tap.

I built Rocky to solve a real problem in my home: the frustrating gap between "I want to watch something" and "it's actually playing on my TV." What started as a personal automation project evolved through seven iterative phases into a complete product — from infrastructure to AI-powered interaction — and became the case study I use to demonstrate how I think about building products.

## The Problem

Deciding what to watch and getting it ready involved a fragmented workflow:
- Checking what's on my Letterboxd watchlist, then manually searching across streaming services
- Downloading and organizing files separately, then hoping Jellyfin picks them up
- Managing playback across multiple devices (TV, phone, laptop) with no unified control

Each step worked in isolation, but the handoffs were manual, unreliable, and slow. The total time from "I want to watch X" to "X is playing" was 20+ minutes of active effort — almost always on the couch when nobody wants to be doing admin work.

## The Product

Rocky eliminates every manual step in the watchlist-to-watching pipeline:

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

- **Discover** — Scrapes your Letterboxd watchlist and enriches each movie with TMDB metadata (posters, genres, trailers, mood tags, cast, directors).
- **Track availability** — Queries JustWatch to know which OTT platforms carry each title, so you never pay for something already streaming.
- **Automate delivery** — Pushes untracked movies to Radarr, which hands off to qBittorrent → Jellyfin. New movies appear in your library without you touching anything.
- **Control playback** — Chat with the Telegram bot: "Play Inception on my TV" or "Something light, under 2 hours." Gemini picks from your watchlist, finds the movie in Jellyfin, and plays it on the right device.
- **Understand your taste** — The bot builds a taste profile from your watch history (loved/liked/disliked genres, directors, countries) and uses it to make better recommendations over time.

The full pipeline runs as a single scheduled job (daily via macOS launchd) and requires zero day-to-day interaction.

## Key Product Decisions & Trade-offs

### SQLite over Postgres
A media library for a household doesn't need a server process. SQLite is zero-config, runs embedded, and handles the read-heavy query pattern perfectly. The trade-off: no built-in concurrency — but only one sync job runs at a time, so it's the right constraint for the problem.

### Gemini Flash Lite over larger models
The concierge runs on Gemini 2.5 Flash Lite (free tier: 15 RPM). I built a proactive RPM tracker and retry system with exponential backoff so the bot stays within quota without users ever seeing a delay. The trade-off: less nuanced reasoning than larger models — but for "pick a movie from this list," speed and cost win over depth.

### Telegram bot over a custom web UI
Building a conversational interface via Telegram gave me a fully hosted, cross-platform UI with push notifications, inline buttons, and auth — for free. The trade-off: less layout control — but the goal was reducing friction to zero, and everyone already has Telegram open.

### Dry-run by default for Radarr
New installs run Radarr in dry-run mode (logs what *would* be added without pushing). This is a safety-first default: one wrong config could flood your library. Users opt into live mode explicitly. The trade-off: extra step to go live — but it prevents the worst failure mode on first use.

### VPN kill switch with three redundant layers
Security isn't a feature you ship once — it's a property that must hold even when things break. The kill switch uses Docker network namespacing + interface binding + iptables firewall so that if *any* layer fails, the others still block traffic. The trade-off: harder to debug network issues — but the guarantee (IP never exposed) is worth the operational complexity.

### What I deprioritized
- **Jellyseerr (request management UI)** — Overkill for a household. A Telegram bot handles the same job with less setup.
- **Cloud deployment** — Home media is inherently local. Cloud adds latency and cost for no user benefit.
- **Multi-user access control** — The bot's optional allowlist is sufficient. Full RBAC is scope creep for a family setup.
- **Web dashboard** — Would be nice, but the CLI + bot + Jellyfin UI already cover all use cases. A FastAPI layer is reserved for future expansion.

## Impact & Metrics

The system has been running daily since Phase 1:

- **End-to-end automation**: Watchlist sync → metadata enrichment → availability check → Radarr push runs unattended daily. Zero manual intervention.
- **Sync observability**: Every run is tracked in `sync_runs` with seen/enriched/availability/requested counts and success/failure status. The `status.py` CLI surfaces this instantly.
- **Reliability**: All external API calls (TMDB, JustWatch, Letterboxd, Radarr) retry with exponential backoff on transient failures. Structured rotating logs make debugging deterministic.
- **Security posture**: 3-layer VPN kill switch verified — qBittorrent has no internet path outside the tunnel. ClamAV scans every completed download. Dangerous file extensions are excluded at the client level.
- **Household adoption**: Family members use the Telegram bot for movie discovery and playback without needing to understand any underlying service. The bot's natural-language interface reduced the learning curve to zero.
- **Taste intelligence**: The bot builds per-user taste profiles from watch history (genre patterns, director preferences, country affinity) and feeds them into recommendations — the system gets better the more you use it.

## How I Built This — 0 to 1 in 7 Phases

I treated this like a product launch: each phase had a clear scope, success criteria, and user-facing outcome.

- **Phase 0 — Infrastructure**: Docker Compose stack (8 containers). Shipped first because nothing else works without a stable foundation.
- **Phase 1 — Core pipeline**: Letterboxd scraper → TMDB enrichment → SQLite → Radarr push. The minimum viable loop: "I add a movie, it appears in Jellyfin."
- **Phase 2 — Availability intelligence**: JustWatch integration. Now the system knows *where* a movie is available, not just *that* it exists. Prevents unnecessary downloads when a title is already streaming.
- **Phase 3 — Reliability**: Logging, retries, scheduled execution, status CLI. Made the pipeline trustworthy enough to run unattended.
- **Phase 4 — Subtitles**: Bazarr + FlareSolverr. Small addition, outsized impact on non-English households.
- **Phase 5 — Playback control**: Jellyfin API client with multi-device support. For the first time, you could trigger playback without leaving the couch.
- **Phase 6 — Conversational interface**: Telegram bot + Gemini concierge with tool use, inline device picker, taste profiles, and rate limiting. The "aha" moment — the system went from automated to intelligent.

Each phase was incremental and backward-compatible. No rewrites, no breaking changes. The roadmap is documented in [ROADMAP.md](ROADMAP.md) and the product spec in [PRD.md](PRD.md).

## Future Product Vision

### Near-term
- **Watch history loop** — Jellyfin webhooks already log playback events into `watch_history`. Next: auto-remove watched movies from the watchlist, auto-refresh availability, and surface "you watched X last week" in bot recommendations.
- **Raspberry Pi migration** — Run the bot and automation services 24/7 on a Pi so the Mac doesn't need to be awake for scheduled syncs.

### Medium-term
- **Household profiles** — Per-user taste profiles already exist. Next: let each family member have their own bot conversation with personalized recommendations, not just a shared session.
- **FastAPI dashboard** — A lightweight web layer for browse/search/status, using the existing SQLite data and webhook infrastructure. Enables mobile browser access without Telegram.
- **Social features** — Letterboxd friends' watchlists as discovery signals: "Your friend Alex just added X."

### Long-term
- **Content-based recommendation engine** — Replace Gemini's watchlist-only picks with a collaborative or content-based model that can surface movies *outside* the watchlist based on taste patterns.
- **Multi-home support** — The architecture already separates the automation layer from the media layer. With a FastAPI API and shared DB, two households could run independent stacks with a shared recommendation signal.

<details>
<summary><strong>Technical Reference</strong></summary>

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
git clone <repo-url> && cd rocky
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
cp scripts/dev.rocky.sync.plist ~/Library/LaunchAgents/dev.rocky.sync.plist
launchctl load ~/Library/LaunchAgents/dev.rocky.sync.plist
```

To uninstall:

```sh
launchctl unload ~/Library/LaunchAgents/dev.rocky.sync.plist
rm ~/Library/LaunchAgents/dev.rocky.sync.plist
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
PYTHONPATH=src python -m rocky.bot
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
- `SQLITE_PATH` — Path to the SQLite database (default: `data/rocky.db`).
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
Rocky/
├── .env.example              # env var template
├── PRD.md                    # product requirements
├── ROADMAP.md                # phased execution tracker
├── requirements.txt          # python dependencies
├── data/
│   ├── rocky.db       # sqlite database (gitignored)
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
│   ├── run_webhook.py        # FastAPI webhook launcher
│   ├── dev.rocky.sync.plist   # macOS launchd schedule
│   └── README.md             # script + scheduling docs
└── src/rocky/
    ├── __init__.py           # package marker
    ├── config.py             # settings loader (.env → dataclass)
    ├── db.py                 # sqlite schema, sync runs, movie CRUD, mood tags
    ├── letterboxd.py         # watchlist scraper
    ├── tmdb.py               # TMDB metadata enrichment
    ├── justwatch.py          # OTT availability via JustWatch GraphQL
    ├── radarr.py             # Radarr API client
    ├── sync.py               # pipeline orchestration
    ├── jellyfin.py           # Jellyfin playback client (play, pause, stop)
    ├── gemini.py             # Gemini 2.5 Flash Lite concierge with tool use + RPM management
    ├── bot.py                # Telegram bot (polling, inline buttons, rate limiting)
    ├── stats.py              # watchlist progress card generator
    ├── taste_profile.py      # per-user taste profile from watch history
    ├── webhook.py            # FastAPI webhook server (Jellyfin + Radarr events)
    └── logging_config.py     # structured logging setup
```

</details>
