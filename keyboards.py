import hashlib
import json
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import KeyboardButtonStyle

import downloader
from downloader import AUDIO_CODECS, Choice, VideoInfo, audio_choice, video_choice
from emojis import button_label, icon_id
from texts import human_size

# Two per row leaves room for the file size on each button
_BUTTONS_PER_ROW = 2


def link_id(url: str) -> str:
    """Short, stable key for a link — used in callback_data and chat_data."""
    return hashlib.md5(url.encode()).hexdigest()[:8]


def _make_callback(label: str, link: str) -> str:
    """
    Build callback_data that fits in Telegram's 64-byte limit.
    Only the button label and the link ID are encoded here.
    The actual download choice is looked up from chat_data by label.

    Longest possible output: {"l":"1080p","h":"12345678"} = 29 bytes.
    """
    return json.dumps({"l": label, "h": link}, separators=(",", ":"))


def _with_size(text: str, size: Optional[int]) -> str:
    if not size:
        return text
    if size > downloader.upload_limit:
        return f"{text} · too big"
    return f"{text} · ~{human_size(size)}"  # an estimate, before download and conversion


def build_quality_keyboard(info: VideoInfo, link: str) -> tuple[InlineKeyboardMarkup, dict[str, Choice]]:
    """
    Build the download keyboard: video qualities first, MP3 / FLAC below them,
    each with its expected file size when known.

    Returns:
        keyboard — InlineKeyboardMarkup to show the user
        choices  — dict mapping button label -> Choice
                   (stored in chat_data; looked up when a button is tapped)
    """
    choices: dict[str, Choice] = {}

    def button(choice: Choice, text: str, emoji: Optional[str] = None, style: Optional[str] = None) -> InlineKeyboardButton:
        choices[choice.label] = choice
        text = _with_size(text, info.sizes.get(choice.label))
        return InlineKeyboardButton(
            button_label(emoji, text) if emoji else text,
            callback_data=_make_callback(choice.label, link),
            icon_custom_emoji_id=icon_id(emoji) if emoji else None,
            style=style,
        )

    video_buttons = [button(video_choice(info), "Best", "BEST", KeyboardButtonStyle.PRIMARY)]
    # A single tier would just repeat "Best", so tiers only show when there's a real choice
    if len(info.tiers) > 1:
        video_buttons += [button(video_choice(info, tier), f"{tier}p") for tier in info.tiers]

    rows = [
        video_buttons[i : i + _BUTTONS_PER_ROW]
        for i in range(0, len(video_buttons), _BUTTONS_PER_ROW)
    ]

    if info.has_audio:
        rows.append([
            button(audio_choice(codec), codec.upper(), codec.upper())
            for codec in AUDIO_CODECS
        ])

    return InlineKeyboardMarkup(rows), choices


def cancel_keyboard(token: str) -> InlineKeyboardMarkup:
    """The Cancel button shown under a download's progress."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            button_label("CROSS", "Cancel"),
            callback_data=json.dumps({"c": token}, separators=(",", ":")),
            icon_custom_emoji_id=icon_id("CROSS"),
            style=KeyboardButtonStyle.DANGER,
        )
    ]])
