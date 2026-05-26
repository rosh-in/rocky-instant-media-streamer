# ROCKY BOT 👾

> *Grace Rocky Save Movies*
> 
> *Watchlist to playing in under 3 minutes. No OTTs. No scrolling. Minimal Decisions to make.*

---

Rocky's personality is entirely inspired by Rocky from *Project Hail Mary*.

I suffer from severe decision fatigue trying to choose an OTT movie to watch. I'd scroll endlessly and end up not watching anything sometimes.

Rocky is a bot I made to solve that. I went from wasting 20 mins jumping across OTTs TO finding and playing a movie in under 2 minutes, just by asking Rocky what I feel like and tapping play.

---

## THIS IS HOW ROCKY DOES IT

Under the hood, Rocky reads my Letterboxd watchlist → downloads it, finds subtitles, and makes it ready to play on any device on my home network (TV, phone, or laptop).

**Rocky needs no manual intervention.**

Rocky gets all my watchlist movies ready while I sleep. 

I just have to ask Rocky via Telegram to help me select a movie from my watchlist based on what I feel like watching. Rocky would then recommend a few options. 

Rocky uses an LLM as his brain to help with complex recommendations like *"I feel like watching an action movie starring Arnold that's under 100 minutes."

* I just need to choose one and Rocky would immediately play the movie on the device I want in my home network, without me going and opening and typing there.

**LEAST CLICKS AND THINKING AS POSSIBLE.**

> Few disclaimers: Rocky is a pirate. Rocky is also safe, virus checks happen frequently.

---

## The Problem Rocky Solves

Before Rocky, finding a movie to watch meant:

- Manually searching across streaming services
- Opening up OTTs and scrolling without an idea of what to watch
- Downloading ans setting up media in certain scenarios
- Managing playback across multiple devices (TV, phone, laptop) with no unified control

Total time from *"I want to watch X"* to *"X is playing"* was 20+ minutes of active effort when instead I should rather be 20 minutes into watching something.

---

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
- **Control playback** — Chat with the Telegram bot: *"Play Inception on my TV"* or *"Something light, under 2 hours."* Gemini picks from your watchlist, finds the movie in Jellyfin, and plays it on the right device.
- **Understand your taste** — The bot builds a taste profile from your watch history (loved/liked/disliked genres, directors, countries) and uses it to make better recommendations over time.

The full pipeline runs as a single scheduled job (daily via macOS launchd) and requires zero day-to-day interaction.

---

## Future Product Vision

### Near-term
- **Raspberry Pi migration** — Run the bot and automation services 24/7 on a Pi so the Mac doesn't need to be awake for scheduled syncs.

### Medium-term
- **Household profiles** — Per-user taste profiles already exist. Next: let each family member have their own bot conversation with personalized recommendations, not just a shared session.
- **FastAPI dashboard** — A lightweight web layer for browse/search/status. Mobile browser access without Telegram.

### Long-term
- **Content-based recommendation engine** — Replace Gemini's watchlist-only picks with a collaborative or content-based model that can surface movies *outside* the watchlist based on taste patterns.

---

## Rocky's Tech Stack

| Layer | Tools |
|---|---|
| Media server | Jellyfin, Radarr, qBittorrent, Prowlarr, Bazarr, FlareSolverr |
| VPN & security | Gluetun VPN, ClamAV |
| AI brain | Gemini 2.5 Flash Lite |
| Bot interface | Telegram Bot |
| Storage | SQLite, ChromaDB |
| Data sources | Letterboxd watchlist, TMDB API, JustWatch API |
| Playback control | Android ADB (to play without manual intervention) |
| Infrastructure | Docker |

---

<details>
<summary><strong>Technical Reference</strong></summary>
        
## Security

Rocky is a pirate, but a careful one.

### Rule 1 — Only download video file formats

qBittorrent is configured to skip dangerous file extensions automatically:
```
*.exe, *.bat, *.cmd, *.msi, *.com, *.scr, *.zip, *.rar, *.7z, *.iso
```
Safe formats (`.mkv`, `.mp4`, `.avi`) are allowed. A movie is never an executable — if it is, it's malware.

