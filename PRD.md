# PRD: PROJECT TOTO (Personal Media Automation Project)
## Overview
I want to build a personal home media system that can automatically discover movies, download them through my existing pipeline, and make them instantly available in Jellyfin across my devices.

This project is meant to be practical and maintainable first, then expanded with automation features like watchlist sync and bot-based playback controls.

## Problem
Right now, deciding what to watch and getting it ready takes too many manual steps:
- checking availability across services
- manually searching and downloading
- managing files and playback separately

I want one workflow where my tools handle discovery, download, organization, and playback prep.

## Goals
- Run a stable local media stack with:
  - Gluetun (VPN tunnel for safe torrenting)
  - qBittorrent (downloads, routed through VPN)
  - ClamAV (post-download malware scanning)
  - Prowlarr (indexer management)
  - Radarr (movie monitoring + automation)
  - Jellyfin (streaming/playback)
  - Bazarr (automatic subtitle fetching)
  - FlareSolverr (Cloudflare bypass for Prowlarr)
- Keep a clean, consistent folder and container path structure.
- Ensure end-to-end automation works: request → download → scan → import → stream.
- Ensure security: VPN kill switch, malware scanning, dangerous file exclusion.
- Automate watchlist sync, OTT availability tracking, and playback control.
- Keep setup easy to reproduce for GitHub documentation.

## Non-Goals
- No Jellyseerr (request management UI not needed for single-user).
- No cloud deployment.
- No multi-user access control beyond basic local usage.
- No advanced transcoding optimization.
- No FastAPI web layer (data store and automation are CLI-driven for now).

## Users
Primary user: me (single-user setup on home network).

## Scope
### In Scope
- Docker Compose-based deployment for eight services (Gluetun, qBittorrent, ClamAV, Prowlarr, Radarr, Jellyfin, Bazarr, FlareSolverr).
- Service wiring and validation.
- Security: VPN kill switch, ClamAV scanning, dangerous file extension blocking.
- Letterboxd watchlist scraper with scheduled sync (macOS launchd).
- SQLite as source of truth for movie metadata, availability, and sync status.
- TMDB metadata enrichment for watchlist movies.
- JustWatch OTT availability lookup and refresh.
- Radarr integration for automated movie requests.
- Jellyfin multi-device playback control (CLI + bot).
- Telegram bot with Gemini 2.5 Flash Lite concierge, inline device picker, rate limiting, and access allowlists.
- Operational docs and troubleshooting notes.

### Future Scope
- Optional Raspberry Pi migration for always-on bot/automation jobs.
- Optional FastAPI web layer for dashboard or external integrations.
- Optional Jellyseerr for a request management UI.

## Technical Decisions
- Container orchestration: Docker Compose.
- Shared canonical paths under a single `mediaserver` workspace.
- Internal service communication via Docker network + container names.
- Data store: SQLite (lightweight, local, no server needed).
- Python automation: CLI scripts + Telegram bot (no web framework yet; FastAPI reserved for future use).
- AI concierge: Gemini 2.5 Flash Lite with automatic function calling (tool use).
- Bot framework: python-telegram-bot in polling mode.
- VPN: Gluetun with ProtonVPN WireGuard; qBittorrent shares VPN network namespace (kill switch).

## Success Criteria
- All eight containers start and stay healthy.
- qBittorrent traffic is routed exclusively through VPN (kill switch verified).
- Completed downloads are scanned by ClamAV automatically.
- Radarr can send downloads to qBittorrent.
- Completed downloads are auto-imported into the movie library.
- Jellyfin detects and plays imported content.
- Watchlist sync runs end-to-end: scrape → enrich → availability → Radarr push.
- Telegram bot responds to natural language and triggers playback.
- Setup can be reproduced from repo instructions without guesswork.

## Milestones
1. Environment + Compose setup ✅
2. Service bring-up and UI validation ✅
3. App wiring (Prowlarr ↔ Radarr ↔ qBittorrent ↔ Jellyfin) ✅
4. End-to-end test with one movie ✅
5. VPN + ClamAV security hardening ✅
6. Letterboxd scraper + TMDB + Radarr sync ✅
7. JustWatch OTT availability integration ✅
8. Automation & reliability (logging, retries, scheduling) ✅
9. Bazarr + FlareSolverr addition ✅
10. Jellyfin playback control CLI ✅
11. Telegram bot + Gemini concierge ✅

## Risks / Notes
- Path mismatches between containers can break import automation.
- Incorrect download client settings can stall Radarr processing.
- Mac networking/firewall settings may affect local access on some devices.

The original v1 (media stack foundation) is complete. All automation layers — watchlist sync, OTT tracking, playback control, and the Telegram concierge — are built and running. The project is now in maintenance/polish phase (Phase 8).
