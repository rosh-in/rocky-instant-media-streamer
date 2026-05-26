"""Rocky's dialogue — personality responses for non-Gemini paths.

Gemini handles all conversational questions now. These responses are used
by slash commands, the device picker, playback, and error messages.
"""
from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# Personality responses for state transitions and UI elements
# ---------------------------------------------------------------------------

ROCKY_PLAY_AMBIGUOUS = [
    "Rocky find more than one. You pick.",
    "Multiple match. Rocky show cards. You choose.",
]

ROCKY_PLAY_DIRECT = [
    "Amaze amaze amaze! {title}. Where watch?",
    "{title}. Rocky find. Where watch?",
    "Rocky play {title}. Pick device.",
]

ROCKY_NO_DEVICES = [
    "No active device. Open Jellyfin on device first.",
    "Rocky see no device. Turn on TV or open app?",
]

ROCKY_RESET = [
    "Rocky forget everything. Fresh start. — wipes brain —",
    "Brain empty. New beginning. Rocky ready.",
]

ROCKY_SHUFFLE = [
    "Rocky shuffle. Different three coming.",
    "New picks. Rocky try again.",
]

ROCKY_GREETING = [
    "Hey! What you want watch?",
    "Yo! Rocky here. What movie you in mood for?",
    "Rocky hear you! What you want watch tonight?",
    "Hey hey! Tell Rocky what kind of movie you want.",
    "Hello! Rocky ready. What you feel like watching?",
    "Yo yo! Rocky standing by. What you want see?",
]

ROCKY_AMAZE = "Amaze Amaze Amaze"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_rocky_response(category: str, **kwargs) -> str:
    """Pick a random Rocky response from a category, with optional formatting.

    Categories: play_ambiguous, play_direct, no_devices,
    reset, shuffle
    """
    category_map = {
        "play_ambiguous": ROCKY_PLAY_AMBIGUOUS,
        "play_direct": ROCKY_PLAY_DIRECT,
        "no_devices": ROCKY_NO_DEVICES,
        "reset": ROCKY_RESET,
        "shuffle": ROCKY_SHUFFLE,
        "greeting": ROCKY_GREETING,
    }
    options = category_map.get(category, ROCKY_SHUFFLE)
    template = random.choice(options)
    # Format with any provided kwargs (e.g., title="Inception")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
