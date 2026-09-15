import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from telegram import Bot, BotCommand, CallbackQuery, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import downloader
import texts
import uploader
from cache import Store
from config import (
    ADMIN_IDS,
    API_HASH,
    API_ID,
    BOT_TOKEN,
    CACHE_PATH,
    FILE_CACHE_TTL,
    MAX_CACHED_FILES,
    PROGRESS_INTERVAL,
    RELAY_CHANNEL_ID,
    SESSION_PATH,
    USER_DOWNLOADS_PER_HOUR,
    USER_LOOKUPS_PER_HOUR,
    USER_MAX_ACTIVE_DOWNLOADS,
    VIDEO_CACHE_TTL,
    config_error,
)
from downloader import (
    Choice,
    DownloadError,
    Progress,
    VideoInfo,
    check_pot_provider,
    check_proxy,
    cleanup_job_files,
    cleanup_temp_dir,
    download_video,
    get_info,
    guess_key,
)
from keyboards import build_quality_keyboard, cancel_keyboard, link_id
from limits import BUSY, UserLimits
from uploader import UploadCancelled

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx logs every Telegram API poll at INFO, which buries the bot's own logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Recent links kept per chat for their buttons; older menus show "expired"
MAX_LINKS_PER_CHAT = 20

# Longest RetryAfter worth waiting for before giving up on a status edit
MAX_EDIT_RETRY_WAIT = 30

store = Store(CACHE_PATH, RELAY_CHANNEL_ID, MAX_CACHED_FILES, VIDEO_CACHE_TTL, FILE_CACHE_TTL)
limits = UserLimits(ADMIN_IDS, USER_MAX_ACTIVE_DOWNLOADS, USER_DOWNLOADS_PER_HOUR, USER_LOOKUPS_PER_HOUR)

# Handler tasks still running, so a shutdown can cancel them and tell their users
_active_tasks: set[asyncio.Task] = set()
_shutting_down = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seconds(value) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


