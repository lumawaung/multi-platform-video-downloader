import asyncio
import concurrent.futures
import contextlib
import copy
import hashlib
import importlib.metadata
import ipaddress
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.extractor import gen_extractor_classes

from config import (
    CONCURRENT_FRAGMENTS,
    COOKIES_FILE,
    DOWNLOAD_RATE_LIMIT,
    DOWNLOAD_TIMEOUT,
    INFO_TIMEOUT,
    INFO_WORKERS,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_SAVED_INFOS,
    MAX_UPLOAD_BYTES,
    MIN_FREE_DISK_BYTES,
    MP3_BITRATE,
    POT_PROVIDER_URL,
    PROXY_SITES,
    PROXY_URL,
    SAVED_INFO_TTL,
    STALE_FILE_AGE,
    TEMP_DIR,
    USE_ARIA2C,
    YOUTUBE_REQUEST_GAP,
    config_error,
)

logger = logging.getLogger(__name__)

# Standard quality tiers, smallest first. A video belongs to the smallest tier
# whose 16:9 frame (1920x1080 for 1080p) it fits in, in either orientation —
# so Shorts, TikToks and ultrawide films all get sensible tiers.
QUALITY_TIERS = (360, 480, 720, 1080, 1440, 2160)

# Slack on a tier's frame, e.g. 1920x1088 still counts as 1080p
TIER_TOLERANCE = 0.08

# Audio formats offered below the video qualities, in button order
AUDIO_CODECS = ("mp3", "flac")

# Rough output bitrates (kbps), used to refuse audio files that would be too big
_AUDIO_KBPS = {"mp3": MP3_BITRATE, "flac": 1100}

# At equal resolution, prefer H.264 + AAC so most videos skip re-encoding
_VIDEO_FORMAT_SORT = ["res", "fps", "vcodec:h264", "acodec:aac"]

# Best audio, skipping TikTok's separate music track (format_id "audio") so the
# sound comes from the video itself; YouTube audio formats have numeric IDs
_BEST_AUDIO = "ba[format_id!=audio]"

# Info fields a download never needs; dropped to keep saved info small
_BULKY_INFO_KEYS = ("automatic_captions", "subtitles", "heatmap", "thumbnails")

# How long a cancelled download may take to stop before its files are removed anyway
_CANCEL_GRACE = 60

# Status text while yt-dlp post-processes a download, by post-processor key
# (yt-dlp drops the "FFmpeg" prefix and "PP" suffix from class names)
_POSTPROCESSOR_DETAILS = {
    "Merger": "Merging video and audio",
    "ExtractAudio": "Converting to {codec}",
    "Metadata": "Adding title and artist tags",
}

_UNSUPPORTED_LINK = "This link isn't supported. Send a link to a single video."

# ffmpeg's fast H.264 encode is bigger than the VP9/AV1/HEVC it replaces; size
# estimates for videos that get re-encoded are scaled up by this much
_REENCODE_GROWTH = 1.6

# DOWNLOAD_RATE_LIMIT from .env in bytes per second (None = unlimited)
_RATE_LIMIT: Optional[int] = None
if DOWNLOAD_RATE_LIMIT:
    _RATE_LIMIT = yt_dlp.utils.parse_bytes(DOWNLOAD_RATE_LIMIT)
    if not _RATE_LIMIT:
        config_error(f"DOWNLOAD_RATE_LIMIT in .env must look like 5M or 800K, got {DOWNLOAD_RATE_LIMIT!r}.")
_FORMAT_GONE = "That quality is no longer available. Please send the link again."
_COULD_NOT_GET = "Could not get this video. Please check the link and try again."

# Largest file the uploader account may send; raised at startup for Premium uploaders
upload_limit = MAX_UPLOAD_BYTES

# Awaited with (queue name, position) while a job waits in line, then with
# position 0 when its turn comes. Queues: "youtube" (YouTube info requests)
# and "download" (download slots).
OnWait = Callable[[str, int], Awaitable[None]]