### Rule 2 — ClamAV post-download scanning

Every completed torrent is automatically scanned before you open it. Scan results are logged to `mediaserver/data/torrents/scan.log`. Threats are flagged for manual review.

```bash
# Manual scan via ClamAV container
clamdscan --host clamav:3310 /data/torrents/complete/some-movie
```

### Rule 3 — VPN via Gluetun

All qBittorrent traffic is routed through a VPN tunnel. qBittorrent uses `network_mode: "service:gluetun"` — it shares the VPN container's network namespace and has **no independent internet access**. Your IP is never exposed to torrent swarms.

### Rule 4 — Kill switch (three layers)

If the VPN tunnel drops, all torrent traffic stops:

1. **Docker network namespace** — qBittorrent loses all network access if gluetun goes down.
2. **Interface binding** — qBittorrent's connection interface is set to `tun0`. Even with an alternative route, it only uses the tunnel.
3. **Gluetun firewall** — iptables blocks all traffic except through the VPN tunnel.

---

## Prerequisites

- Python 3.9+
- Docker & Docker Compose
- A Letterboxd account with a public watchlist
- A TMDB API key ([themoviedb.org](https://www.themoviedb.org/settings/api))

---

## Setup

### 1. Clone and configure

```bash
git clone <repo-url> && cd rocky
cp .env.example .env
```

Edit `.env` with your credentials:

```env
LETTERBOXD_USERNAME=your_username
TMDB_API_KEY=your_key
RADARR_ENABLED=true
RADARR_API_KEY=your_radarr_api_key
RADARR_DRY_RUN=false
```

See `.env.example` for the full list of options.

### 2. Start the media stack

```bash
cp mediaserver/.env.example mediaserver/.env
docker compose -f mediaserver/docker-compose.yml up -d
```

Verify services are up:

| Service | URL |
|---|---|
| Prowlarr | http://localhost:9696 |
| Radarr | http://localhost:7878 |
| qBittorrent | http://localhost:8080 |
| Jellyfin | http://localhost:8096 |
| Bazarr | http://localhost:6767 |

Wire the services together through their UIs (Prowlarr → Radarr → qBittorrent, Bazarr → Radarr). See the [Servarr wiki](https://wiki.servarr.com/) for guidance.

### 3. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

### Run a full sync

```bash
python scripts/sync_watchlist.py
```

Scrape → enrich → availability check → Radarr push. The whole pipeline.

### Check sync status

```bash
python scripts/status.py            # last 5 runs
python scripts/status.py --last 10  # last 10 runs
```

Exits with code 1 if the most recent run failed — useful in monitoring scripts.

### Schedule daily syncs (macOS)

```bash
cp scripts/dev.rocky.sync.plist ~/Library/LaunchAgents/dev.rocky.sync.plist
launchctl load ~/Library/LaunchAgents/dev.rocky.sync.plist
```

### Jellyfin playback control

```bash
python scripts/play.py devices                     # list active devices
python scripts/play.py search "inception"           # search movie library
python scripts/play.py start "inception" "bravia"   # play on TV
```

Requires `JELLYFIN_API_KEY` and `JELLYFIN_USERNAME` in `.env`.

### Telegram bot

```bash
PYTHONPATH=src python -m rocky.bot
```

Talk to Rocky naturally:

- *"Something light, under 2 hours"*
- *"Play Inception on my TV"*
- *"Just pick something for me"*

**Slash commands:**

| Command | What it does |
|---|---|
| `/start` | Show onboarding tips |
| `/reset` | Clear conversation memory |
| `/devices` | List active Jellyfin devices |
| `/status` | Last sync run + library stats |
| `/stats` | Watchlist progress card |
| `/watched <title>` | Log a movie as watched |

**Intent routing:** *"Play X"* and *"Watch X"* skip Gemini and go straight to the device picker. Everything else goes to the Gemini brain.

**ADB phone wake:** If `ADB_PHONE_IP` is configured, Rocky automatically wakes the phone screen, unlocks it, and launches the Jellyfin app before showing the device picker.

Requires `TELEGRAM_BOT_TOKEN` (from @BotFather) and `GEMINI_API_KEY` in `.env`.

---

## Configuration Reference

### Core

| Variable | Description |
|---|---|
| `LETTERBOXD_USERNAME` | Your Letterboxd username (required) |
| `TMDB_API_KEY` | TMDB API key (required) |
| `SQLITE_PATH` | SQLite database path (default: `data/rocky.db`) |
| `LETTERBOXD_MAX_PAGES` | Max watchlist pages to scrape (default: `5`) |

### Radarr

| Variable | Description |
|---|---|
| `RADARR_ENABLED` | Enable Radarr integration (default: `false`) |
| `RADARR_URL` | Radarr base URL (default: `http://localhost:7878`) |
| `RADARR_API_KEY` | Radarr API key (required if enabled) |
| `RADARR_DRY_RUN` | Log what would be added, without pushing (default: `true`) |

### Jellyfin

| Variable | Description |
|---|---|
| `JELLYFIN_URL` | Jellyfin server URL (default: `http://localhost:8096`) |
| `JELLYFIN_API_KEY` | API key from Jellyfin dashboard |
| `JELLYFIN_USERNAME` | Jellyfin username for device resolution |

### Telegram Bot / Gemini

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (required) |
| `GEMINI_API_KEY` | Google Gemini API key (required) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs allowed to use the bot |
| `TELEGRAM_RATE_LIMIT_WINDOW_SECONDS` | Sliding rate-limit window (default: `20`) |
| `TELEGRAM_RATE_LIMIT_MAX_MESSAGES` | Max messages per window (default: `8`) |

### ADB

| Variable | Description |
|---|---|
| `ADB_PHONE_IP` | Phone IP for wireless ADB (enables phone wake) |
| `ADB_PHONE_PACKAGE` | Android package to launch (default: `org.jellyfin.mobile`) |

See `.env.example` for the full list.

---

## Project Structure

```
rocky/
├── .env.example
├── PRD.md
├── data/
│   ├── rocky.db                  # SQLite database (gitignored)
│   └── logs/                     # Application logs (gitignored)
├── mediaserver/
│   ├── docker-compose.yml        # All 8 containers
│   ├── scripts/
│   │   └── scan_download.sh      # Post-download ClamAV scanner
│   ├── config/                   # Per-service config volumes (gitignored)
│   └── data/                     # Media + torrent data (gitignored)
├── scripts/
│   ├── sync_watchlist.py         # Main sync entrypoint
│   ├── status.py                 # Sync run health checker
│   ├── play.py                   # Jellyfin playback control
│   ├── run_webhook.py            # FastAPI webhook launcher
│   ├── dev.rocky.sync.plist      # macOS launchd schedule
│   └── README.md
└── src/rocky/
    ├── config.py                 # Settings loader
    ├── db.py                     # SQLite schema + CRUD
    ├── letterboxd.py             # Watchlist scraper
    ├── tmdb.py                   # TMDB metadata enrichment
    ├── justwatch.py              # OTT availability (JustWatch GraphQL)
    ├── radarr.py                 # Radarr API client
    ├── sync.py                   # Pipeline orchestration
    ├── jellyfin.py               # Jellyfin playback client
    ├── gemini.py                 # Gemini brain + tool declarations
    ├── intent.py                 # Local intent classifier
    ├── rocky_dialogue.py         # Rocky personality responses
    ├── adb_controller.py         # ADB controller (wake, unlock, launch)
    ├── bot.py                    # Telegram bot
    ├── stats.py                  # Watchlist progress card
    ├── taste_profile.py          # Per-user taste profile
    ├── webhook.py                # FastAPI webhook server
    ├── vector_store.py           # ChromaDB semantic search
    └── logging_config.py         # Structured logging
```

---

*Grace Rocky Save Movies 👾*