async def _edit(target: Message | CallbackQuery, text: str, reply_markup=None, retry: bool = True) -> bool:
    """
    Edit a status message. A failed edit must never abort the work in progress,
    so errors are logged and reported by returning False. A short flood wait
    is waited out once, so final messages ("Done", errors) aren't lost.
    """
    edit = target.edit_message_text if isinstance(target, CallbackQuery) else target.edit_text
    try:
        await edit(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return True
    except RetryAfter as e:
        wait = _seconds(e.retry_after)
        if retry and wait <= MAX_EDIT_RETRY_WAIT:
            await asyncio.sleep(wait)
            return await _edit(target, text, reply_markup, retry=False)
        logger.warning("Could not edit status message: %s", e)
        return False
    except (TelegramError, TypeError) as e:
        # PTB raises TypeError for callback messages too old to access
        if isinstance(e, TypeError) and "inaccessible" not in str(e).lower():
            raise
        if "not modified" in str(e).lower():
            return True
        logger.warning("Could not edit status message: %s", e)
        return False


async def _answer(query: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Answer a button tap. A stale or failed answer must not stop the work behind it."""
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramError as e:
        logger.warning("Could not answer callback query: %s", e)


def _remember_link(chat_data: dict, link: str, entry: dict) -> None:
    links: dict = chat_data.setdefault("links", {})
    links.pop(link, None)
    links[link] = entry
    while len(links) > MAX_LINKS_PER_CHAT:
        links.pop(next(iter(links)))


def _thread_id(message) -> int | None:
    """Forum topic of a message, so files land in the same topic."""
    return message.message_thread_id if getattr(message, "is_topic_message", False) else None


async def _copy_to_user(
    bot: Bot, chat_id: int, thread_id: int | None, relay_message_id: int, title: str, is_audio: bool,
) -> None:
    # Channel message IDs are the same for every member, so copyMessage works reliably
    await bot.copy_message(
        chat_id=chat_id,
        from_chat_id=RELAY_CHANNEL_ID,
        message_id=relay_message_id,
        message_thread_id=thread_id,
        caption=texts.caption(title, is_audio),
        parse_mode=ParseMode.HTML,
    )


def _relay_message_missing(error: BadRequest) -> bool:
    text = str(error).lower()
    return any(s in text for s in ("message to copy not found", "message_id_invalid", "message not found"))


async def _delete_relay_message(bot: Bot, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=RELAY_CHANNEL_ID, message_id=message_id)
    except TelegramError as e:
        logger.warning(
            "Could not delete relay message %s (does the bot have delete rights?): %s",
            message_id, e,
        )


def _tracked(handler):
    """Register a handler's task while it runs, so shutdown can cancel it cleanly."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        task = asyncio.current_task()
        _active_tasks.add(task)
        try:
            await handler(update, context)
        finally:
            _active_tasks.discard(task)
    return wrapper


# ---------------------------------------------------------------------------
# Download progress and cancelling
# ---------------------------------------------------------------------------

class _StatusUpdater:
    """
    Shows a download's progress on its status message: at most one edit every
    PROGRESS_INTERVAL seconds (Telegram rate-limits edits), always with a Cancel button.
    """

    def __init__(self, query: CallbackQuery, title: str, label: str, token: str):
        self._query = query
        self._title = title
        self._label = label
        self._keyboard = cancel_keyboard(token)
        self._loop = asyncio.get_running_loop()
        self._pending: Optional[Progress] = None
        self._flusher: Optional[asyncio.Task] = None
        self._next_edit = 0.0
        self._last_text: Optional[str] = None
        self.closed = False

    def report(self, progress: Progress) -> None:
        """Take a progress report. Safe to call from any thread."""
        self._loop.call_soon_threadsafe(self._schedule, progress)

    def _schedule(self, progress: Progress) -> None:
        if self.closed:
            return
        self._pending = progress
        if self._flusher is None or self._flusher.done():
            self._flusher = self._loop.create_task(self._flush())

    async def _flush(self) -> None:
        delay = self._next_edit - self._loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        progress, self._pending = self._pending, None
        if progress is not None:
            await self.show(texts.progress(self._title, self._label, progress))

    async def show(self, text: str) -> None:
        """Edit the status now (keeping the Cancel button), unless closed or unchanged."""
        if self.closed or text == self._last_text:
            return
        try:
            await self._query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=self._keyboard)
            self._last_text = text
        except RetryAfter as e:
            self._next_edit = self._loop.time() + _seconds(e.retry_after)
            return
        except (TelegramError, TypeError) as e:
            if "not modified" not in str(e).lower():
                logger.debug("Progress edit failed: %s", e)
        self._next_edit = self._loop.time() + PROGRESS_INTERVAL

    def close(self) -> None:
        """Stop showing progress, so later status edits aren't overwritten."""
        self.closed = True
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()


@dataclass
class _Job:
    """A running download that its user can cancel."""
    user_id: int
    task: asyncio.Task
    updater: _StatusUpdater
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    uploading: bool = False
    finished: bool = False  # the outcome is decided; Cancel can't change it any more

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> bool:
        """Ask the job to stop. False if it's already finishing or cancelled."""
        if self.finished or self.cancelled:
            return False
        self.cancel_event.set()
        self.updater.close()
        if not self.uploading:
            self.task.cancel()  # an upload watches cancel_event instead
        return True


_jobs: dict[str, _Job] = {}


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        token = json.loads(query.data)["c"]
    except (json.JSONDecodeError, KeyError, TypeError):
        token = None
    job = _jobs.get(token)
    if job is None or job.finished:
        await _answer(query, texts.nothing_to_cancel())
        return
    user_id = update.effective_user.id
    if user_id != job.user_id and user_id not in ADMIN_IDS:
        await _answer(query, texts.not_your_download(), show_alert=True)
        return
    # Decide before any network round trip; the download's own handler writes
    # the final message, so nothing here can overwrite "Done" or "Cancelled"
    cancelled = job.cancel()
    await _answer(query, texts.cancelling() if cancelled else texts.nothing_to_cancel())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _begin_shutdown(app: Application) -> None:
    global _shutting_down
    if _shutting_down:
        logger.warning("Second stop signal: exiting immediately.")
        os._exit(1)
    _shutting_down = True
    logger.info("Stopping: cancelling %d running job(s)…", len(_active_tasks))
    for task in list(_active_tasks):
        task.cancel()
    if app.running:
        app.stop_running()
    else:
        # Still starting up, where stop_running() would be missed; PTB treats
        # SystemExit as a stop request at any point
        raise SystemExit(0)


def _install_signal_handlers(app: Application) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _begin_shutdown, app)
        except (NotImplementedError, RuntimeError):
            pass


