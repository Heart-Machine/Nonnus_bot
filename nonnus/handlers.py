"""Handlers for commands and for links sent to the bot directly."""

import asyncio
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from nonnus import config, links, media, instagram, cache, delivery, preparation, progress


logger = logging.getLogger(__name__)


INCOMPLETE_POST_TEXT = (
    "Не получилось скачать публикацию целиком: часть файлов не загрузилась. "
    "Попробуй ещё раз чуть позже."
)


SEND_FAILED_TEXT = (
    "Не получилось отправить публикацию в этот чат. Если она не пришла, пришли ссылку ещё раз."
)


UPLOAD_FAILED_TEXT = (
    "Публикация скачалась, но загрузить её в Telegram не получилось. Попробуй ещё раз чуть позже."
)


UNEXPECTED_ERROR_TEXT = "Что-то пошло не так. Попробуй ещё раз чуть позже."


SHARE_LINK_UNRESOLVED_TEXT = (
    "Не получилось понять, на какой пост ведёт эта ссылка: Instagram не ответил. "
    "Пришли обычную ссылку на пост - в Instagram это «Копировать ссылку»."
)


DOWNLOADING_TEXT = "Скачиваю публикацию..."


def progress_text(stage: str, done: int = 0, total: int = 0) -> str:
    if stage == progress.QUEUED:
        return "Жду своей очереди: сейчас скачиваются другие публикации..."
    if stage == progress.COMPRESSING:
        return "Видео большое, сжимаю перед отправкой..."
    if stage == progress.UPLOADING:
        return "Загружаю в Telegram..."
    if total > 1:
        return f"Скачиваю публикацию: {done} из {total}..."
    return DOWNLOADING_TEXT


# The least time between two edits of a status message. Telegram limits how
# often a message can be edited, and a count ticking up every half second is
# no more use than one every two.
STATUS_EDIT_INTERVAL_SECONDS = 2.0


class StatusMessage:
    """The "Скачиваю публикацию..." message under a link: kept up to date while
    the post is prepared, then settled once - deleted when the post has gone
    out, or turned into what went wrong.

    Progress edits are thinned out to one per STATUS_EDIT_INTERVAL_SECONDS,
    latest text winning, and run in the background so that the preparation
    never waits on one. Settling cancels whatever edit is still pending, so a
    late "3 из 10" cannot overwrite the error it was followed by."""

    def __init__(self, message, text: str) -> None:
        self._message = message
        self._shown = text
        self._wanted = text
        self._last_edit = time.monotonic()
        self._pending: Optional[asyncio.Task] = None
        self._settled = False

    @classmethod
    async def send(cls, reply_to, text: str = DOWNLOADING_TEXT) -> "StatusMessage":
        return cls(await reply_to.reply_text(text), text)

    def show(self, text: str) -> None:
        if self._settled:
            return
        self._wanted = text
        if self._pending is None or self._pending.done():
            self._pending = asyncio.get_running_loop().create_task(self._edit_when_due())

    def follow(self, tracker: Optional[progress.Progress]) -> None:
        if tracker is not None:
            tracker.subscribe(lambda stage, done, total: self.show(progress_text(stage, done, total)))

    async def _edit_when_due(self) -> None:
        delay = self._last_edit + STATUS_EDIT_INTERVAL_SECONDS - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._settled or self._wanted == self._shown:
            return

        text = self._wanted
        try:
            await self._message.edit_text(text)
            self._shown = text
        except TelegramError:
            # A progress line is not worth failing over; the final edit is.
            logger.warning("Failed to update a status message", exc_info=True)
        finally:
            self._last_edit = time.monotonic()

    async def _settle(self) -> None:
        self._settled = True
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            # gather returns the edit's own CancelledError instead of
            # raising it, while a cancellation of this handler still goes
            # through - suppressing CancelledError here would swallow that.
            await asyncio.gather(self._pending, return_exceptions=True)

    async def fail(self, text: str) -> None:
        await self._settle()
        await self._message.edit_text(text)

    async def done(self) -> None:
        await self._settle()
        await self._message.delete()


def too_large_text(error: media.MediaTooLargeError) -> str:
    """Name the limit that was actually crossed: a photo's is five times
    lower than a video's, and "larger than 50 MB" about a 12 MB photo would be
    plainly wrong."""
    what = "видео" if error.kind == "video" else "фото"
    return f"Публикация скачалась, но {what} в ней больше {error.limit_mb} МБ, а Telegram такие не принимает."


