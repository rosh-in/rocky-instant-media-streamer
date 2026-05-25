"""Local intent classifier — DIRECT_PLAY detection only.

Only detects direct play requests ("play X", "watch X") which skip Gemini
to go straight to the device picker. Everything else goes to the
Gemini brain for conversational handling.
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
