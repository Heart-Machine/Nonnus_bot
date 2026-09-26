"""Handlers for commands and for links sent to the bot directly."""

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from nonnus import config, links, media, instagram, cache, delivery, preparation, progress, status_message, users


logger = logging.getLogger(__name__)


SEND_FAILED_TEXT = (
    "Не получилось отправить публикацию в этот чат. Если она не пришла, пришли ссылку ещё раз."
)


UNEXPECTED_ERROR_TEXT = "Что-то пошло не так. Попробуй ещё раз чуть позже."


SHARE_LINK_UNRESOLVED_TEXT = (
    "Не получилось понять, на какой пост ведёт эта ссылка: Instagram не ответил. "
    "Пришли обычную ссылку на пост - в Instagram это «Копировать ссылку»."
)


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

    users.remember(message.from_user)

    # The button under an inline carousel opens t.me/<bot>?start=<payload>,
    # which arrives here as /start <payload>.
    if context.args:
        url = links.post_url_from_start_payload(context.args[0])
        if url:
            await deliver_post(message, url, context)
            return

    text = (
        "Пришли ссылку на Instagram — Reel, пост с фото или карусель, сторис или хайлайт, "
        "а я отправлю всё содержимое сюда."
    )
    note = users.limit_note(message.from_user)
    await message.reply_text(f"{text}\n\n{note}" if note else text)


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

    users.remember(message.from_user)
    url = links.find_instagram_url(message.text)
    if not url:
        await message.reply_text(
            "Не вижу ссылку на Instagram. Пришли ссылку на Reel, пост, сторис или хайлайт в формате: "
            "instagram.com / reel / CODE, instagram.com / p / CODE или instagram.com / stories / ...",
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
    status = await status_message.ReplyStatus.send(message)
    try:
        await _deliver_post(message, url, context, status)
    except Exception:
        logger.exception("Unexpected failure delivering %s", url)
        try:
            await status.fail(UNEXPECTED_ERROR_TEXT)
        except TelegramError:
            logger.exception("Failed to tell the user about it either")


async def _deliver_post(message, url: str, context: ContextTypes.DEFAULT_TYPE, status: status_message.ReplyStatus) -> None:
    refusal = users.refusal_for(message.from_user, url)
    if refusal:
        await status.fail(refusal)
        return

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
        try:
            task = preparation.get_or_create_prepare_task(url, context, user=message.from_user)
        except users.DailyLimitReached as error:
            await status.fail(users.limit_reached_text(error))
            return
        status.follow(preparation.progress_of(task))
        # A failed preparation's traceback is logged once, from the task
        # itself; here, a line on what the user was told.
        try:
            cached_result = await task
        except instagram.NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status.fail(status_message.no_media_text(url))
            return
        except instagram.IncompletePostError as error:
            logger.warning("Could not fetch all of %s: %s", url, preparation.describe_failure(error))
            await status.fail(status_message.INCOMPLETE_POST_TEXT)
            return
        except media.MediaTooLargeError as error:
            logger.warning("Media too large for %s: %s", url, preparation.describe_failure(error))
            await status.fail(status_message.too_large_text(error))
            return
        except TelegramError as error:
            # The one thing in the preparation that talks to Telegram is the
            # upload to the storage chat, so the post itself came through.
            # Blaming it - private, deleted - would send the user looking in
            # the wrong place; the storage chat is what to check.
            logger.warning("Failed to upload %s to the storage chat: %s", url, preparation.describe_failure(error))
            await status.fail(status_message.UPLOAD_FAILED_TEXT)
            return
        except Exception as error:
            logger.warning("Failed to prepare %s: %s", url, preparation.describe_failure(error))
            await status.fail(status_message.download_failed_text(url))
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
    try:
        download = users.take_download(message.from_user)
    except users.DailyLimitReached as error:
        await status.fail(users.limit_reached_text(error))
        return
    tracker = progress.Progress(asyncio.get_running_loop())
    status.follow(tracker)
    token = progress.CURRENT.set(tracker)
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_post_"))
    try:
        try:
            items, caption = await preparation.download_post_in_thread(url, temp_dir, context)
        # A post that could not be downloaded does not use up the limit.
        except instagram.NoMediaInPostError:
            users.give_back_download(download)
            logger.info("No downloadable media in post %s", url)
            await status.fail(status_message.no_media_text(url))
            return
        except instagram.IncompletePostError:
            users.give_back_download(download)
            logger.exception("Could not fetch all of %s", url)
            await status.fail(status_message.INCOMPLETE_POST_TEXT)
            return
        except Exception:
            users.give_back_download(download)
            logger.exception("Failed to download %s", url)
            await status.fail(status_message.download_failed_text(url))
            return

        items, compressed = await preparation.prepare_items_in_thread(items, temp_dir)
        caption = media.add_compression_note_if_needed(caption, compressed)

        try:
            media.ensure_items_fit_telegram(items)
        except media.MediaTooLargeError as error:
            await status.fail(status_message.too_large_text(error))
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
