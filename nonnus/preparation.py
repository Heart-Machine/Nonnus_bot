"""Getting a post ready to send: downloading and converting it off the event
loop, a few at a time, once per post however many ask for it at once."""

import logging
import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any, Tuple

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from nonnus import config, links, media, instagram, cache, delivery


logger = logging.getLogger(__name__)


DOWNLOAD_SLOTS = asyncio.Semaphore(config.MAX_PARALLEL_DOWNLOADS)


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
        async with DOWNLOAD_SLOTS:
            return await asyncio.to_thread(instagram.download_post, url, download_dir)
    finally:
        await alert_if_cookies_rejected(context)


async def prepare_items_in_thread(items: list[media.MediaItem], work_dir: Path) -> Tuple[list[media.MediaItem], bool]:
    """prepare_items_for_upload off the event loop, in one of the same slots
    downloads take: compressing a video is ffmpeg at full tilt."""
    async with DOWNLOAD_SLOTS:
        return await asyncio.to_thread(media.prepare_items_for_upload, items, work_dir)


async def prepare_inline_post(url: str, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    cached_result = cache.get_cached_inline_result(url)
    if cached_result:
        return cached_result

    temp_dir = Path(tempfile.mkdtemp(prefix="ig_inline_"))
    try:
        items, caption = await download_post_in_thread(url, temp_dir, context)
        items, compressed = await prepare_items_in_thread(items, temp_dir)
        caption = media.add_compression_note_if_needed(caption, compressed)
        media.ensure_items_fit_telegram(items)

        cached_result = {
            "caption": caption,
            "title": delivery.title_from_caption(caption),
            "items": await delivery.upload_items_to_storage(context, items, caption),
        }
        cache.save_cached_inline_result(url, cached_result)
        return cached_result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def get_or_create_prepare_task(url: str, context: ContextTypes.DEFAULT_TYPE) -> asyncio.Task:
    """Reuse an in-flight prepare_inline_post() task for the same post so
    concurrent requests - inline queries and direct messages alike - don't
    trigger duplicate downloads and uploads for the same URL."""
    cache_key = links.normalize_post_url(url)
    inline_tasks = context.application.bot_data.setdefault("inline_tasks", {})
    task = inline_tasks.get(cache_key)
    if task is None or task.done():
        task = context.application.create_task(prepare_inline_post(url, context))
        inline_tasks[cache_key] = task

        def forget_task(done_task: asyncio.Task, key: str = cache_key) -> None:
            if inline_tasks.get(key) is done_task:
                inline_tasks.pop(key, None)

        task.add_done_callback(forget_task)

    return task
