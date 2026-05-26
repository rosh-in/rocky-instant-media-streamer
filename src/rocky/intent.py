"""Local intent classifier — DIRECT_PLAY and casual message detection.

Detects direct play requests ("play X", "watch X") which skip Gemini
to go straight to the device picker. Also detects casual/greeting
messages ("hey", "hi", "yo") which should get a local response asking
what the user wants to watch, instead of being sent to Gemini.
"""
from __future__ import annotations

import re

# Direct-play trigger patterns
_PLAY_PATTERNS = [
    r"^play\s+",
    r"^watch\s+(?:on\s+)?",
    r"^put\s+on\s+",
    r"^start\s+",
]

# Casual/greeting patterns — messages that are just small talk, not movie requests
_CASUAL_PATTERNS = [
    r"^(hey|hi|hello|yo|sup|what'?s?\s*up|howdy|heya|hola|ciao|bruh|bro|dude)[!\s]*$",
    r"^(hey|hi|hello|yo|sup|howdy|heya|hola|ciao)\s*(rocky|bot)?[!\s]*$",
    r"^(good\s*(morning|evening|night|afternoon)|gm|gn)[!\s]*$",
    r"^(thanks|thank\s*you|thx|ty|cheers)[!\s]*$",
    r"^(ok|okay|k|kk|cool|nice|sweet|awesome|great|lol|haha|😊|👍|🤙|✌️)[!\s]*$",
    r"^(bye|later|see\s*ya|cya|goodbye|peace|ttyl|gotta\s*go)[!\s]*$",
    r"^(yes|no|nope|nah|yep|yeah|ya)[!\s]*$",
    r"^(rocky|bot)[!\s?]*$",
]

# Keywords that indicate the user IS talking about movies (even in a short message)
_MOVIE_KEYWORDS = {
    "movie", "film", "watch", "something", "recommend", "suggest", "pick",
    "genre", "mood", "comedy", "horror", "action", "drama", "thriller",
    "sci-fi", "scifi", "romance", "animation", "documentary", "director",
    "actor", "funny", "scary", "sad", "happy", "light", "heavy", "epic",
    "bored", "tonight", "weekend", "tonight",
}


def is_direct_play(message: str) -> bool:
    """Check if message is a direct playback request: 'play X', 'watch X', etc."""
    lower = message.strip().lower()
    return any(re.search(pat, lower) for pat in _PLAY_PATTERNS)


def extract_play_title(message: str) -> str:
    """Extract the movie title from a direct-play message.

    'play Inception' → 'Inception'
    'watch The Dark Knight on TV' → 'The Dark Knight'
    """
    lower = message.strip()
    for pat in _PLAY_PATTERNS:
        lower = re.sub(pat, "", lower, count=1).strip()
    # Remove trailing device references
    lower = re.sub(r"\s+on\s+(?:my\s+)?(?:tv|phone|laptop|chrome|computer|ipad|iphone)\s*$", "", lower, flags=re.IGNORECASE)
    return lower.strip()


def is_casual_message(message: str) -> bool:
    """Check if message is a casual greeting or small talk with no movie intent.

    Returns True for 'hey', 'hi', 'yo', 'thanks', 'ok', etc.
    Returns False if the message contains movie-related keywords,
    even if it starts with a greeting (e.g., 'hey recommend a comedy').
    """
    lower = message.strip().lower()
    # If any movie keyword is present, it's not purely casual
    words = set(lower.split())
    if words & _MOVIE_KEYWORDS:
        return False
    # Check against casual patterns
    return any(re.search(pat, lower) for pat in _CASUAL_PATTERNS)
