"""
Telegram premium (custom) emoji.

Messages use tg_emoji("KEY") inside HTML text; buttons use button_label() and
icon_id(). Custom emoji show only when the bot's owner has Telegram Premium
(or the bot owns Fragment usernames); everyone else sees the fallback emoji.

- An empty ID means the plain fallback emoji is always used.
- Override one ID without editing code: CUSTOM_EMOJI_<KEY>=<id> in .env
  (an empty value turns that one emoji off).
- Turn all custom emoji off: DISABLE_CUSTOM_EMOJI=true in .env

The fallback must be the emoji the custom emoji was made for (the "emoji"
field of the sticker), not just a similar-looking one.
"""

import os
from typing import Optional

from config import DISABLE_CUSTOM_EMOJI

# KEY: (custom emoji ID, fallback emoji)
_EMOJI: dict[str, tuple[str, str]] = {
    # Branding
    "WELCOME": ("", "👋"),
    "ENJOY": ("", "🎉"),
    # Platforms
    "YOUTUBE": ("5278611117130653414", "📺"),
    "TIKTOK": ("5877403629199036187", "❤️"),
    # Welcome and help
    "LINK": ("5271604874419647061", "🔗"),
    "POINT_DOWN": ("6059936951845266219", "⬇️"),
    "QUESTION": ("5436113877181941026", "❓"),
    "INFO": ("5334544901428229844", "ℹ️"),
    "KEYBOARD": ("5841359499146825803", "⌨️"),
    # Progress
    "SEARCH": ("5231012545799666522", "🔍"),
    "HOURGLASS": ("5451732530048802485", "⏳"),
    "VIDEO": ("5937999673510858217", "🎬"),
    "TIMER": ("5382194935057372936", "⏱"),
    "DOWNLOAD": ("5433811242135331842", "📥"),
    "UPLOAD": ("5445355530111437729", "📤"),
    "GEAR": ("5341715473882955310", "⚙️"),
    "CHECK": ("5206607081334906820", "✔️"),
    "ZAP": ("5456140674028019486", "⚡️"),
    "CROSS": ("5210952531676504517", "❌"),
    "WARNING": ("5447644880824181073", "⚠️"),
    # Buttons
    "BEST": ("5427168083074628963", "💎"),
    "MP3": ("5463107823946717464", "🎵"),
    "FLAC": ("5415730881518126908", "🎼"),
}


def _resolve(key: str) -> tuple[str, str]:
    custom_id, fallback = _EMOJI[key]
    override = os.environ.get(f"CUSTOM_EMOJI_{key}")
    if override is not None:
        return override.strip(), fallback
    if DISABLE_CUSTOM_EMOJI:
        return "", fallback
    return custom_id, fallback


def tg_emoji(key: str) -> str:
    """HTML for message text: the custom emoji, or its plain fallback."""
    custom_id, fallback = _resolve(key)
    if not custom_id:
        return fallback
    return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'


def icon_id(key: str) -> Optional[str]:
    """Custom emoji ID for a button's icon_custom_emoji_id, if one is active."""
    return _resolve(key)[0] or None


def button_label(key: str, text: str) -> str:
    """
    Button text. With a custom icon Telegram draws the emoji itself, so the
    text stays plain; otherwise the fallback emoji is put in front.
    """
    custom_id, fallback = _resolve(key)
    return text if custom_id else f"{fallback} {text}"