async def is_message_addressed_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if message is None:
        return False

    if message.chat.type == "private":
        return True

    username = await delivery.get_bot_username(context)
    if not username:
        return False

    return re.search(rf"@{re.escape(username)}(?![A-Za-z0-9_])", message.text or "", re.IGNORECASE) is not None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    # The button under an inline carousel opens t.me/<bot>?start=<payload>,
    # which arrives here as /start <payload>.
    if context.args:
        url = links.post_url_from_start_payload(context.args[0])
        if url:
            await deliver_post(message, url, context)
            return

    await message.reply_text(
        "Пришли ссылку на Instagram — Reel, пост с фото или карусель, "
        "а я отправлю всё содержимое сюда."
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    await message.reply_text(f"Chat ID: <code>{message.chat_id}</code>", parse_mode=ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    if not await is_message_addressed_to_bot(update, context):
        return

    url = links.find_instagram_url(message.text)
    if not url:
        await message.reply_text(
            "Не вижу ссылку на Instagram. Пришли ссылку на Reel или пост в формате: "
            "instagram.com / reel / CODE или instagram.com / p / CODE",
            disable_web_page_preview=True,
        )
        return

    post_url = await preparation.resolve_link(url)
    if post_url is None:
        await message.reply_text(SHARE_LINK_UNRESOLVED_TEXT, disable_web_page_preview=True)
        return

    await deliver_post(message, post_url, context)


async def deliver_post(message, url: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the post behind `url` to the chat `message` came from - as an
    album when it is a carousel - from the file_id cache when it is there and
    downloading it otherwise. Shared by links sent to the bot and by the
    /start deep link behind the inline carousel button.

    Every failure the flow foresees settles the status message with its own
    text. Anything else is caught here, so the message never stays at
    "Скачиваю публикацию..." with nothing coming."""
    status = await StatusMessage.send(message)
    try:
        await _deliver_post(message, url, context, status)
    except Exception:
        logger.exception("Unexpected failure delivering %s", url)
        try:
            await status.fail(UNEXPECTED_ERROR_TEXT)
        except TelegramError:
            logger.exception("Failed to tell the user about it either")


async def _deliver_post(message, url: str, context: ContextTypes.DEFAULT_TYPE, status: StatusMessage) -> None:
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    # Fast path: this post was already downloaded and uploaded to the
    # storage chat before (via inline mode or an earlier message), so we
    # can just resend the existing file_ids instead of downloading again.
    cached_result = cache.get_cached_inline_result(url)
    if cached_result:
        try:
            await delivery.send_prepared_result(message, cached_result, url)
        except TelegramError as error:
            if not delivery.is_dead_file_id_error(error):
                # Not the files - the chat, or the network. Sending again
                # would not help, and after a timeout the post may well have
                # arrived already, so it would arrive twice.
                logger.exception("Failed to send cached media for %s", url)
                await status.fail(SEND_FAILED_TEXT)
                return

            # The files are gone for this bot. Forget them, so that the
            # preparation below downloads the post again instead of handing
            # back the same file_ids from the cache.
            logger.warning("Telegram no longer accepts the cached files of %s (%s), preparing it again", url, error)
            cache.forget_cached_inline_result(url)
        else:
            await status.done()
            return

    if config.STORAGE_CHAT_ID:
        # Prepare (download + upload to storage) via the same deduplicated
        # task inline queries use, so concurrent requests for the same URL
        # - from any chat - share one download/encode instead of each
        # running their own. Each of them follows its progress.
        task = preparation.get_or_create_prepare_task(url, context)
        status.follow(preparation.progress_of(task))
        try:
            cached_result = await task
        except instagram.NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status.fail("В этой публикации нет ни видео, ни фото, которые я могу скачать.")
            return
        except instagram.IncompletePostError:
            logger.exception("Could not fetch all of %s", url)
            await status.fail(INCOMPLETE_POST_TEXT)
            return
        except media.MediaTooLargeError as error:
            logger.exception("Media too large for %s", url)
            await status.fail(too_large_text(error))
            return
        except TelegramError:
            # The one thing in the preparation that talks to Telegram is the
            # upload to the storage chat, so the post itself came through.
            # Blaming it - private, deleted - would send the user looking in
            # the wrong place; the storage chat is what to check.
            logger.exception("Failed to upload %s to the storage chat", url)
            await status.fail(UPLOAD_FAILED_TEXT)
            return
        except Exception:
            logger.exception("Failed to prepare %s", url)
            await status.fail(
                "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
                "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
            )
            return

        try:
            await delivery.send_prepared_result(message, cached_result, url)
        except TelegramError:
            logger.exception("Failed to deliver prepared media for %s", url)
            await status.fail(
                "Файлы подготовлены, но Telegram не смог их отправить в этот чат. Попробуй отправить ссылку еще раз."
            )
            return

        await status.done()
        return

    # Legacy path for setups without STORAGE_CHAT_ID: download and send
    # straight to this chat, without the shared cache/dedup above. Its
    # progress is reported from this handler's own task.
    tracker = progress.Progress(asyncio.get_running_loop())
    status.follow(tracker)
    token = progress.CURRENT.set(tracker)
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_post_"))
    try:
        try:
            items, caption = await preparation.download_post_in_thread(url, temp_dir, context)
        except instagram.NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status.fail("В этой публикации нет ни видео, ни фото, которые я могу скачать.")
            return
        except instagram.IncompletePostError:
            logger.exception("Could not fetch all of %s", url)
            await status.fail(INCOMPLETE_POST_TEXT)
            return
        except Exception:
            logger.exception("Failed to download %s", url)
            await status.fail(
                "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
                "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
            )
            return

        items, compressed = await preparation.prepare_items_in_thread(items, temp_dir)
        caption = media.add_compression_note_if_needed(caption, compressed)

        try:
            media.ensure_items_fit_telegram(items)
        except media.MediaTooLargeError as error:
            await status.fail(too_large_text(error))
            return

        tracker.report(progress.UPLOADING)
        try:
            await delivery.send_local_media_items(message, items, caption)
        except TimedOut:
            logger.exception("Telegram timed out while uploading the post")
            await status.fail(
                "Файлы скачаны, но Telegram слишком долго отвечал при отправке. Проверь чат: иногда они приходят позже. "
                "Если не пришли, попробуй еще раз или увеличь UPLOAD_TIMEOUT_SECONDS."
            )
            return
        except NetworkError:
            logger.exception("Network error while uploading the post")
            await status.fail(
                "Файлы скачаны, но при отправке в Telegram был сетевой сбой. Попробуй отправить ссылку еще раз."
            )
            return
        except TelegramError:
            logger.exception("Telegram failed to upload the post")
            await status.fail(
                "Файлы скачаны, но Telegram не смог их отправить. Попробуй другую публикацию или отправь ссылку еще раз."
            )
            return
        await status.done()
    finally:
        progress.CURRENT.reset(token)
        shutil.rmtree(temp_dir, ignore_errors=True)
