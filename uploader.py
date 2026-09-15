import asyncio
import contextlib
import logging
import os
import re
import time
from collections import deque
from typing import Awaitable, Callable, Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from config import (
    API_HASH,
    API_ID,
    BASE_DIR,
    MAX_CONCURRENT_UPLOADS,
    MAX_UPLOAD_BYTES,
    PREMIUM_MAX_UPLOAD_BYTES,
    RELAY_CHANNEL_ID,
)
from downloader import DownloadResult, OnProgress, Progress

logger = logging.getLogger(__name__)

# Longest FloodWait worth sleeping through before giving up on an upload
_MAX_FLOOD_WAIT = 600

pyro = Client(
    "uploader",
    api_id=API_ID,
    api_hash=API_HASH,
    # The session file lives next to the code, whatever the working directory
    workdir=str(BASE_DIR),
    # Pyrogram uploads one file at a time unless told otherwise
    max_concurrent_transmissions=MAX_CONCURRENT_UPLOADS,
    # Sleep through ordinary flood waits instead of failing the upload
    sleep_threshold=180,
)


class UploadCancelled(Exception):
    """The user cancelled while the file was uploading."""


async def prime_peer_cache() -> None:
    """
    Iterate through the user account's dialogs so Pyrogram populates its
    SQLite peer cache. Without this, numeric channel IDs can't be resolved
    on a fresh session. Called once after pyro.start().
    """
    logger.info("Priming Pyrogram peer cache from dialogs…")
    count = 0
    async for dialog in pyro.get_dialogs():
        count += 1
    logger.info("Peer cache primed: %d dialogs loaded", count)


async def check_relay_channel() -> None:
    """Fail startup loudly if the uploader account can't reach the relay channel."""
    try:
        chat = await pyro.get_chat(RELAY_CHANNEL_ID)
    except Exception as e:
        raise RuntimeError(
            f"The uploader account can't open the relay channel {RELAY_CHANNEL_ID} ({e}). "
            "Make sure it has joined the channel as an admin and RELAY_CHANNEL_ID is right."
        ) from e
    logger.info("Uploader account can reach relay channel %r", chat.title)


def upload_limit() -> int:
    """Largest file the uploader account may send (higher with Telegram Premium)."""
    return PREMIUM_MAX_UPLOAD_BYTES if pyro.me and pyro.me.is_premium else MAX_UPLOAD_BYTES


def _file_name(title: str, suffix: str) -> str:
    """File name users see in Telegram, built from the video title."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)
    name = re.sub(r"\s+", " ", name).strip()[:100] or "video"
    return f"{name}{suffix}"


class _RateMeter:
    """Transfer speed over the last few seconds, and the time left at that speed."""

    def __init__(self, window: float = 5.0):
        self._window = window
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, done: int, total: int) -> tuple[Optional[float], Optional[float]]:
        now = time.monotonic()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        started, done_then = self._samples[0]
        if now <= started or done <= done_then:
            return None, None
        speed = (done - done_then) / (now - started)
        return speed, (total - done) / speed if total else None


async def _send(result: DownloadResult, progress: Callable) -> Optional[Message]:
    path = str(result.path)
    if result.is_audio:
        return await pyro.send_audio(
            chat_id=RELAY_CHANNEL_ID,
            audio=path,
            duration=result.duration,
            title=result.title[:128],
            performer=result.uploader,
            file_name=_file_name(result.title, result.path.suffix),
            parse_mode=ParseMode.DISABLED,
            progress=progress,
        )
    return await pyro.send_video(
        chat_id=RELAY_CHANNEL_ID,
        video=path,
        duration=result.duration,
        width=result.width,
        height=result.height,
        thumb=str(result.thumb) if result.thumb else None,
        file_name=_file_name(result.title, result.path.suffix),
        parse_mode=ParseMode.DISABLED,
        supports_streaming=True,
        progress=progress,
    )


async def _unless_cancelled(work: Awaitable, cancel_event: asyncio.Event):
    """
    Await `work`, but stop it and raise UploadCancelled as soon as cancel_event
    is set — including while it waits for a free upload slot, sleeps through a
    flood wait, or sends the final message after the last chunk.
    """
    task = asyncio.ensure_future(work)
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        waiter.cancel()
        raise
    waiter.cancel()
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise UploadCancelled()
    return task.result()


async def send_to_relay(
    result: DownloadResult,
    on_progress: Optional[OnProgress] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Message:
    """
    Upload a downloaded file to the relay channel via the user account
    (MTProto). Returns the Pyrogram Message object — its .id is used to copy
    the file to the end user via the Bot API. Raises UploadCancelled once
    cancel_event is set.
    """
    logger.info("Uploading via MTProto is_audio=%s path=%s (%dx%d)",
                result.is_audio, result.path, result.width, result.height)
    cancel_event = cancel_event or asyncio.Event()
    meter = _RateMeter()

    async def progress(current: int, total: int) -> None:
        if cancel_event.is_set():
            pyro.stop_transmission()  # Pyrogram stops the upload and returns None
        if on_progress is not None:
            speed, eta = meter.update(current, total)
            on_progress(Progress("upload", done=current, total=total, speed=speed, eta=eta))

    for attempt in (1, 2):
        if cancel_event.is_set():
            raise UploadCancelled()
        try:
            # Fresh mtime keeps the stale-file sweep away while uploads queue up
            os.utime(result.path)
            msg = await _unless_cancelled(_send(result, progress), cancel_event)
        except FloodWait as e:
            wait = int(e.value)
            if attempt == 2 or wait > _MAX_FLOOD_WAIT:
                logger.warning("Upload rate-limited by Telegram for %ss; giving up", wait)
                raise RuntimeError(
                    "Telegram is limiting uploads right now. Please try again in a few minutes."
                ) from e
            logger.warning("Upload rate-limited by Telegram; retrying in %ss", wait)
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            raise UploadCancelled() from e
        except UploadCancelled:
            logger.info("Upload cancelled by the user")
            raise
        except Exception as e:
            logger.exception("MTProto upload failed")
            raise RuntimeError("The upload to Telegram failed. Please try again.") from e
        if msg is None:
            logger.info("Upload cancelled by the user")
            raise UploadCancelled()
        logger.info("MTProto upload complete: channel message_id=%s", msg.id)
        return msg
    raise AssertionError("unreachable")
