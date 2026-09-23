"""Sending posts to Telegram: uploading to the storage chat, and sending a
prepared post back - as a slideshow, an album or a single file."""

import asyncio
import logging
from html import unescape
import re
from contextlib import ExitStack
from typing import Any, Callable, Optional

from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
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


# How Telegram words a refusal of the file_id itself, rather than of the
# chat or the message: a file_id belongs to the bot that uploaded the file, so
# a cache kept under another bot token is full of them, and one Telegram has
# dropped reads the same way. There is no error code for it, only the text.
DEAD_FILE_ID_MARKERS = (
    "wrong file identifier",
    "wrong remote file identifier",
    "file reference",
    "file_reference",
    "type of file mismatch",
    "media_empty",
)


def is_dead_file_id_error(error: TelegramError) -> bool:
    """Whether Telegram turned a send down over its file_ids - which a new
    upload fixes - and not over something a new upload would hit again, like
    missing rights in the chat."""
    message = error.message.lower()
    return isinstance(error, BadRequest) and any(marker in message for marker in DEAD_FILE_ID_MARKERS)


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


def album_chunks(items: list[Any]) -> list[list[Any]]:
    """Split files that may share an album into albums of 2 to
    MEDIA_GROUP_LIMIT, as even as they come.

    Telegram refuses an album of one, which cutting by ten leaves for an 11-
    or 21-file carousel - and a carousel holds up to 20 files now. Split
    evenly, 11 is 6 + 5 and 21 is 7 + 7 + 7, so only a lone file stays alone,
    to be sent as a message of its own."""
    if not items:
        return []

    count = -(-len(items) // MEDIA_GROUP_LIMIT)
    size, larger = divmod(len(items), count)
    chunks, start = [], 0
    for number in range(count):
        end = start + size + (1 if number < larger else 0)
        chunks.append(items[start:end])
        start = end

    return chunks


def album_groups(items: list[Any], kind: Callable[[Any], str]) -> list[list[Any]]:
    """The messages a post goes out as, in its own order: albums of 2 to 10
    files, and single files where one cannot share an album with the files
    around it.

    Telegram puts photos and videos in an album together, but a document only
    with other documents - so a file it would take only as a document splits
    the post around it, rather than failing the whole album."""
    runs: list[list[Any]] = []
    for item in items:
        is_document = kind(item) == "document"
        if runs and (kind(runs[-1][0]) == "document") == is_document:
            runs[-1].append(item)
        else:
            runs.append([item])

    return [chunk for run in runs for chunk in album_chunks(run)]


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


async def video_hints(item: media.MediaItem) -> dict[str, Any]:
    """media.video_send_hints for a video, run off the event loop: it starts
    ffprobe twice and waits on it, and every other update would wait too."""
    if not item.is_video:
        return {}

    return await asyncio.to_thread(media.video_send_hints, item.path)


async def upload_item_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    storage_chat_id: int | str,
    item: media.MediaItem,
    caption: Optional[str],
) -> dict[str, str]:
    hints = await video_hints(item)
    try:
        with item.path.open("rb") as media_file:
            if item.is_video:
                sent_message = await context.bot.send_video(
                    chat_id=storage_chat_id,
                    video=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    **hints,
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
    uploaded: list[dict[str, str]] = []
    for chunk in album_groups(items, lambda item: item.kind):
        if len(chunk) == 1:
            uploaded.append(
                await upload_item_to_storage(context, storage_chat_id, chunk[0], caption if not uploaded else None)
            )
            continue

        sent_messages = None
        with ExitStack() as stack:
            media_group = [
                build_input_media(
                    item.kind,
                    stack.enter_context(item.path.open("rb")),
                    caption if not uploaded and position == 0 else None,
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
    a slideshow at all: one holding a file Telegram only took as a document,
    which then goes out on its own between the albums (see album_groups)."""
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
    for chunk_index, chunk in enumerate(album_groups(items, lambda item: item.get("type", "video"))):
        chunk_caption = caption if chunk_index == 0 else None
        if len(chunk) == 1:
            await send_cached_item(message, chunk[0], chunk_caption)
            continue

        await message.reply_media_group(
            media=[
                build_input_media(
                    item.get("type", "video"),
                    item["file_id"],
                    chunk_caption if position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ],
            **UPLOAD_TIMEOUTS,
        )


async def send_cached_item(message, item: dict[str, str], caption: Optional[str]) -> None:
    """One already-uploaded file as a message of its own, by its file_id."""
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


async def send_local_media_items(message, items: list[media.MediaItem], caption: str) -> None:
    """Send freshly downloaded files straight from disk - the path taken when
    no storage chat is configured, so there are no file_ids to reuse."""
    for chunk_index, chunk in enumerate(album_groups(items, lambda item: item.kind)):
        chunk_caption = caption if chunk_index == 0 else None
        if len(chunk) == 1:
            await send_local_item(message, chunk[0], chunk_caption)
            continue

        with ExitStack() as stack:
            media_group = [
                build_input_media(
                    item.kind,
                    stack.enter_context(item.path.open("rb")),
                    chunk_caption if position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ]
            await message.reply_media_group(media=media_group, **UPLOAD_TIMEOUTS)


async def send_local_item(message, item: media.MediaItem, caption: Optional[str]) -> None:
    """One file from disk as a message of its own - as a document if Telegram
    will not take it as a photo or video."""
    hints = await video_hints(item)
    try:
        with item.path.open("rb") as media_file:
            if item.is_video:
                await message.reply_video(
                    video=media_file,
                    filename=item.path.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    **hints,
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
