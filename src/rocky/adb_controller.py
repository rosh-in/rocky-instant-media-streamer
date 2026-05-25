"""ADB controller — wake screen, unlock, and launch Jellyfin on Android phone over WiFi."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("rocky.adb")


async def ensure_connected(phone_ip: str, timeout: int = 10) -> bool:
    """Make sure ADB is connected to the phone over WiFi.

    If not already connected, attempt to connect. Returns True on success.
    """
    target = f"{phone_ip}:5555"

    # Check if already connected
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=timeout,
        )
        if target in result.stdout:
            logger.info("ADB already connected to %s", target)
            return True
    except Exception as exc:
        logger.warning("ADB devices check failed: %s", exc)

    # Attempt connection
    try:
        result = subprocess.run(
            ["adb", "connect", target],
            capture_output=True, text=True, timeout=timeout,
        )
        if "connected" in result.stdout.lower() or "already connected" in result.stdout.lower():
            logger.info("ADB connected to %s", target)
            return True
        logger.warning("ADB connect failed: %s", result.stdout.strip())
        return False
    except subprocess.TimeoutExpired:
        logger.error("ADB connect timed out for %s", target)
        return False
    except Exception as exc:
        logger.error("ADB connect error: %s", exc)
        return False


async def wake_and_launch(
    phone_ip: str,
    package: str,
    activity: str,
    *,
    wait: float = 4.0,
    timeout: int = 10,
) -> bool:
    """Wake phone screen, unlock (swipe up), and launch Jellyfin app.

    Returns True if all steps succeeded, False otherwise.
    """
    target = f"{phone_ip}:5555"

    # Step 1 — Ensure connected
    if not await ensure_connected(phone_ip, timeout=timeout):
        logger.error("Cannot connect to phone at %s — is wireless debugging on?", phone_ip)
        return False

    def _adb_shell(*args: str) -> bool:
        """Run an ADB shell command. Returns True on success."""
        try:
            result = subprocess.run(
                ["adb", "-s", target, "shell"] + list(args),
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                logger.warning("ADB shell %s failed: %s", args, result.stderr.strip())
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("ADB shell %s timed out", args)
            return False
        except Exception as exc:
            logger.error("ADB shell %s error: %s", args, exc)
            return False

    # Step 2 — Wake screen
    if not _adb_shell("input", "keyevent", "KEYCODE_WAKEUP"):
        return False
    logger.info("Screen woken on %s", target)

    # Small pause after wake so the screen is responsive
    await asyncio.sleep(0.5)

    # Step 3 — Unlock screen (swipe up)
    if not _adb_shell("input", "swipe", "500", "1500", "500", "500"):
        return False
    logger.info("Screen unlocked on %s", target)

    # Small pause after unlock
    await asyncio.sleep(0.5)

    # Step 4 — Launch Jellyfin app
    component = f"{package}/{activity}"
    if not _adb_shell("am", "start", "-n", component):
        return False
    logger.info("Launched %s on %s", component, target)

    # Step 5 — Wait for app to load and register Jellyfin session
    await asyncio.sleep(wait)
    return True


async def is_phone_reachable(phone_ip: str, timeout: int = 5) -> bool:
    """Quick check if the phone is reachable via ADB (without launching anything)."""
    return await ensure_connected(phone_ip, timeout=timeout)