async def post_init(app: Application) -> None:
    # First, so a stop during the slow startup steps below is handled too
    _install_signal_handlers(app)
    cleanup_temp_dir()
    await asyncio.to_thread(store.load)
    await downloader.warm_up()
    await uploader.pyro.start()
    logger.info("Pyrogram uploader client started.")
    await uploader.prime_peer_cache()
    await uploader.check_relay_channel()
    try:
        await app.bot.get_chat(RELAY_CHANNEL_ID)
    except TelegramError as e:
        raise RuntimeError(
            f"The bot can't open the relay channel {RELAY_CHANNEL_ID} ({e}). "
            "Add the bot to the channel as an admin."
        ) from e
    downloader.set_upload_limit(uploader.upload_limit())
    logger.info("Upload limit: %d MiB", downloader.upload_limit // 2**20)
    # /start and /help in Telegram's command menu
    try:
        await app.bot.set_my_commands([BotCommand(name, description) for name, description in texts.commands()])
    except TelegramError as e:
        logger.warning("Could not set the bot's command menu: %s", e)
    await check_pot_provider()
    await check_proxy()


async def post_shutdown(app: Application) -> None:
    if uploader.pyro.is_connected:
        await uploader.pyro.stop()
    downloader.shutdown()
    logger.info("Stopped.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(texts.start(), parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(texts.help_text(), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Link handler — video details and the download keyboard
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    match = URL_RE.search(message.text or "")
    if not match:
        if update.effective_chat.type == ChatType.PRIVATE:
            await message.reply_text(texts.no_link(), parse_mode=ParseMode.HTML)
        return

    url = match.group(0)
    user_id = update.effective_user.id
    status = await message.reply_text(texts.fetching(), parse_mode=ParseMode.HTML)

    try:
        guess = await guess_key(url)
        cached = store.video(guess) if guess else None
        if cached is not None:
            info = VideoInfo.from_dict(cached)
        else:
            wait = limits.take_lookup(user_id)
            if wait:
                await _edit(status, texts.lookup_limit(wait))
                return

            async def on_wait(queue: str, position: int) -> None:
                await _edit(status, texts.queued(queue, position) if position else texts.fetching())

            try:
                info = await get_info(url, key_hint=guess, on_wait=on_wait)
            except DownloadError as e:
                await _edit(status, texts.error(str(e)))
                return
            await asyncio.to_thread(store.put_video, info.to_dict(), guess)

        link = link_id(url)
        keyboard, choices = build_quality_keyboard(info, link)
        # Chat-scoped, so anyone in a group can use the menu, whoever sent the link
        _remember_link(context.chat_data, link, {
            "url": url, "key": info.key, "title": info.title, "choices": choices,
        })
        if not await _edit(status, texts.choose(info), reply_markup=keyboard):
            await message.reply_text(texts.choose(info), parse_mode=ParseMode.HTML, reply_markup=keyboard)

    except asyncio.CancelledError:
        await _edit(status, texts.restarting())
        raise
    except Exception:
        logger.exception("Unexpected error while handling a link")
        await _edit(status, texts.error(texts.GENERIC_ERROR))


# ---------------------------------------------------------------------------
# Choice handler — cache → download → MTProto upload → copy to user
# ---------------------------------------------------------------------------

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        payload = json.loads(query.data)
        label: str = payload["l"]
        link: str = payload["h"]
    except (json.JSONDecodeError, KeyError, TypeError):
        payload = None

    entry = (context.chat_data or {}).get("links", {}).get(link) if payload else None
    choice: Choice | None = entry["choices"].get(label) if entry else None
    if choice is None:
        await _answer(query)
        await _edit(query, texts.menu_expired())
        return

    chat_id = update.effective_chat.id
    thread_id = _thread_id(query.message)
    user_id = update.effective_user.id
    title: str = entry["title"]
    keyboard = getattr(query.message, "reply_markup", None)
    answered = False

    # 1. Already uploaded before? Re-send it instantly; costs no download or limit
    cached_message_id = store.file(entry["key"], choice.label)
    if cached_message_id is not None:
        await _answer(query)
        answered = True
        try:
            await _copy_to_user(context.bot, chat_id, thread_id, cached_message_id, title, choice.is_audio)
            await _edit(query, texts.done_instantly(title))
            return
        except BadRequest as e:
            if not _relay_message_missing(e):
                logger.warning("Could not send cached file %s to chat %s: %s", cached_message_id, chat_id, e)
                await _edit(query, texts.error(texts.DELIVERY_FAILED), reply_markup=keyboard)
                return
            logger.info("Cached relay message %s is gone (%s); downloading again", cached_message_id, e)
            await asyncio.to_thread(store.drop_file, entry["key"], choice.label, cached_message_id)
        except TelegramError as e:
            logger.warning("Could not send cached file %s to chat %s: %s", cached_message_id, chat_id, e)
            await _edit(query, texts.error(texts.DELIVERY_FAILED), reply_markup=keyboard)
            return

    # 2. Per-user limits
    verdict = limits.start_download(user_id)
    if verdict:
        alert = texts.busy_alert() if verdict == BUSY else texts.download_limit_alert(verdict)
        if answered:
            await _edit(query, texts.error(alert), reply_markup=keyboard)
        else:
            await _answer(query, alert, show_alert=True)
        return

    token = secrets.token_hex(4)
    updater = _StatusUpdater(query, title, choice.label, token)
    job = _Job(user_id, asyncio.current_task(), updater)
    _jobs[token] = job

    def finish() -> None:
        """The outcome is decided: Cancel can no longer change it, and progress stops."""
        job.finished = True
        _jobs.pop(token, None)
        updater.close()

    result = None
    pending = None
    try:
        if not answered:
            await _answer(query)
        await updater.show(texts.downloading(title, choice.label))

        async def on_wait(queue: str, position: int) -> None:
            await updater.show(texts.queued(queue, position) if position else texts.downloading(title, choice.label))

        # Step 1: download (and convert) into TEMP_DIR — removed in finally
        result = await download_video(
            entry["url"], choice, key=entry["key"], on_wait=on_wait, on_progress=updater.report,
        )
        logger.info("Download complete: %s", result.path)

        # Step 2: upload via Pyrogram (MTProto) to the relay channel
        job.uploading = True
        if job.cancelled:
            raise UploadCancelled()  # tapped just as the download finished
        await updater.show(texts.uploading(title))
        relay_msg = await uploader.send_to_relay(
            result, on_progress=updater.report, cancel_event=job.cancel_event,
        )
        evicted = await asyncio.to_thread(store.put_file, result.key, choice.label, relay_msg.id)
        for old_id in evicted:
            context.application.create_task(_delete_relay_message(context.bot, old_id))
        if job.cancelled:
            raise UploadCancelled()  # cancelled as the upload finished: kept in the cache only
        finish()

        # Step 3: copy to the user; the relay copy stays cached for next time
        await _copy_to_user(context.bot, chat_id, thread_id, relay_msg.id, title, choice.is_audio)
        logger.info("Delivered to chat_id=%s", chat_id)
        await _edit(query, texts.done(title))

    except asyncio.CancelledError as e:
        finish()
        pending = getattr(e, "pending", None)
        if job.cancelled and not _shutting_down:
            # Cancelled by its user: the handler itself carries on
            asyncio.current_task().uncancel()
            await _edit(query, texts.cancelled(title), reply_markup=keyboard)
        else:
            await _edit(query, texts.restarting())
            raise
    except UploadCancelled:
        finish()
        await _edit(query, texts.cancelled(title), reply_markup=keyboard)
    except (DownloadError, RuntimeError) as e:
        finish()
        pending = getattr(e, "pending", None)
        # Keep the menu so the user can retry or pick another format
        await _edit(query, texts.error(str(e)), reply_markup=keyboard)
    except TelegramError as e:
        finish()
        logger.warning("Could not deliver to chat %s: %s", chat_id, e)
        await _edit(query, texts.error(texts.DELIVERY_FAILED), reply_markup=keyboard)
    except Exception:
        finish()
        logger.exception("Unexpected error in handle_choice")
        await _edit(query, texts.error(texts.GENERIC_ERROR), reply_markup=keyboard)
    finally:
        finish()
        if pending is not None:
            # The download is still stopping: keep the user's slot until it has
            pending.add_done_callback(lambda _: limits.finish_download(user_id))
        else:
            limits.finish_download(user_id)
        if result is not None:
            cleanup_job_files(result.job_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _session_logged_in() -> bool:
    """True once setup_session.py has finished logging the uploader account in."""
    if not SESSION_PATH.exists():
        return False
    try:
        with contextlib.closing(sqlite3.connect(SESSION_PATH.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT user_id FROM sessions").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def _check_settings() -> None:
    missing = [
        name for name, value in (
            ("BOT_TOKEN", BOT_TOKEN), ("API_ID", API_ID), ("API_HASH", API_HASH), ("RELAY_CHANNEL_ID", RELAY_CHANNEL_ID),
        ) if not value
    ]
    if missing:
        config_error(f"Missing settings in .env: {', '.join(missing)}. See README, 'Configure .env'.")
    if not _session_logged_in():
        config_error("The uploader account isn't logged in yet. Run setup_session.py first (see README).")


def main() -> None:
    _check_settings()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        # Without this, one long download blocks every other user's messages
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
            _tracked(handle_text),
        )
    )
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern=r'^\{"c":'))
    app.add_handler(CallbackQueryHandler(_tracked(handle_choice)))
    app.add_error_handler(on_error)

    logger.info("Bot is running…")
    # Stop signals are handled in post_init, so running jobs are cancelled cleanly
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], stop_signals=None)
    # Everything is saved and closed by now. Worker threads stuck in network
    # reads can't be stopped and would otherwise keep the process alive
    # until systemd kills it.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
