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

## Goals (v1)
- Run a stable local media stack with:
  - Prowlarr (indexer management)
  - Radarr (movie monitoring + automation)
  - qBittorrent (downloads)
  - Jellyfin (streaming/playback)
- Keep a clean, consistent folder and container path structure.
- Ensure end-to-end automation works: request → download → import → stream.
- Keep setup easy to reproduce for GitHub documentation.

## Non-Goals (v1)
- No Jellyseerr in the first version.
- No cloud deployment.
- No multi-user access control beyond basic local usage.
- No advanced transcoding optimization initially.

## Users
Primary user: me (single-user setup on home network).

## Scope
### In Scope
- Docker Compose-based deployment for the four core services.
- Service wiring and validation.
- Basic operational docs and troubleshooting notes.
- Foundation for later automation phases.

### Future Scope
- Letterboxd watchlist scraper (Python) with scheduled sync.
- SQLite as source of truth for movie metadata and status.
- OTT availability lookup integration.
- Telegram bot assistant for recommendations and playback actions.
- Optional Raspberry Pi migration for always-on bot/automation jobs.

## Technical Decisions
- Container orchestration: Docker Compose.
- Shared canonical paths under a single `mediaserver` workspace.
- Internal service communication via Docker network + container names.
- Data store for automation phases: SQLite.
- API framework for later services/tools: FastAPI.

## Success Criteria
- All containers start and stay healthy.
- Radarr can send downloads to qBittorrent.
- Completed downloads are auto-imported into the movie library.
- Jellyfin detects and plays imported content.
- Setup can be reproduced from repo instructions without guesswork.

## Milestones
1. Environment + Compose setup
2. Service bring-up and UI validation
3. App wiring (Prowlarr ↔ Radarr ↔ qBittorrent ↔ Jellyfin)
4. End-to-end test with one movie

## Risks / Notes
- Path mismatches between containers can break import automation.
- Incorrect download client settings can stall Radarr processing.
- Mac networking/firewall settings may affect local access on some devices.

This is intentionally a practical v1. Once the pipeline is reliable, I’ll build the automation layers on top.