@dataclass
class VideoInfo:
    key: str  # "<extractor>:<video id>", e.g. "Youtube:nzIUwWC8nfs"
    title: str
    uploader: Optional[str]
    duration: Optional[int]
    portrait: bool = False
    tiers: list[int] = field(default_factory=list)
    has_audio: bool = True
    # Button label -> expected file size in bytes (None when unknown)
    sizes: dict[str, Optional[int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VideoInfo":
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


@dataclass
class Choice:
    """One keyboard button: a video quality or an audio format."""
    label: str
    format: str
    audio_codec: Optional[str] = None  # "mp3" / "flac"; None for video

    @property
    def is_audio(self) -> bool:
        return self.audio_codec is not None


@dataclass
class DownloadResult:
    job_id: str
    key: str  # video key of the info actually downloaded
    path: Path
    title: str
    uploader: Optional[str]
    duration: int
    is_audio: bool
    width: int = 0
    height: int = 0
    thumb: Optional[Path] = None


@dataclass
class Progress:
    """A progress report for the status message."""
    stage: str  # "download", "process" (merging/converting) or "upload"
    done: float = 0  # bytes, or seconds of video for "process"
    total: Optional[float] = None
    speed: Optional[float] = None  # bytes per second
    eta: Optional[float] = None  # seconds left
    part: int = 1  # e.g. video and audio are downloaded as parts 1 and 2
    parts: int = 1
    detail: str = ""  # what "process" is doing, e.g. "Converting to MP3"


# Receives progress reports, from worker threads too: it must be thread-safe
OnProgress = Callable[[Progress], None]


class DownloadError(Exception):
    def __init__(self, message: str = "", pending: Optional[asyncio.Future] = None):
        super().__init__(message)
        # Set when the work behind a failed download couldn't be stopped yet;
        # it completes once that work has really finished
        self.pending = pending


class _SavedInfoFailed(Exception):
    """A download from saved info failed; fetch fresh info and try once more."""


class _Cancelled(yt_dlp.utils.DownloadCancelled):
    msg = "Download cancelled"


class _TooBig(yt_dlp.utils.DownloadCancelled):
    msg = "Download stopped: over the size limit"


def set_upload_limit(limit_bytes: int) -> None:
    global upload_limit
    upload_limit = limit_bytes


def too_big_message(size_bytes: Optional[int] = None) -> str:
    limit = f"{round(upload_limit / 1024**3)} GB"
    if size_bytes:
        return f"This file is about {size_bytes / 1024**3:.1f} GB, over Telegram's {limit} limit. Try a lower quality."
    return f"This file is over Telegram's {limit} limit. Try a lower quality."


# ---------------------------------------------------------------------------
# Download choices
# ---------------------------------------------------------------------------

def _tier_frame(tier: int) -> tuple[int, int]:
    """(long side, short side) limits of a tier's 16:9 frame, with tolerance."""
    return int(tier * 16 / 9 * (1 + TIER_TOLERANCE)), int(tier * (1 + TIER_TOLERANCE))


def tier_for(width: int, height: int) -> Optional[int]:
    """Quality tier of a width x height video, or None if below 360p or above 4K."""
    long_side, short_side = max(width, height), min(width, height)
    if long_side < QUALITY_TIERS[0] * 16 / 9 * 0.8 and short_side < QUALITY_TIERS[0] * 0.8:
        return None
    for tier in QUALITY_TIERS:
        max_long, max_short = _tier_frame(tier)
        if long_side <= max_long and short_side <= max_short:
            return tier
    return None


def video_choice(info: VideoInfo, tier: Optional[int] = None) -> Choice:
    """Best available video, or the best video that fits a quality tier."""
    if tier is None:
        return Choice("Best", f"bv*+{_BEST_AUDIO}/b/bv*+ba")
    long_dim, short_dim = ("height", "width") if info.portrait else ("width", "height")
    max_long, max_short = _tier_frame(tier)
    fits = f"[{long_dim}<={max_long}][{short_dim}<={max_short}]"
    return Choice(f"{tier}p", f"bv*{fits}+{_BEST_AUDIO}/b{fits}/bv*{fits}+ba")


def audio_choice(codec: str) -> Choice:
    return Choice(codec.upper(), f"{_BEST_AUDIO}/b", audio_codec=codec)


# ---------------------------------------------------------------------------
# Temp file cleanup
# ---------------------------------------------------------------------------

def _delete_files(pattern: str = "*", older_than: Optional[float] = None) -> None:
    """Best-effort deletion of files in TEMP_DIR matching a glob pattern."""
    cutoff = time.time() - older_than if older_than is not None else None
    try:
        for f in TEMP_DIR.glob(pattern):
            try:
                if f.is_file() and (cutoff is None or f.stat().st_mtime < cutoff):
                    f.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def cleanup_temp_dir() -> None:
    """Delete all leftover files in TEMP_DIR (called on bot startup)."""
    _delete_files()


def cleanup_job_files(job_id: str) -> None:
    """Delete every file of one job: media, partial downloads, thumbnail."""
    _delete_files(f"{job_id}*")


def _cleanup_stale_files() -> None:
    """Delete files left behind by downloads that couldn't be stopped in time."""
    _delete_files(older_than=STALE_FILE_AGE)


def _kill_job_processes(job_id: str) -> None:
    """Stop helper processes (ffmpeg, aria2c) still working on a cancelled job's files."""
    try:
        subprocess.run(["pkill", "-f", job_id], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# Site detection (offline)
# ---------------------------------------------------------------------------

_EXTRACTORS = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_YOUTUBE_ID_RE = re.compile(r"[\w-]{11}")
_YOUTUBE_PATH_PREFIXES = ("shorts", "live", "embed", "v", "e")

# Sites whose query string never picks a different video (only tracking parameters)
_QUERY_IGNORED = frozenset({"TikTok"})


def _youtube_id(parsed) -> Optional[str]:
    video_id = parse_qs(parsed.query).get("v", [""])[0]
    if not video_id:
        segments = [s for s in parsed.path.split("/") if s]
        if parsed.hostname.endswith("youtu.be") and segments:
            video_id = segments[0]
        elif len(segments) >= 2 and segments[0] in _YOUTUBE_PATH_PREFIXES:
            video_id = segments[1]
    return video_id if _YOUTUBE_ID_RE.fullmatch(video_id) else None


def guess_video_key(url: str) -> Optional[str]:
    """
    Video key worked out from the URL alone, with no network request — used to
    check the cache and to recognise YouTube links. None if it can't be told.
    Slow on first use (compiles extractor patterns): call it via guess_key().
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS):
        video_id = _youtube_id(parsed)
        if video_id:
            return f"Youtube:{video_id}"
    for ie in _EXTRACTORS:
        if ie.suitable(url):
            match = ie._match_valid_url(url)
            # Every captured part, not just the ID: some sites pick a different
            # video with another part (e.g. Twitter's /video/2)
            parts = [str(v) for v in (match.groupdict().values() if match else ()) if v]
            if not parts:
                return None
            key = f"{ie.ie_key()}:{':'.join(parts)}"
            if parsed.query and ie.ie_key() not in _QUERY_IGNORED:
                key += f"?{parsed.query}"  # e.g. BiliBili's ?p=2 selects a part
            return key
    return None


def is_youtube_key(key: Optional[str]) -> bool:
    return key is not None and key.startswith("Youtube")


def site_of(url: str) -> Optional[str]:
    """yt-dlp's name for the site of a link (e.g. "PornHub"), None if unknown."""
    return next((ie.ie_key() for ie in _EXTRACTORS if ie.suitable(url)), None)


def _needs_proxy(site: Optional[str]) -> bool:
    """True for sites listed in PROXY_SITES (yt-dlp site names, matched by prefix)."""
    return bool(PROXY_URL and site and site.lower().startswith(PROXY_SITES))


# ---------------------------------------------------------------------------
# yt-dlp setup
# ---------------------------------------------------------------------------

class _YtdlpLogger:
    """
    One per yt-dlp run. Keeps yt-dlp's chatter in debug logs, surfaces what an
    admin must act on, and notes when a download was stopped for being too big.
    """

    _last_cookie_alert = 0.0  # shared: at most one cookie alert every 10 minutes

    def __init__(self) -> None:
        self.too_big = False
        # Warnings of this run, e.g. the reason a video came back without formats
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        if "larger than max-filesize" in msg:
            self.too_big = True

    info = debug

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)
        if "cookies are no longer valid" in msg:
            now = time.monotonic()
            if now - _YtdlpLogger._last_cookie_alert > 600:
                _YtdlpLogger._last_cookie_alert = now
                logger.error(
                    "The YouTube cookies in COOKIES_FILE are no longer valid. "
                    "Export fresh ones (see README, 'Cookies')."
                )
        elif "Deprecated Feature" in msg:
            logger.warning("yt-dlp: %s", msg)
        else:
            logger.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        if "Deprecated Feature" in msg:
            logger.warning("yt-dlp: %s", msg)
        else:
            logger.debug("yt-dlp: %s", msg)


# yt-dlp reads the cookies file lazily and rewrites it (not atomically) when a
# YoutubeDL closes. Serialising both keeps parallel jobs from corrupting it.
_COOKIE_LOCK = threading.Lock()


@contextlib.contextmanager
def _ydl(opts: dict) -> Iterator[yt_dlp.YoutubeDL]:
    with _COOKIE_LOCK:
        ydl = yt_dlp.YoutubeDL(opts)
        ydl.cookiejar  # load the cookies file now, under the lock
    try:
        yield ydl
    finally:
        with _COOKIE_LOCK:
            ydl.close()


def _build_ydl_opts(extra: dict | None = None, proxied: bool = False) -> dict:
    opts = {
        "quiet": True,
        "logger": _YtdlpLogger(),
        "noplaylist": True,
        # Multi-video posts: only take the first video
        "playlist_items": "1",
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 10,
        "noprogress": True,
        # Download multiple DASH/HLS fragments simultaneously
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
    }

    # aria2c: multi-connection downloader (install: sudo apt install -y aria2)
    # Falls back to yt-dlp's built-in if aria2c is not found. Not used with a
    # cookies file: aria2c runs would rewrite it outside _COOKIE_LOCK.
    if USE_ARIA2C and not COOKIES_FILE:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": [
                "--max-connection-per-server=16",
                "--min-split-size=1M",
                "--split=16",
                "--max-concurrent-downloads=16",
                "--continue=true",
                "--quiet=true",
            ]
        }

    # Logged-in cookies unlock age-restricted and login-only videos
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE

    if _RATE_LIMIT:
        opts["ratelimit"] = _RATE_LIMIT

    # Where the bgutil plugin fetches YouTube PO tokens from
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}}

    # Sites that block the server's country go through PROXY_URL
    if proxied:
        opts["proxy"] = PROXY_URL

    if extra:
        opts.update(extra)
    return opts


