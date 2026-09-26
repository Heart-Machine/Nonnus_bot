"""Getting a post ready to send: downloading and converting it off the event
loop, a few at a time, once per post however many ask for it at once."""

import logging
import asyncio
import shutil
import tempfile
import weakref
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Optional, Tuple

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from nonnus import config, links, media, instagram, cache, delivery, progress, users


logger = logging.getLogger(__name__)


DOWNLOAD_SLOTS = asyncio.Semaphore(config.MAX_PARALLEL_DOWNLOADS)


# Share links already resolved, by their path. The client sends an inline
# query on every keystroke, and the post behind a share link does not
# change, so Instagram is asked once. Bounded, oldest first out.
RESOLVED_SHARE_LINKS: "OrderedDict[str, str]" = OrderedDict()
RESOLVED_SHARE_LINKS_LIMIT = 1000


async def resolve_link(url: str) -> Optional[str]:
    """The link to work with: `url` itself, or for a share link the post it
    stands for - None if Instagram would not say. Everything downstream -
    the cache key, the deep link, yt-dlp - needs the real shortcode, which a
    share link does not carry."""
    if not links.is_share_link(url):
        return url

    key = urlparse(url).path.rstrip("/")
    if key in RESOLVED_SHARE_LINKS:
        RESOLVED_SHARE_LINKS.move_to_end(key)
        return RESOLVED_SHARE_LINKS[key]

    post = await asyncio.to_thread(instagram.resolve_share_link, url)
    if post is not None:
        RESOLVED_SHARE_LINKS[key] = post
        while len(RESOLVED_SHARE_LINKS) > RESOLVED_SHARE_LINKS_LIMIT:
            RESOLVED_SHARE_LINKS.popitem(last=False)
    return post


