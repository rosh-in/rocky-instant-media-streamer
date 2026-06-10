"""Rocky's dialogue — personality responses for non-Gemini paths.

Gemini handles all conversational questions now. These responses are used
by slash commands, the device picker, playback, and error messages.

Voice: PHM Rocky — alien scientist from Erid, curious, wonder-filled,
choppy sentences, earnest but not goofy.
"""
from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# Personality responses for state transitions and UI elements
# ---------------------------------------------------------------------------

ROCKY_PLAY_AMBIGUOUS = [
    "Rocky observe multiple matches. Interesting. You choose.",
    "More than one result. Rocky show cards. You select.",
]

ROCKY_PLAY_DIRECT = [
    "Rocky locate this one: {title}. Which device for playback?",
    "{title}. Rocky find this. Where do you want to watch?",
    "Rocky identify {title}. Specify device, please.",
]

ROCKY_NO_DEVICES = [
    "No active device detected. Open Jellyfin on a device first.",
    "Rocky scan… no device. Turn on TV or open app, then try.",
]

ROCKY_RESET = [
    "Rocky clear all memory. Fresh start. — wipes brain — ",
    "Data reset. New beginning. Rocky ready.",
]

ROCKY_SHUFFLE = [
    "Rocky shuffle. Different selections coming.",
    "New picks. Rocky try again.",
]

ROCKY_GREETING = [
    "Rocky here. What kind of cinema you want to observe?",
    "Greetings. Rocky standing by. What film you in mood for?",
    "Rocky present. What you wish to watch tonight?",
    "Hello. Rocky ready to assist. What genre interests you?",
    "Rocky hears you. What type of movie you want to experience?",
    "Rocky online. Tell Rocky what you feel like watching.",
]

ROCKY_POST_PLAY = [
    "{title} now playing. Rocky curious — what you want to watch next?",
    "{title} on screen. Rocky stand by for next selection.",
    "{title} is running. When ready, tell Rocky what next.",
]

ROCKY_AMAZE = "Fascinating."


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_rocky_response(category: str, **kwargs) -> str:
    """Pick a random Rocky response from a category, with optional formatting.

    Categories: play_ambiguous, play_direct, no_devices,
    reset, shuffle, greeting, post_play
    """
    category_map = {
        "play_ambiguous": ROCKY_PLAY_AMBIGUOUS,
        "play_direct": ROCKY_PLAY_DIRECT,
        "no_devices": ROCKY_NO_DEVICES,
        "reset": ROCKY_RESET,
        "shuffle": ROCKY_SHUFFLE,
        "greeting": ROCKY_GREETING,
        "post_play": ROCKY_POST_PLAY,
    }
    options = category_map.get(category, ROCKY_SHUFFLE)
    template = random.choice(options)
    # Format with any provided kwargs (e.g., title="Inception")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