_DEFAULT_POT_PROVIDER_URL = "http://127.0.0.1:4416"


def _ping_pot_provider(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/ping", timeout=3) as response:
        return str(json.load(response).get("version"))


async def check_pot_provider() -> None:
    """Log at startup whether the optional PO token provider is reachable and compatible."""
    url = POT_PROVIDER_URL or _DEFAULT_POT_PROVIDER_URL
    try:
        server_version = await asyncio.to_thread(_ping_pot_provider, url)
    except Exception as e:
        if POT_PROVIDER_URL:
            logger.warning(
                "PO token provider not reachable at %s (%s). YouTube requests will go without PO tokens.",
                url, e,
            )
        else:
            logger.info("No PO token provider at %s (optional, see README).", url)
        return

    try:
        plugin_version = importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        logger.warning(
            "PO token provider %s is running at %s, but the bgutil-ytdlp-pot-provider "
            "plugin isn't installed in this Python environment.", server_version, url,
        )
        return
    if plugin_version.split(".")[0] != server_version.split(".")[0]:
        logger.warning(
            "PO token provider server %s and plugin %s have different major versions; "
            "the plugin will reject its tokens. Install matching versions.",
            server_version, plugin_version,
        )
    else:
        logger.info("PO token provider %s ready at %s (plugin %s).", server_version, url, plugin_version)


_PROXY_DOWN = "This site is temporarily unavailable. Please try again later."


def _proxy_address() -> str:
    """host:port of PROXY_URL, without any password in it."""
    parsed = urlparse(PROXY_URL)
    return f"{parsed.hostname}:{parsed.port}"


def _proxy_reachable() -> bool:
    """True if something accepts connections at PROXY_URL's address."""
    parsed = urlparse(PROXY_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=3):
            return True
    except OSError:
        return False


async def _proxy_down_error() -> Optional[DownloadError]:
    """The error to show when a proxied request failed because the proxy is down."""
    if await asyncio.to_thread(_proxy_reachable):
        return None
    logger.error("The proxy at %s isn't reachable, so %s can't be downloaded.", _proxy_address(), ", ".join(PROXY_SITES))
    return DownloadError(_PROXY_DOWN)


async def check_proxy() -> None:
    """Log at startup whether the optional proxy for PROXY_SITES is reachable."""
    if not PROXY_URL:
        return
    if await asyncio.to_thread(_proxy_reachable):
        logger.info("Proxy at %s ready for: %s", _proxy_address(), ", ".join(PROXY_SITES))
    else:
        logger.warning(
            "Proxy at %s isn't reachable; %s will fail until it is (see README, "
            "'Sites blocked in the server's country').", _proxy_address(), ", ".join(PROXY_SITES),
        )


def _is_local_address(address: str) -> bool:
    """True if the address belongs to this machine (e.g. its own public IP)."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((address, 0))  # only works for the machine's own addresses
        return True
    except OSError:
        return False


def _check_public_url(url: str) -> None:
    """
    Refuse links to this server (including its own public IP, where panels and
    other services may listen) or private networks, so users can't make the bot
    fetch internal services (SSRF). Redirects yt-dlp follows aren't re-checked.
    """
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise DownloadError(_UNSUPPORTED_LINK)
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise DownloadError(_UNSUPPORTED_LINK)
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        raise DownloadError("Couldn't reach that website. Please check the link.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or _is_local_address(str(ip)):
            logger.warning("Refused a link to non-public or local address %s (%s)", host, ip)
            raise DownloadError(_UNSUPPORTED_LINK)


def _friendly_error(message: str) -> str:
    """Turn a raw yt-dlp error into a short message for the end user."""
    msg = message.lower()
    if "not a bot" in msg or "try again later" in msg or "http error 429" in msg:
        return "The site is temporarily blocking the server. Please try again later."
    # Whole word only: a substring check also matches "webpage", "message", ...
    if re.search(r"\bage\b", msg):
        return (
            "This video is age-restricted. The bot needs cookies from an "
            "age-verified account to download it."
        )
    if "private" in msg or "permission to view" in msg:
        return "This video is private."
    if "ip address is blocked" in msg or "your country" in msg or "your location" in msg:
        return "This video isn't available in the server's region."
    if "unsupported url" in msg:
        return _UNSUPPORTED_LINK
    if "requested format is not available" in msg:
        return _FORMAT_GONE
    if any(s in msg for s in ("unavailable", "removed", "not available", "does not exist")):
        return "This video is unavailable or has been removed."
    if any(s in msg for s in ("login", "log in", "sign in", "authentication")):
        return "This video requires login. The bot can't access it without cookies."
    return _COULD_NOT_GET


# ---------------------------------------------------------------------------
# Queues and worker threads
# ---------------------------------------------------------------------------

class _RequestGate:
    """
    Lets one info request run at a time, with a minimum gap after each, so
    requests to a site arrive spaced out instead of in bursts.
    """

    def __init__(self, name: str, gap: float):
        self._name = name
        self._gap = gap
        self._lock = asyncio.Lock()
        self._ready_at = 0.0
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def slot(self, on_wait: Optional[OnWait] = None) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        must_wait = on_wait is not None and (self._lock.locked() or loop.time() < self._ready_at)
        # Take a place in line before the (slow) status update, so callers
        # arriving together get distinct positions
        self._waiting += 1
        try:
            if must_wait:
                await on_wait(self._name, self._waiting)
            await self._lock.acquire()
        finally:
            self._waiting -= 1
        try:
            delay = self._ready_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if must_wait:
                await on_wait(self._name, 0)
            yield
        finally:
            self._ready_at = loop.time() + self._gap
            self._lock.release()


class _Slots:
    """At most `size` jobs run at once; the rest wait in line."""

    def __init__(self, name: str, size: int):
        self._name = name
        self._semaphore = asyncio.Semaphore(size)
        self._waiting = 0

    async def acquire(self, on_wait: Optional[OnWait] = None) -> None:
        must_wait = on_wait is not None and self._semaphore.locked()
        self._waiting += 1
        try:
            if must_wait:
                await on_wait(self._name, self._waiting)
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        if must_wait:
            try:
                await on_wait(self._name, 0)
            except BaseException:
                self._semaphore.release()
                raise

    def release(self) -> None:
        self._semaphore.release()


_YOUTUBE_GATE = _RequestGate("youtube", YOUTUBE_REQUEST_GAP)
_INFO_SLOTS = _Slots("info", INFO_WORKERS)
_DOWNLOAD_SLOTS = _Slots("download", MAX_CONCURRENT_DOWNLOADS)

# yt-dlp and ffmpeg get their own threads. asyncio's default pool also does
# DNS lookups for Telegram requests, which long downloads would starve.
_INFO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(INFO_WORKERS, thread_name_prefix="info")
_DOWNLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(MAX_CONCURRENT_DOWNLOADS, thread_name_prefix="download")


def shutdown() -> None:
    """Drop queued work and let worker threads end (called when the bot stops)."""
    _INFO_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    _DOWNLOAD_EXECUTOR.shutdown(wait=False, cancel_futures=True)


async def guess_key(url: str) -> Optional[str]:
    """
    guess_video_key() off the event loop. It's CPU-only and quick after
    warm_up(), so it doesn't wait in line behind network lookups.
    """
    return await asyncio.to_thread(guess_video_key, url)


async def warm_up() -> None:
    """Compile every site's URL pattern once at startup, so link checks are quick later."""
    await asyncio.to_thread(guess_video_key, "https://example.invalid/")


def _release_when_done(slots: "_Slots") -> Callable[[asyncio.Future], None]:
    """Done-callback that frees a slot once a worker thread has really finished."""
    def release(future: asyncio.Future) -> None:
        if not future.cancelled():
            future.exception()  # mark any error as seen
        slots.release()
    return release


# ---------------------------------------------------------------------------
# Video info
# ---------------------------------------------------------------------------

# Raw yt-dlp info from recent lookups, reused by downloads: key -> (time, info)
_saved_infos: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


def _remember(key: str, raw: dict) -> None:
    _saved_infos.pop(key, None)
    _saved_infos[key] = (time.monotonic(), raw)
    while len(_saved_infos) > MAX_SAVED_INFOS:
        _saved_infos.popitem(last=False)


def _recall(key: str) -> Optional[dict]:
    entry = _saved_infos.get(key)
    if entry is None:
        return None
    saved_at, raw = entry
    if time.monotonic() - saved_at > SAVED_INFO_TTL:
        del _saved_infos[key]
        return None
    return raw


def _has_media(info: dict) -> bool:
    return bool(info.get("url")) or any(
        f.get("vcodec") != "none" or f.get("acodec") != "none" for f in info.get("formats") or []
    )


def _extract_info_sync(url: str, proxied: bool = False) -> dict:
    _check_public_url(url)
    # extract_flat keeps playlist/profile links from fetching every entry; a few
    # items (and tolerating photo-only entries) let a carousel that starts with
    # a photo still find its video
    ytdlp_logger = _YtdlpLogger()
    opts = _build_ydl_opts({
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlist_items": "1:10",
        "ignore_no_formats_error": True,
        "logger": ytdlp_logger,
    }, proxied=proxied)
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries")
    if entries is not None:
        entries = [e for e in entries if e]
        if not entries or any(e.get("_type") in ("url", "url_transparent") for e in entries):
            raise DownloadError("Playlists and profiles aren't supported. Send a link to a single video.")
        info = next((e for e in entries if _has_media(e)), entries[0])

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise DownloadError("Live streams can't be downloaded. Try again once the broadcast has ended and been processed.")
    if not _has_media(info):
        # ignore_no_formats_error turns reasons like "Sign in to confirm your age"
        # into warnings: show the matching message when one explains it
        for warning in reversed(ytdlp_logger.warnings):
            message = _friendly_error(warning)
            if message not in (_COULD_NOT_GET, _FORMAT_GONE):
                raise DownloadError(message)
        raise DownloadError("There's no video or audio to download in this post.")

    for key in _BULKY_INFO_KEYS:
        info.pop(key, None)
    # Same cleanup yt-dlp applies before re-downloading from saved info — but
    # keep the "__needs_testing" marks, so doubtful formats still get tested
    needs_testing = {f.get("format_id") for f in info.get("formats") or [] if f.get("__needs_testing")}
    sanitized = yt_dlp.YoutubeDL.sanitize_info(info, remove_private_keys=True)
    for f in sanitized.get("formats") or []:
        if f.get("format_id") in needs_testing:
            f["__needs_testing"] = True
    return sanitized


async def _fetch_info(url: str, gated: bool, on_wait: Optional[OnWait]) -> dict:
    """Fetch video info in a worker thread; YouTube links go through the request queue."""
    loop = asyncio.get_running_loop()
    proxied = bool(PROXY_URL) and _needs_proxy(await asyncio.to_thread(site_of, url))
    slot = _YOUTUBE_GATE.slot(on_wait) if gated else contextlib.nullcontext()
    async with slot:
        # A thread is free before the clock starts, so INFO_TIMEOUT measures only the lookup
        await _INFO_SLOTS.acquire(on_wait)
        future = loop.run_in_executor(_INFO_EXECUTOR, _extract_info_sync, url, proxied)
        # The thread can't be stopped early, so its slot frees up when it really ends
        future.add_done_callback(_release_when_done(_INFO_SLOTS))
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=INFO_TIMEOUT)
        except asyncio.TimeoutError:
            raise DownloadError("Timed out while fetching video info.")
        except yt_dlp.utils.DownloadError as e:
            logger.warning("Could not fetch info for %s: %s", url, e)
            raise (proxied and await _proxy_down_error()) or DownloadError(_friendly_error(str(e)))


