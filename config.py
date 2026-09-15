import os
import sys
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

BASE_DIR: Path = Path(__file__).resolve().parent

# Exit status for configuration errors (bad or missing settings, not logged in).
# video-bot.service doesn't restart on it: fix the problem, then start the bot again.
CONFIG_ERROR_EXIT = 78


def config_error(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(CONFIG_ERROR_EXIT)


def _int_env(name: str, default: int, minimum: int | None = None) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        config_error(f"{name} in .env must be a whole number, got {value!r}. See README, 'Configuration'.")
    if minimum is not None and number < minimum:
        config_error(f"{name} in .env must be at least {minimum}, got {number}. See README, 'Configuration'.")
    return number


# Required to run the bot; bot.py stops with a clear message if any is missing.
# (setup_session.py only needs API_ID and API_HASH, and can fill them in.)
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()

# Name shown in the welcome message
BOT_NAME: str = os.environ.get("BOT_NAME", "").strip() or "Video Downloader"

# Pyrogram user-account credentials (from https://my.telegram.org)
API_ID: int = _int_env("API_ID", 0)
API_HASH: str = os.environ.get("API_HASH", "").strip()

# Private relay channel ID (negative integer like -1001234567890)
# Pyrogram posts files here; bot copies them to the end user
# Both the bot AND the user account must be admins of this channel
RELAY_CHANNEL_ID: int = _int_env("RELAY_CHANNEL_ID", 0)

# Optional Netscape-format cookies file from a logged-in browser
# Unlocks age-restricted videos; leave unset to download without cookies
COOKIES_FILE: str | None = os.environ.get("COOKIES_FILE") or None

# Optional URL of a bgutil PO token provider, e.g. http://127.0.0.1:4416
# PO tokens make YouTube requests look like a real player and reduce blocks
POT_PROVIDER_URL: str | None = os.environ.get("POT_PROVIDER_URL") or None

# Send plain emoji instead of premium custom emoji (see emojis.py)
DISABLE_CUSTOM_EMOJI: bool = os.environ.get("DISABLE_CUSTOM_EMOJI", "").strip().lower() in ("1", "true", "yes")

# Telegram user IDs exempt from the per-user limits below (comma-separated)
try:
    ADMIN_IDS: frozenset[int] = frozenset(
        int(part) for part in os.environ.get("ADMIN_IDS", "").split(",") if part.strip()
    )
except ValueError:
    config_error("ADMIN_IDS in .env must be Telegram user IDs separated by commas. See README, 'Configuration'.")

# Optional cap on download speed, e.g. 5M for 5 MB/s; empty = unlimited
DOWNLOAD_RATE_LIMIT: str | None = os.environ.get("DOWNLOAD_RATE_LIMIT", "").strip() or None

# Optional proxy for sites that block the server's country, e.g.
# socks5h://127.0.0.1:1080 for an SSH tunnel to a server elsewhere (see README,
# "Sites blocked in the server's country"). Only PROXY_SITES use it.
PROXY_URL: str | None = os.environ.get("PROXY_URL", "").strip() or None

# yt-dlp site names, comma-separated, matched by prefix: "pornhub" also covers PornHubUser
PROXY_SITES: tuple[str, ...] = tuple(
    part.strip().lower() for part in os.environ.get("PROXY_SITES", "").split(",") if part.strip()
)

if PROXY_URL:
    try:
        _proxy = urlparse(PROXY_URL)
        _proxy_valid = bool(
            _proxy.scheme in ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
            and _proxy.hostname and _proxy.port
        )
    except ValueError:
        _proxy_valid = False
    if not _proxy_valid:
        config_error("PROXY_URL in .env must look like socks5h://127.0.0.1:1080 (with a port). See README, 'Configuration'.")
    if not PROXY_SITES:
        config_error("PROXY_URL is set but PROXY_SITES is empty: list the sites that should use it. See README, 'Configuration'.")

TEMP_DIR: Path = Path("/tmp/video-downloader")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_TIMEOUT: int = 1800

# Fetching video info is quick; give up long before a download would
INFO_TIMEOUT: int = 120

# Files in TEMP_DIR older than this are deleted before each new download.
# Catches leftovers from timed-out downloads that kept running in the background.
STALE_FILE_AGE: int = DOWNLOAD_TIMEOUT * 2

# Largest file the uploader account can send: 2000 MiB, or 4000 MiB if that
# account has Telegram Premium (detected at startup)
MAX_UPLOAD_BYTES: int = 2000 * 1024**2
PREMIUM_MAX_UPLOAD_BYTES: int = 4000 * 1024**2

# Downloads aren't started when less disk space than this would be left
MIN_FREE_DISK_BYTES: int = 1024**3

# Bitrate (kbps) for the MP3 option — FLAC is lossless and ignores this
MP3_BITRATE: int = 320

# Number of fragment download threads (DASH/HLS streams)
CONCURRENT_FRAGMENTS: int = 8

# Use aria2c (sudo apt install -y aria2) for multi-connection downloads.
# Off by default: aria2c downloads show no progress and don't stop quickly on
# Cancel. It's also skipped while COOKIES_FILE is set (it rewrites the file).
USE_ARIA2C: bool = False

# Seconds between progress updates on a status message (Telegram rate-limits edits)
PROGRESS_INTERVAL: float = 3

# ---------------------------------------------------------------------------
# Server capacity (can also be set in .env)
# ---------------------------------------------------------------------------

# Downloads (and conversions) running at once across all users; more wait in line
MAX_CONCURRENT_DOWNLOADS: int = _int_env("MAX_CONCURRENT_DOWNLOADS", 3, minimum=1)

# Threads for quick info lookups, kept apart from downloads
INFO_WORKERS: int = _int_env("INFO_WORKERS", 4, minimum=1)

# Uploads to the relay channel running at once
MAX_CONCURRENT_UPLOADS: int = _int_env("MAX_CONCURRENT_UPLOADS", 3, minimum=1)

# ---------------------------------------------------------------------------
# Fewer requests to YouTube
# ---------------------------------------------------------------------------

# Minimum pause (seconds) between YouTube info requests. Requests queue up
# instead of arriving in bursts, which is what trips YouTube's bot checks.
YOUTUBE_REQUEST_GAP: float = 5

# Video info fetched when a link is sent is reused for the download when the
# user picks a format within this many seconds — no second request needed
SAVED_INFO_TTL: int = 30 * 60
MAX_SAVED_INFOS: int = 200

# Delivered files stay in the relay channel and are re-sent instantly when
# anyone asks for the same video and format again. The oldest are deleted
# past this count; 0 disables the cache (relay copies deleted after delivery).
MAX_CACHED_FILES: int = 2000

# Cached files older than this are downloaded again when requested, since
# sites (YouTube especially) add higher qualities some time after upload
FILE_CACHE_TTL: int = 3 * 24 * 3600

# Video details (title, qualities) are remembered this long, so a link sent
# again shows its buttons without asking the site again
VIDEO_CACHE_TTL: int = 24 * 3600

CACHE_PATH: Path = BASE_DIR / "cache.json"
SESSION_PATH: Path = BASE_DIR / "uploader.session"

# ---------------------------------------------------------------------------
# Per-user limits (ADMIN_IDS are exempt; 0 = unlimited; can also be set in .env)
# ---------------------------------------------------------------------------

# Downloads one user can run at the same time
USER_MAX_ACTIVE_DOWNLOADS: int = _int_env("USER_MAX_ACTIVE_DOWNLOADS", 1, minimum=0)

# New downloads per user per hour (re-sending a cached file doesn't count)
USER_DOWNLOADS_PER_HOUR: int = _int_env("USER_DOWNLOADS_PER_HOUR", 20, minimum=0)

# Links per user per hour that need fetching (cached links don't count)
USER_LOOKUPS_PER_HOUR: int = _int_env("USER_LOOKUPS_PER_HOUR", 60, minimum=0)
