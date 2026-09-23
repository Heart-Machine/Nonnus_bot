"""Sending posts to Telegram: uploading to the storage chat, and sending a
prepared post back - as a slideshow, an album or a single file."""

import logging
from html import unescape
import re
from contextlib import ExitStack
from typing import Any, Iterator, Optional

from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from nonnus import config, links, media


logger = logging.getLogger(__name__)


# Telegram accepts at most 10 items in a single album.
MEDIA_GROUP_LIMIT = 10


UPLOAD_TIMEOUTS: dict[str, Any] = {
    "read_timeout": config.UPLOAD_TIMEOUT_SECONDS,
    "write_timeout": config.UPLOAD_TIMEOUT_SECONDS,
    "connect_timeout": 30,
    "pool_timeout": 30,
}


def parse_storage_chat_id() -> int | str:
    if not config.STORAGE_CHAT_ID:
        raise RuntimeError("Set STORAGE_CHAT_ID to use inline mode")

    if re.fullmatch(r"-?\d+", config.STORAGE_CHAT_ID):
        return int(config.STORAGE_CHAT_ID)

    return config.STORAGE_CHAT_ID


def title_from_caption(caption: str) -> str:
    match = re.search(r">([^<>]+)</a>", caption)
    if match:
        return match.group(1)

    return "Instagram"


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def build_input_media(kind: str, media: Any, caption: Optional[str]) -> InputMediaPhoto | InputMediaDocument | InputMediaVideo:
    if kind == "photo":
        return InputMediaPhoto(media=media, caption=caption, parse_mode=ParseMode.HTML)

    if kind == "document":
        return InputMediaDocument(media=media, caption=caption, parse_mode=ParseMode.HTML)

    return InputMediaVideo(
        media=media,
        caption=caption,
        parse_mode=ParseMode.HTML,
        supports_streaming=True,
    )


# Telegram's limit on media in a single rich message. An Instagram carousel
# tops out at 20, so this only matters if that ever changes.
RICH_MESSAGE_MEDIA_LIMIT = 50


