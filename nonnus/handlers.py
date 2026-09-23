"""Handlers for commands and for links sent to the bot directly."""

import logging
import re
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from nonnus import config, links, media, instagram, cache, delivery, preparation


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

    await deliver_post(message, url, context)


async def deliver_post(message, url: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the post behind `url` to the chat `message` came from - as an
    album when it is a carousel - from the file_id cache when it is there and
    downloading it otherwise. Shared by links sent to the bot and by the
    /start deep link behind the inline carousel button."""
    status_message = await message.reply_text("Скачиваю публикацию...")
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
                await status_message.edit_text(SEND_FAILED_TEXT)
                return

            # The files are gone for this bot. Forget them, so that the
            # preparation below downloads the post again instead of handing
            # back the same file_ids from the cache.
            logger.warning("Telegram no longer accepts the cached files of %s (%s), preparing it again", url, error)
            cache.forget_cached_inline_result(url)
        else:
            await status_message.delete()
            return

    if config.STORAGE_CHAT_ID:
        # Prepare (download + upload to storage) via the same deduplicated
        # task inline queries use, so concurrent requests for the same URL
        # - from any chat - share one download/encode instead of each
        # running their own.
        task = preparation.get_or_create_prepare_task(url, context)
        try:
            cached_result = await task
        except instagram.NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status_message.edit_text(
                "В этой публикации нет ни видео, ни фото, которые я могу скачать."
            )
            return
        except instagram.IncompletePostError:
            logger.exception("Could not fetch all of %s", url)
            await status_message.edit_text(INCOMPLETE_POST_TEXT)
            return
        except media.MediaTooLargeError as error:
            logger.exception("Media too large for %s", url)
            await status_message.edit_text(too_large_text(error))
            return
        except TelegramError:
            # The one thing in the preparation that talks to Telegram is the
            # upload to the storage chat, so the post itself came through.
            # Blaming it - private, deleted - would send the user looking in
            # the wrong place; the storage chat is what to check.
            logger.exception("Failed to upload %s to the storage chat", url)
            await status_message.edit_text(UPLOAD_FAILED_TEXT)
            return
        except Exception:
            logger.exception("Failed to prepare %s", url)
            await status_message.edit_text(
                "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
                "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
            )
            return

        try:
            await delivery.send_prepared_result(message, cached_result, url)
        except TelegramError:
            logger.exception("Failed to deliver prepared media for %s", url)
            await status_message.edit_text(
                "Файлы подготовлены, но Telegram не смог их отправить в этот чат. Попробуй отправить ссылку еще раз."
            )
            return

        await status_message.delete()
        return

    # Legacy path for setups without STORAGE_CHAT_ID: download and send
    # straight to this chat, without the shared cache/dedup above.
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_post_"))
    try:
        try:
            items, caption = await preparation.download_post_in_thread(url, temp_dir, context)
        except instagram.NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status_message.edit_text(
                "В этой публикации нет ни видео, ни фото, которые я могу скачать."
            )
            return
        except instagram.IncompletePostError:
            logger.exception("Could not fetch all of %s", url)
            await status_message.edit_text(INCOMPLETE_POST_TEXT)
            return
        except Exception:
            logger.exception("Failed to download %s", url)
            await status_message.edit_text(
                "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
                "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
            )
            return

        oversized_video = any(
            item.is_video and item.path.stat().st_size > config.MAX_FILE_SIZE_BYTES for item in items
        )
        if oversized_video and config.ENABLE_VIDEO_COMPRESSION:
            await status_message.edit_text("Видео большое, сжимаю перед отправкой...")

        items, compressed = await preparation.prepare_items_in_thread(items, temp_dir)
        caption = media.add_compression_note_if_needed(caption, compressed)

        try:
            media.ensure_items_fit_telegram(items)
        except media.MediaTooLargeError as error:
            await status_message.edit_text(too_large_text(error))
            return

        try:
            await delivery.send_local_media_items(message, items, caption)
        except TimedOut:
            logger.exception("Telegram timed out while uploading the post")
            await status_message.edit_text(
                "Файлы скачаны, но Telegram слишком долго отвечал при отправке. Проверь чат: иногда они приходят позже. "
                "Если не пришли, попробуй еще раз или увеличь UPLOAD_TIMEOUT_SECONDS."
            )
            return
        except NetworkError:
            logger.exception("Network error while uploading the post")
            await status_message.edit_text(
                "Файлы скачаны, но при отправке в Telegram был сетевой сбой. Попробуй отправить ссылку еще раз."
            )
            return
        except TelegramError:
            logger.exception("Telegram failed to upload the post")
            await status_message.edit_text(
                "Файлы скачаны, но Telegram не смог их отправить. Попробуй другую публикацию или отправь ссылку еще раз."
            )
            return
        await status_message.delete()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