async def alert_if_cookies_rejected(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell the owner in the storage chat that the session cookies stopped
    working. Downloads carry on without them meanwhile; this is so it gets
    noticed before someone sends a post that needs a login, not after."""
    if not instagram.INSTAGRAM_SESSION.take_alert() or not config.STORAGE_CHAT_ID:
        return

    try:
        await context.bot.send_message(chat_id=delivery.parse_storage_chat_id(), text=instagram.COOKIE_ALERT_TEXT)
    except TelegramError:
        logger.exception("Failed to send the Instagram cookie alert to the storage chat")


async def download_post_in_thread(
    url: str,
    download_dir: Path,
    context: ContextTypes.DEFAULT_TYPE,
) -> Tuple[list[media.MediaItem], str]:
    """download_post off the event loop. The download only notes that the
    cookies were turned down - it runs in a worker thread and cannot talk to
    Telegram - so the alert goes out from here, and in `finally`: the owner
    should hear about the cookies even when the post then fails for some
    other reason."""
    try:
        await take_slot_reporting_the_wait()
        try:
            progress.report(progress.DOWNLOADING)
            return await asyncio.to_thread(instagram.download_post, url, download_dir)
        finally:
            DOWNLOAD_SLOTS.release()
    finally:
        await alert_if_cookies_rejected(context)


async def prepare_items_in_thread(items: list[media.MediaItem], work_dir: Path) -> Tuple[list[media.MediaItem], bool]:
    """prepare_items_for_upload off the event loop, in one of the same slots
    downloads take: compressing a video is ffmpeg at full tilt."""
    await take_slot_reporting_the_wait()
    try:
        if config.ENABLE_VIDEO_COMPRESSION and any(
            item.is_video and item.path.stat().st_size > config.MAX_FILE_SIZE_BYTES for item in items
        ):
            progress.report(progress.COMPRESSING)
        return await asyncio.to_thread(media.prepare_items_for_upload, items, work_dir)
    finally:
        DOWNLOAD_SLOTS.release()


async def take_slot_reporting_the_wait() -> None:
    """Acquire a download slot, saying so first when there is a wait: with
    every slot busy a request can sit here for minutes, and without a word
    it would look like the bot hung."""
    if DOWNLOAD_SLOTS.locked():
        progress.report(progress.QUEUED)
    await DOWNLOAD_SLOTS.acquire()


async def prepare_inline_post(
    url: str,
    context: ContextTypes.DEFAULT_TYPE,
    tracker: Optional[progress.Progress] = None,
) -> dict[str, Any]:
    # Stages reported from here on, the worker threads included, go to this
    # preparation's tracker. The variable is set inside this task's own
    # context, so it never leaks into anyone else's.
    if tracker is not None:
        progress.CURRENT.set(tracker)

    cached_result = cache.get_cached_inline_result(url)
    if cached_result:
        return cached_result

    temp_dir = Path(tempfile.mkdtemp(prefix="ig_inline_"))
    try:
        return await _prepare(url, context, temp_dir)
    except Exception as error:
        # A failed preparation is logged in full once, by the application's
        # error handler, which knows nothing of the post; the note puts the
        # link into that traceback. Everyone waiting on the task logs a line.
        error.add_note(f"While preparing {url}")
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _prepare(url: str, context: ContextTypes.DEFAULT_TYPE, temp_dir: Path) -> dict[str, Any]:
    items, caption = await download_post_in_thread(url, temp_dir, context)
    items, compressed = await prepare_items_in_thread(items, temp_dir)
    caption = media.add_compression_note_if_needed(caption, compressed)
    media.ensure_items_fit_telegram(items)

    progress.report(progress.UPLOADING)
    cached_result = {
        "caption": caption,
        "title": delivery.title_from_caption(caption),
        "items": await delivery.upload_items_to_storage(context, items, caption),
    }
    cache.save_cached_inline_result(url, cached_result)
    return cached_result


def describe_failure(error: BaseException) -> str:
    """One line for a failure whose traceback is already in the log - yt-dlp's
    messages run to a paragraph."""
    text = str(error).strip()
    first_line = text.splitlines()[0] if text else ""
    return f"{type(error).__name__}: {first_line[:200]}"


# The progress of each running preparation, by its task. Weak, so a task's
# tracker goes away with the task.
TRACKERS: "weakref.WeakKeyDictionary[asyncio.Task, progress.Progress]" = weakref.WeakKeyDictionary()


def progress_of(task: asyncio.Task) -> Optional[progress.Progress]:
    """The tracker of a preparation task, to follow what it is doing."""
    return TRACKERS.get(task)


# How long a failed preparation keeps answering inline queries for its post.
# The client sends a new inline query on every keystroke, so without this each
# one would start the download over - for a private or deleted post, two full
# yt-dlp runs apiece, with the cookies and without.
FAILED_PREPARATION_MEMORY_SECONDS = 60


def get_or_create_prepare_task(
    url: str,
    context: ContextTypes.DEFAULT_TYPE,
    reuse_failure: bool = False,
    user: Any = None,
) -> asyncio.Task:
    """Reuse an in-flight prepare_inline_post() task for the same post so
    concurrent requests - inline queries and direct messages alike - don't
    trigger duplicate downloads and uploads for the same URL.

    A task that failed stays on hand for FAILED_PREPARATION_MEMORY_SECONDS,
    and with reuse_failure it is what comes back - already done, so the
    inline query answers with the error at once instead of downloading again.
    A link sent to the bot directly is someone asking for another try, so
    without reuse_failure a failed task is replaced by a new one.

    A new task is a new download from Instagram, the one thing the daily
    limit counts: it goes on `user`'s count, or raises
    users.DailyLimitReached, and is given back if the task fails. Joining a
    task under way costs nothing - that download is already happening."""
    cache_key = links.normalize_post_url(url)
    inline_tasks = context.application.bot_data.setdefault("inline_tasks", {})
    task = inline_tasks.get(cache_key)
    if task is not None and (not task.done() or reuse_failure):
        return task

    download = users.take_download(user)
    tracker = progress.Progress(asyncio.get_running_loop())
    task = context.application.create_task(prepare_inline_post(url, context, tracker))
    TRACKERS[task] = tracker
    inline_tasks[cache_key] = task

    def forget_task(key: str = cache_key, done_task: asyncio.Task = task) -> None:
        if inline_tasks.get(key) is done_task:
            inline_tasks.pop(key, None)

    def on_done(done_task: asyncio.Task) -> None:
        if done_task.cancelled() or done_task.exception() is not None:
            users.give_back_download(download)

        # A post that came through is in the cache now, so its task has
        # nothing more to offer; a failed one is kept a while as the answer.
        if done_task.cancelled() or done_task.exception() is None:
            forget_task()
        else:
            asyncio.get_running_loop().call_later(FAILED_PREPARATION_MEMORY_SECONDS, forget_task)

    task.add_done_callback(on_done)
    return task