def carousel_slideshow_message(url: str, cached_result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The whole carousel as one rich message: a slideshow of every file in
    carousel order, with the author link as its caption.

    This is what gets a carousel into a chat through inline mode in one piece.
    An inline message cannot be an album, but it can be a rich message, and
    Telegram only allows previously uploaded files in one - which every item
    here already is, by its file_id in the storage chat.

    Only photos and videos can be slides. A file Telegram refused as media and
    took as a document cannot, so a carousel holding one is not offered as a
    slideshow at all rather than shown with a file missing."""
    items = cached_result.get("items") or []
    if len(items) < 2 or len(items) > RICH_MESSAGE_MEDIA_LIMIT:
        return None

    slides = []
    for item in items:
        kind = item.get("type")
        if kind not in ("photo", "video"):
            return None
        slides.append({"type": kind, kind: {"type": kind, "media": item["file_id"]}})

    post_url = links.normalize_post_url(url)
    author = cached_result.get("title")
    link_text = author if author and author != "Instagram" else post_url
    blocks: list[dict[str, Any]] = [
        {
            "type": "slideshow",
            "blocks": slides,
            # A slideshow is always a carousel, so always a post.
            "caption": {"text": ["Пост ", {"type": "url", "text": link_text, "url": post_url}]},
        }
    ]

    # Notes the caption picked up after the author link - that a video was
    # compressed, say - were escaped for Telegram's HTML parse mode. A rich
    # message block takes plain text, so they go back to it, one paragraph each.
    for note in (cached_result.get("caption") or "").split("\n\n")[1:]:
        blocks.append({"type": "paragraph", "text": unescape(note)})

    return {"blocks": blocks}


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    cached_username = context.application.bot_data.get("bot_username")
    if cached_username:
        return str(cached_username)

    bot_user = await context.bot.get_me()
    username = bot_user.username or ""
    context.application.bot_data["bot_username"] = username
    return username


def file_reference_from_message(message) -> dict[str, str]:
    if message.video is not None:
        return {"type": "video", "file_id": message.video.file_id}

    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}

    if message.document is not None:
        return {"type": "document", "file_id": message.document.file_id}

    raise RuntimeError("Telegram did not return a file_id for the uploaded media")


async def upload_item_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    storage_chat_id: int | str,
    item: media.MediaItem,
    caption: Optional[str],
) -> dict[str, str]:
    try:
        with item.path.open("rb") as media_file:
            if item.is_video:
                sent_message = await context.bot.send_video(
                    chat_id=storage_chat_id,
                    video=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    **media.video_send_hints(item.path),
                    **UPLOAD_TIMEOUTS,
                )
            else:
                sent_message = await context.bot.send_photo(
                    chat_id=storage_chat_id,
                    photo=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    **UPLOAD_TIMEOUTS,
                )

        return file_reference_from_message(sent_message)
    except BadRequest:
        logger.exception("Telegram refused the storage %s upload, sending as document", item.kind)
        with item.path.open("rb") as media_file:
            sent_message = await context.bot.send_document(
                chat_id=storage_chat_id,
                document=media_file,
                caption=caption,
                parse_mode=ParseMode.HTML,
                **UPLOAD_TIMEOUTS,
            )

        return file_reference_from_message(sent_message)


async def upload_items_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    items: list[media.MediaItem],
    caption: str,
) -> list[dict[str, str]]:
    """Park every file of the post in the storage chat and keep the file_ids,
    so later requests for the same post - inline or direct - can be answered
    without downloading or uploading the bytes again."""
    storage_chat_id = parse_storage_chat_id()
    if len(items) == 1:
        return [await upload_item_to_storage(context, storage_chat_id, items[0], caption)]

    uploaded: list[dict[str, str]] = []
    for chunk_index, chunk in enumerate(chunked(items, MEDIA_GROUP_LIMIT)):
        sent_messages = None
        with ExitStack() as stack:
            media_group = [
                build_input_media(
                    item.kind,
                    stack.enter_context(item.path.open("rb")),
                    caption if chunk_index == 0 and position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ]
            try:
                sent_messages = await context.bot.send_media_group(
                    chat_id=storage_chat_id,
                    media=media_group,
                    **UPLOAD_TIMEOUTS,
                )
            except BadRequest:
                logger.exception("Telegram refused the storage album, uploading its items one by one")

        if sent_messages is None:
            for item in chunk:
                uploaded.append(
                    await upload_item_to_storage(
                        context,
                        storage_chat_id,
                        item,
                        caption if not uploaded else None,
                    )
                )
        else:
            uploaded.extend(file_reference_from_message(sent_message) for sent_message in sent_messages)

    return uploaded


async def send_rich_message(message, rich_message: dict[str, Any]) -> None:
    """sendRichMessage, which python-telegram-bot has no method for yet, sent
    the way Message.reply_* sends everything else: quoting the message it
    answers outside a private chat, and into the same forum topic. The library
    JSON-encodes a dict parameter on its own, so rich_message goes in as is."""
    api_kwargs: dict[str, Any] = {"chat_id": message.chat_id, "rich_message": rich_message}
    if message.chat.type != "private":
        api_kwargs["reply_parameters"] = {"message_id": message.message_id}
    if message.is_topic_message and message.message_thread_id:
        api_kwargs["message_thread_id"] = message.message_thread_id

    await message.get_bot().do_api_request("sendRichMessage", api_kwargs=api_kwargs, **UPLOAD_TIMEOUTS)


async def send_prepared_result(message, cached_result: dict[str, Any], url: str) -> None:
    """Send an already-uploaded (storage-chat) post by file_id, without
    re-downloading or re-uploading the bytes.

    A carousel goes out as a single slideshow message - one message however
    long the post, against one album per ten files. If Telegram turns the
    slideshow down it falls back to albums, as does a carousel that cannot be
    a slideshow at all: one holding a file Telegram only took as a document."""
    items = cached_result.get("items") or []
    if not items:
        raise RuntimeError("Prepared result has no media")

    slideshow = carousel_slideshow_message(url, cached_result) if len(items) > 1 else None
    if slideshow is not None:
        try:
            await send_rich_message(message, slideshow)
            return
        except BadRequest:
            logger.exception("Telegram refused the carousel slideshow for %s, sending albums instead", url)

    caption = cached_result.get("caption", "")
    if len(items) == 1:
        item = items[0]
        if item.get("type") == "document":
            await message.reply_document(
                document=item["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                **UPLOAD_TIMEOUTS,
            )
        elif item.get("type") == "photo":
            await message.reply_photo(
                photo=item["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                **UPLOAD_TIMEOUTS,
            )
        else:
            await message.reply_video(
                video=item["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                **UPLOAD_TIMEOUTS,
            )
        return

    for chunk_index, chunk in enumerate(chunked(items, MEDIA_GROUP_LIMIT)):
        await message.reply_media_group(
            media=[
                build_input_media(
                    item.get("type", "video"),
                    item["file_id"],
                    caption if chunk_index == 0 and position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ],
            **UPLOAD_TIMEOUTS,
        )


async def send_local_media_items(message, items: list[media.MediaItem], caption: str) -> None:
    """Send freshly downloaded files straight from disk - the path taken when
    no storage chat is configured, so there are no file_ids to reuse."""
    if len(items) == 1:
        item = items[0]
        try:
            with item.path.open("rb") as media_file:
                if item.is_video:
                    await message.reply_video(
                        video=media_file,
                        filename=item.path.name,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        **media.video_send_hints(item.path),
                        **UPLOAD_TIMEOUTS,
                    )
                else:
                    await message.reply_photo(
                        photo=media_file,
                        filename=item.path.name,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        **UPLOAD_TIMEOUTS,
                    )
        except BadRequest:
            logger.exception("Telegram refused the %s format, sending as document", item.kind)
            with item.path.open("rb") as media_file:
                await message.reply_document(
                    document=media_file,
                    filename=item.path.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    **UPLOAD_TIMEOUTS,
                )
        return

    for chunk_index, chunk in enumerate(chunked(items, MEDIA_GROUP_LIMIT)):
        with ExitStack() as stack:
            media_group = [
                build_input_media(
                    item.kind,
                    stack.enter_context(item.path.open("rb")),
                    caption if chunk_index == 0 and position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ]
            await message.reply_media_group(media=media_group, **UPLOAD_TIMEOUTS)
