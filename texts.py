"""Every message the bot sends, as Telegram HTML. Emojis come from emojis.py."""

import math
from html import escape
from typing import Optional

import downloader
from config import BOT_NAME, USER_DOWNLOADS_PER_HOUR
from downloader import Progress, VideoInfo
from emojis import tg_emoji as e

# Keeps status messages readable when titles are long (e.g. TikTok descriptions)
TITLE_LIMIT = 200

# Captions allow 1024 characters; the title leaves room for the emoji
CAPTION_TITLE_LIMIT = 900

# Width of the progress bar, in blocks
_BAR_WIDTH = 12

GENERIC_ERROR = "Something went wrong. Please try again."
DELIVERY_FAILED = "I couldn't send the file here. Please try again."


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _title(title: str, limit: int = TITLE_LIMIT) -> str:
    return escape(_shorten(title, limit))


def _duration(seconds: float) -> str:
    hours, rest = divmod(max(0, round(seconds)), 3600)
    mins, secs = divmod(rest, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def _minutes(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def human_size(num_bytes: Optional[float]) -> str:
    """Short file size, e.g. 820 KB, 45 MB, 3.2 MB, 1.4 GB."""
    if num_bytes is None:
        return "?"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit == "MB" and size < 10 else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# -- commands -----------------------------------------------------------------

def start() -> str:
    return (
        f"{e('WELCOME')} <b>Welcome to {escape(BOT_NAME)}!</b>\n\n"
        f"{e('LINK')} Send me a video link and I'll send it back as a video, "
        "<b>MP3</b> or <b>FLAC</b>.\n\n"
        f"Just paste a link to get started {e('POINT_DOWN')}"
    )


def commands() -> list[tuple[str, str]]:
    """Telegram's command menu (the "/" button): (command, description), plain text."""
    return [
        ("start", "Welcome message"),
        ("help", "How to use the bot"),
    ]


def help_text() -> str:
    limit_line = (
        f"• Up to {USER_DOWNLOADS_PER_HOUR} new downloads per hour\n"
        if USER_DOWNLOADS_PER_HOUR > 0 else ""
    )
    return (
        f"{e('QUESTION')} <b>How to use</b>\n\n"
        "1. Paste or send a video link\n"
        "2. Choose a video quality, or MP3 / FLAC for audio only — each button shows the file size\n"
        "3. Watch the progress, or tap Cancel — I'll send the file right here\n\n"
        f"{e('INFO')} <b>Good to know</b>\n"
        f"• Files up to {round(downloader.upload_limit / 1024**3)} GB\n"
        "• Videos someone downloaded before arrive instantly\n"
        f"{limit_line}"
        "• Private videos and live streams can't be downloaded\n"
        "• Age-restricted videos need cookies set up by the bot owner\n\n"
        f"{e('KEYBOARD')} <b>Commands</b>\n"
        "/start — Welcome message\n"
        "/help — This help"
    )


def no_link() -> str:
    return f"{e('LINK')} Send me a link to a video and I'll download it for you."


# -- progress -----------------------------------------------------------------

def fetching() -> str:
    return f"{e('SEARCH')} Fetching video info…"


_QUEUE_WAIT = {
    "youtube": "my turn with YouTube",
    "info": "a free slot",
    "download": "a free download slot",
}


def queued(queue: str, position: int) -> str:
    return f"{e('HOURGLASS')} Waiting for {_QUEUE_WAIT.get(queue, 'my turn')} (position {position})…"


def choose(info: VideoInfo) -> str:
    duration = f"\n{e('TIMER')} {_duration(info.duration)}" if info.duration else ""
    return (
        f"{e('VIDEO')} <b>{_title(info.title)}</b>{duration}\n\n"
        f"Choose a video quality, or MP3 / FLAC for audio {e('POINT_DOWN')}"
    )


def downloading(title: str, label: str) -> str:
    return (
        f"{e('DOWNLOAD')} Downloading <b>{_title(title)}</b> as <b>{escape(label)}</b>…\n\n"
        "This may take a moment."
    )


def uploading(title: str) -> str:
    return f"{e('UPLOAD')} Uploading <b>{_title(title)}</b>…"


def progress(title: str, label: str, report: Progress) -> str:
    """Status message with a progress bar, amount, speed and time left."""
    if report.stage == "upload":
        head = f"{e('UPLOAD')} Uploading <b>{_title(title)}</b>"
    elif report.stage == "process":
        head = f"{e('GEAR')} {escape(report.detail or 'Processing')} — <b>{_title(title)}</b>"
    else:
        part = f" (part {report.part} of {report.parts})" if report.parts > 1 else ""
        head = f"{e('DOWNLOAD')} Downloading <b>{_title(title)}</b> as <b>{escape(label)}</b>{part}"

    lines = [head]
    if report.total:
        fraction = min(max(report.done / report.total, 0.0), 1.0)
        filled = round(fraction * _BAR_WIDTH)
        amount = (
            f"{_duration(report.done)} of {_duration(report.total)}"
            if report.stage == "process"
            else f"{human_size(report.done)} of {human_size(report.total)}"
        )
        lines += ["", f"<code>{'▰' * filled}{'▱' * (_BAR_WIDTH - filled)}</code> {fraction:.0%}", amount]
    elif report.stage != "process" and report.done:
        lines += ["", f"{human_size(report.done)} so far"]

    extras = []
    if report.speed and report.stage != "process":
        extras.append(f"{e('ZAP')} {human_size(report.speed)}/s")
    if report.eta is not None:
        extras.append(f"{e('TIMER')} {_duration(report.eta)} left")
    if extras:
        lines.append("   ".join(extras))
    return "\n".join(lines)


def done(title: str) -> str:
    return f"{e('CHECK')} Done! Enjoy <b>{_title(title)}</b> {e('ENJOY')}"


def done_instantly(title: str) -> str:
    return f"{e('ZAP')} Sent instantly — <b>{_title(title)}</b> was downloaded before."


def caption(title: str, is_audio: bool) -> str:
    return f"{e('MP3') if is_audio else e('VIDEO')} {_title(title, CAPTION_TITLE_LIMIT)}"


def cancelling() -> str:
    """Callback toast (plain text) while a download stops."""
    return "Cancelling…"


def cancelled(title: str) -> str:
    return f"{e('CROSS')} Cancelled <b>{_title(title)}</b>. Pick another option or send a new link."


# -- problems -----------------------------------------------------------------

def error(message: str) -> str:
    return f"{e('CROSS')} {escape(message)}"


def menu_expired() -> str:
    return f"{e('WARNING')} This menu has expired. Please send the link again."


def restarting() -> str:
    return f"{e('WARNING')} The bot is restarting. Please send the link again in a minute."


def lookup_limit(wait_seconds: int) -> str:
    return (
        f"{e('HOURGLASS')} You've sent a lot of links this hour. "
        f"Please try again in {_minutes(wait_seconds)}."
    )


# Callback alerts are plain text (no HTML or custom emoji), max 200 characters

def busy_alert() -> str:
    return "⏳ You already have a download in progress. Please wait for it to finish."


def download_limit_alert(wait_seconds: Optional[int]) -> str:
    return (
        f"⏳ You've reached {USER_DOWNLOADS_PER_HOUR} downloads this hour. "
        f"Try again in {_minutes(wait_seconds or 60)}."
    )


def nothing_to_cancel() -> str:
    return "This download has already finished."


def not_your_download() -> str:
    return "Only the person who started this download can cancel it."
