"""Control Jellyfin playback from the terminal.

Usage:
    python scripts/play.py devices              List active devices
    python scripts/play.py search "query"       Search movie library
    python scripts/play.py start "title" "dev"  Play a movie on a device
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from rocky.config import load_settings
from rocky.jellyfin import JellyfinClient
from rocky.logging_config import setup_logging


def _build_client() -> JellyfinClient:
    settings = load_settings()
    if not settings.jellyfin_api_key:
        print("JELLYFIN_API_KEY is not set. Generate one in Jellyfin → Dashboard → API Keys.")
        sys.exit(1)
    if not settings.jellyfin_username:
        print("JELLYFIN_USERNAME is not set.")
        sys.exit(1)
    return JellyfinClient(
        base_url=settings.jellyfin_url,
        api_key=settings.jellyfin_api_key,
        username=settings.jellyfin_username,
    )


def cmd_devices(_args: argparse.Namespace) -> None:
    client = _build_client()
    devices = client.list_devices()
    if not devices:
        print("No active devices found. Open Jellyfin on a device first.")
        return
    print(f"Active devices ({len(devices)}):\n")
    for d in devices:
        print(f"  • {d.label}")
        print(f"    Session: {d.session_id}")
        print()


def cmd_search(args: argparse.Namespace) -> None:
    client = _build_client()
    movies = client.search_movies(args.query)
    if not movies:
        print(f"No movies found for '{args.query}'.")
        return
    print(f"Results for '{args.query}':\n")
    for m in movies:
        year_str = f" ({m.year})" if m.year else ""
        print(f"  • {m.name}{year_str}")
        print(f"    ID: {m.item_id}")
        if m.overview:
            print(f"    {m.overview}")
        print()


def cmd_start(args: argparse.Namespace) -> None:
    client = _build_client()

    # Find movie
    movies = client.search_movies(args.title, limit=5)
    if not movies:
        print(f"No movie found for '{args.title}'.")
        sys.exit(1)
    movie = movies[0]

    # Find device
    devices = client.list_devices()
    if not devices:
        print("No active devices found.")
        sys.exit(1)

    target = args.device.lower()
    match = None
    for d in devices:
        if target in d.device_name.lower() or target in d.client.lower():
            match = d
            break
    if not match:
        print(f"No device matching '{args.device}'. Active devices:")
        for d in devices:
            print(f"  • {d.label}")
        sys.exit(1)

    year_str = f" ({movie.year})" if movie.year else ""
    print(f"Playing: {movie.name}{year_str}")
    print(f"Device:  {match.label}")
    client.play(session_id=match.session_id, item_id=movie.item_id)
    print("✅ Play command sent.")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Jellyfin playback control.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="List active Jellyfin devices.")

    sp_search = sub.add_parser("search", help="Search the Jellyfin movie library.")
    sp_search.add_argument("query", help="Movie title to search for.")

    sp_start = sub.add_parser("start", help="Play a movie on a device.")
    sp_start.add_argument("title", help="Movie title (best match will be used).")
    sp_start.add_argument("device", help="Device name to play on (partial match).")

    args = parser.parse_args()

    handlers = {"devices": cmd_devices, "search": cmd_search, "start": cmd_start}
    handlers[args.command](args)


if __name__ == "__main__":
    main()