def _video_key(raw: dict) -> str:
    extractor = raw.get("extractor_key") or "Unknown"
    video_id = raw.get("id")
    if extractor == "Generic" or not video_id:
        # Generic IDs are just the file name ("video", "index"...) and repeat across sites
        source = raw.get("webpage_url") or str(video_id)
        video_id = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{extractor}:{video_id}"


def _to_video_info(raw: dict, with_sizes: bool = False) -> VideoInfo:
    formats = raw.get("formats") or [raw]
    video_formats = [
        f for f in formats
        if f.get("vcodec") != "none"
        and isinstance(f.get("width"), int) and f["width"] > 0
        and isinstance(f.get("height"), int) and f["height"] > 0
    ]

    portrait = False
    tiers: set[int] = set()
    if video_formats:
        largest = max(video_formats, key=lambda f: f["width"] * f["height"])
        portrait = largest["height"] > largest["width"]
        for f in video_formats:
            tier = tier_for(f["width"], f["height"])
            if tier:
                tiers.add(tier)

    info = VideoInfo(
        key=_video_key(raw),
        title=raw.get("title") or "Unknown title",
        uploader=raw.get("uploader") or raw.get("channel") or raw.get("creator"),
        duration=round(raw["duration"]) if raw.get("duration") else None,
        portrait=portrait,
        tiers=sorted(tiers, reverse=True),
        # Only hide the audio buttons when every format explicitly has no audio
        has_audio=any(f.get("acodec") != "none" for f in formats),
    )
    if with_sizes:
        info.sizes = _estimate_sizes(raw, info)
    return info


async def get_info(
    url: str,
    key_hint: Optional[str] = None,
    on_wait: Optional[OnWait] = None,
) -> VideoInfo:
    """
    Fetch video details for the buttons. The raw info is kept so a download
    started soon after doesn't need a second request.
    """
    if key_hint is None:
        key_hint = await guess_key(url)
    raw = await _fetch_info(url, is_youtube_key(key_hint), on_wait)
    # Size estimates run the format selector for every button: CPU work, off the loop
    info = await asyncio.to_thread(_to_video_info, raw, True)
    _remember(info.key, raw)
    return info


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _probe(path: Path) -> dict:
    """ffprobe's streams and format info for a file ({} if probing fails)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, check=True, timeout=120,
        )
        return json.loads(result.stdout)
    except Exception:
        return {}


def _first_stream(probe: dict, codec_type: str) -> dict:
    """First stream of a type, ignoring embedded cover art."""
    return next(
        (
            s for s in probe.get("streams", [])
            if s.get("codec_type") == codec_type
            and not s.get("disposition", {}).get("attached_pic")
        ),
        {},
    )


def _display_size(stream: dict) -> tuple[int, int]:
    """Width and height as the video is shown, after its rotation metadata."""
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    rotation = next(
        (sd["rotation"] for sd in stream.get("side_data_list") or [] if "rotation" in sd),
        (stream.get("tags") or {}).get("rotate", 0),
    )
    try:
        if abs(int(float(rotation))) % 180 == 90:
            width, height = height, width
    except (TypeError, ValueError):
        pass
    return width, height


def _duration(info: dict, probe: dict) -> int:
    seconds = info.get("duration") or (probe.get("format") or {}).get("duration") or 0
    try:
        return max(0, round(float(seconds)))
    except (TypeError, ValueError):
        return 0


def _run_ffmpeg(
    cmd: list[str],
    cancel: threading.Event,
    on_time: Optional[Callable[[float, Optional[float]], None]] = None,
) -> None:
    """
    Run ffmpeg, killing it if the job is cancelled. on_time, if given, gets
    (seconds of output written, speed as a multiple of real time) as it works.
    """
    if on_time is not None:
        cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats"] + cmd[-1:]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if on_time is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_lines: list[str] = []
    readers = [threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)]

    if on_time is not None:
        def read_progress() -> None:
            values: dict[str, str] = {}
            for line in proc.stdout:
                name, _, value = line.strip().partition("=")
                values[name] = value
                if name != "progress":
                    continue
                try:
                    seconds = int(values.get("out_time_us") or values.get("out_time_ms") or 0) / 1e6
                except ValueError:
                    continue
                try:
                    speed = float((values.get("speed") or "").rstrip("x")) or None
                except ValueError:
                    speed = None
                on_time(seconds, speed)
        readers.append(threading.Thread(target=read_progress, daemon=True))

    for reader in readers:
        reader.start()
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.kill()
                proc.wait()
                raise _Cancelled()
    for reader in readers:
        reader.join(timeout=5)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stderr="".join(stderr_lines))


def _ensure_telegram_compatible(
    raw_path: Path,
    job_id: str,
    probe: dict,
    cancel: threading.Event,
    on_progress: Optional[OnProgress] = None,
    duration: int = 0,
) -> Path:
    """
    Remux to MP4 with faststart so Telegram can stream the video in-app.
    H.264 video and AAC/MP3 audio are copied as-is (fast, no quality loss);
    anything else is re-encoded. Returns the path to the final file.
    """
    video = _first_stream(probe, "video")
    audio = _first_stream(probe, "audio")
    copy_video = video.get("codec_name") == "h264" and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
    copy_audio = audio.get("codec_name") in ("aac", "mp3")
    logger.info(
        "Preparing video: %s video, %s audio",
        "copying" if copy_video else f"re-encoding {video.get('codec_name')}",
        "copying" if copy_audio else f"re-encoding {audio.get('codec_name')}",
    )

    out_path = TEMP_DIR / f"{job_id}_tg.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_path), "-map", "0:v:0", "-map", "0:a:0?"]
    if copy_video:
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            # libx264 with yuv420p needs an even width and height
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            # yuv420p: 10-bit/HDR sources otherwise won't play on most phones
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
        ]
    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(out_path)]

    detail = "Preparing the video for Telegram" if copy_video else "Converting the video for Telegram"
    on_time = None
    if on_progress is not None:
        on_progress(Progress("process", detail=detail))
        if not copy_video and duration:
            def on_time(seconds: float, speed: Optional[float]) -> None:
                eta = max(0.0, duration - seconds) / speed if speed else None
                on_progress(Progress("process", done=seconds, total=duration, eta=eta, detail=detail))
    try:
        _run_ffmpeg(cmd, cancel, on_time)
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg failed for %s: %s", raw_path.name, (e.stderr or "").strip())
        raise DownloadError("Could not convert the video for Telegram.")

    raw_path.unlink(missing_ok=True)
    return out_path


def _make_thumbnail(video_path: Path, job_id: str, duration: int) -> Optional[Path]:
    """Grab one frame as a Telegram thumbnail (JPEG, max 320px). None on failure."""
    thumb_path = TEMP_DIR / f"{job_id}_thumb.jpg"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(min(1, duration // 2)),
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-q:v", "4",
        str(thumb_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return thumb_path if thumb_path.is_file() else None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _estimated_size(choice: Choice, info: dict) -> Optional[int]:
    """Expected size of the finished file, when it can be told before downloading."""
    if choice.is_audio:
        duration = info.get("duration")
        return int(duration * _AUDIO_KBPS[choice.audio_codec] * 1000 / 8) if duration else None
    formats = info.get("requested_formats") or [info]
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
    if not (sizes and all(sizes)):
        return None
    total = sum(sizes)
    video = next((f for f in formats if f.get("vcodec") not in (None, "none")), None)
    if video and not str(video["vcodec"]).startswith(("avc", "h264")):
        total *= _REENCODE_GROWTH  # it will be re-encoded to H.264 for Telegram
    return int(total)


def _estimate_sizes(raw: dict, info: VideoInfo) -> dict[str, Optional[int]]:
    """Expected file size for every button, from format metadata only (no network)."""
    sizes: dict[str, Optional[int]] = {}
    base = copy.deepcopy(raw)
    for f in base.get("formats") or []:
        f.pop("__needs_testing", None)  # testing a format would download part of it
    choices = [video_choice(info)] + [video_choice(info, tier) for tier in info.tiers]
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "logger": _YtdlpLogger(),
            "simulate": True,
            "format_sort": _VIDEO_FORMAT_SORT,
        }) as ydl:
            for choice in choices:
                try:
                    ydl.format_selector = ydl.build_format_selector(choice.format)
                    planned = ydl.process_ie_result(copy.deepcopy(base), download=False)
                    sizes[choice.label] = _estimated_size(choice, planned)
                except Exception:
                    sizes[choice.label] = None
    except Exception:
        logger.debug("Could not estimate sizes for %s", info.key, exc_info=True)
    for codec in AUDIO_CODECS:
        sizes[codec.upper()] = _estimated_size(audio_choice(codec), raw)
    return sizes


def _check_disk_space(estimate: Optional[int]) -> None:
    # Merging and converting can briefly need about three times the final size;
    # with no estimate, reserve room for the largest download allowed
    needed = MIN_FREE_DISK_BYTES + (3 * estimate if estimate else int(upload_limit * 1.5))
    free = shutil.disk_usage(TEMP_DIR).free
    if free < needed:
        logger.error("Low disk space in %s: %d MiB free, %d MiB needed", TEMP_DIR, free // 2**20, needed // 2**20)
        raise DownloadError("The server is low on disk space right now. Please try again later.")


def _check_final_size(path: Path) -> None:
    size = path.stat().st_size
    if size > upload_limit:
        raise DownloadError(too_big_message(size))


def _download_sync(
    choice: Choice,
    job_id: str,
    raw: dict,
    cancel: threading.Event,
    on_progress: Optional[OnProgress] = None,
) -> DownloadResult:
    key = _video_key(raw)
    ytdlp_logger = _YtdlpLogger()
    size_cap = int(upload_limit * 1.5)
    state: dict = {"file": None, "part": 0, "parts": 1, "bytes": {}}

    def on_download(status: dict) -> None:
        if cancel.is_set():
            raise _Cancelled()
        if status.get("status") != "downloading":
            return
        # A hard stop for downloads whose size wasn't known up front, whatever
        # the download method (max_filesize only works when the size is announced)
        state["bytes"][status.get("filename")] = status.get("downloaded_bytes") or 0
        if sum(state["bytes"].values()) > size_cap:
            raise _TooBig()
        if on_progress is None:
            return
        if status.get("filename") != state["file"]:
            state["file"] = status.get("filename")
            state["part"] += 1
        on_progress(Progress(
            "download",
            done=status.get("downloaded_bytes") or 0,
            total=status.get("total_bytes") or status.get("total_bytes_estimate"),
            speed=status.get("speed"),
            eta=status.get("eta"),
            part=min(state["part"], state["parts"]),
            parts=state["parts"],
        ))

    def on_postprocess(status: dict) -> None:
        if cancel.is_set():
            raise _Cancelled()
        if on_progress is not None and status.get("status") == "started":
            detail = _POSTPROCESSOR_DETAILS.get(status.get("postprocessor"))
            if detail:
                on_progress(Progress("process", detail=detail.format(codec=(choice.audio_codec or "").upper())))

    extra: dict = {
        "format": choice.format,
        "outtmpl": str(TEMP_DIR / f"{job_id}.%(ext)s"),
        "logger": ytdlp_logger,
        "progress_hooks": [on_download],
        "postprocessor_hooks": [on_postprocess],
        # Stops a download early once the server reports a size far past the limit
        "max_filesize": int(upload_limit * 1.5),
    }
    if choice.is_audio:
        extract = {"key": "FFmpegExtractAudio", "preferredcodec": choice.audio_codec}
        if choice.audio_codec == "mp3":
            extract["preferredquality"] = str(MP3_BITRATE)
        extra["postprocessors"] = [
            extract,
            # Title/artist tags so music players show proper names
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False},
        ]
    else:
        extra["merge_output_format"] = "mp4"
        extra["format_sort"] = _VIDEO_FORMAT_SORT

    with _ydl(_build_ydl_opts(extra, proxied=_needs_proxy(raw.get("extractor_key")))) as ydl:
        # Pick the formats first, to refuse files that are known to be too big
        planning = copy.deepcopy(raw)
        for f in planning.get("formats") or []:
            # Testing doubtful formats is left to the real download below
            f.pop("__needs_testing", None)
        planned = ydl.process_ie_result(planning, download=False)
        estimate = _estimated_size(choice, planned)
        state["parts"] = len(planned.get("requested_formats") or [planned])
        if estimate and estimate > upload_limit:
            raise DownloadError(too_big_message(estimate))
        _check_disk_space(estimate)
        # Same path as yt-dlp's --load-info-json: no new info request
        try:
            info = ydl.process_ie_result(raw, download=True)
        except _TooBig:
            raise DownloadError(too_big_message())

    # yt-dlp records the final path, after merging and audio conversion
    downloads = info.get("requested_downloads") or [{}]
    filepath = downloads[-1].get("filepath")
    if not filepath or not Path(filepath).is_file():
        if ytdlp_logger.too_big:
            raise DownloadError(too_big_message())
        raise DownloadError("Download finished but the file was not found.")
    path = Path(filepath)

    title = info.get("title") or "video"
    uploader = info.get("uploader") or info.get("channel") or info.get("creator")
    probe = _probe(path)
    duration = _duration(info, probe)

    # Audio choices, and sites that only had audio, are sent as audio files
    if choice.is_audio or (probe.get("streams") and not _first_stream(probe, "video")):
        _check_final_size(path)
        return DownloadResult(job_id, key, path, title, uploader, duration, is_audio=True)

    path = _ensure_telegram_compatible(path, job_id, probe, cancel, on_progress, duration)
    _check_final_size(path)
    width, height = _display_size(_first_stream(_probe(path), "video"))
    return DownloadResult(
        job_id, key, path, title, uploader, duration, is_audio=False,
        width=width, height=height,
        thumb=_make_thumbnail(path, job_id, duration),
    )


async def _download(
    url: str,
    choice: Choice,
    raw: dict,
    from_saved: bool,
    on_wait: Optional[OnWait],
    on_progress: Optional[OnProgress] = None,
) -> DownloadResult:
    await _DOWNLOAD_SLOTS.acquire(on_wait)
    loop = asyncio.get_running_loop()
    job_id = uuid.uuid4().hex
    cancel = threading.Event()
    # process_ie_result modifies the info it's given, so pass a copy
    future = loop.run_in_executor(
        _DOWNLOAD_EXECUTOR, _download_sync, choice, job_id, copy.deepcopy(raw), cancel, on_progress,
    )
    release_slot = True
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=DOWNLOAD_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        # A thread can't be killed: tell it to stop, stop its helper processes,
        # and keep its download slot until it has really finished
        cancel.set()
        release_slot = False
        future.add_done_callback(_release_when_done(_DOWNLOAD_SLOTS))
        await asyncio.to_thread(_kill_job_processes, job_id)
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(future), timeout=_CANCEL_GRACE)
        cleanup_job_files(job_id)
        if isinstance(e, asyncio.CancelledError):
            # Like DownloadError.pending: lets the caller hold the user's slot
            # until a thread that didn't stop in time really has
            e.pending = None if future.done() else future
            raise
        raise DownloadError(
            "Download timed out. Try a lower quality.",
            pending=None if future.done() else future,
        )
    except DownloadError:
        cleanup_job_files(job_id)
        raise
    except yt_dlp.utils.DownloadError as e:
        cleanup_job_files(job_id)
        if _needs_proxy(raw.get("extractor_key")):
            proxy_error = await _proxy_down_error()
            if proxy_error:
                logger.warning("Download failed for %s: %s", url, e)
                raise proxy_error
        # Postprocessing (conversion) errors won't get better with fresh info
        if from_saved and "postprocessing:" not in str(e).lower():
            # Most likely the saved media links expired; the caller refetches once
            raise _SavedInfoFailed(str(e)) from e
        logger.warning("Download failed for %s: %s", url, e)
        raise DownloadError(_friendly_error(str(e)))
    except Exception:
        logger.exception("Unexpected error while downloading %s", url)
        cleanup_job_files(job_id)
        raise DownloadError("Something went wrong while downloading. Please try again.")
    finally:
        if release_slot:
            _DOWNLOAD_SLOTS.release()


async def download_video(
    url: str,
    choice: Choice,
    key: Optional[str] = None,
    on_wait: Optional[OnWait] = None,
    on_progress: Optional[OnProgress] = None,
) -> DownloadResult:
    """
    Download the chosen video quality or audio format. Reuses the info saved by
    get_info() when it's fresh, so a link normally costs one info request total.
    """
    _cleanup_stale_files()

    raw = _recall(key) if key else None
    if raw is not None:
        try:
            return await _download(url, choice, raw, from_saved=True, on_wait=on_wait, on_progress=on_progress)
        except _SavedInfoFailed as e:
            logger.info("Saved info for %s didn't work (%s); fetching it again", key, e)

    hint = key or await guess_key(url)
    raw = await _fetch_info(url, is_youtube_key(hint), on_wait)
    _remember(_video_key(raw), raw)
    return await _download(url, choice, raw, from_saved=False, on_wait=on_wait, on_progress=on_progress)
