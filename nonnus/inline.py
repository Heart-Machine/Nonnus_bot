"""Inline mode: the results offered for a link, the placeholder shown while a
post is prepared, and swapping it for the real media once chosen."""

import logging
import asyncio
import hashlib
from html import escape
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResult,
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputMessageContent,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from nonnus import config, links, instagram, cache, delivery, preparation, status_message


logger = logging.getLogger(__name__)


PLACEHOLDER_UPLOAD_LOCK = asyncio.Lock()


# A small placeholder photo (assets/inline_placeholder.jpg: a download
# icon plus "Готовлю видео..." caption text) shown as the inline result
# while a Reel is being downloaded, so the query doesn't have to be
# retyped once it's ready. Note: InlineQueryResultCachedPhoto's
# title/description fields are NOT rendered by any major Telegram client
# for photo-type inline results (confirmed via python-telegram-bot#2115
# and telegramdesktop/tdesktop#7310) - the only text/graphics that
# actually show up are whatever is baked into the image itself, which is
# why this is a drawn icon rather than relying on API metadata fields.
# See get_placeholder_photo_file_id() and handle_chosen_inline_result().
INLINE_PLACEHOLDER_IMAGE_PATH = config.BASE_DIR / "assets" / "inline_placeholder.jpg"


INLINE_PLACEHOLDER_IMAGE_BYTES = INLINE_PLACEHOLDER_IMAGE_PATH.read_bytes()


def inline_result_id(url: str) -> str:
    return hashlib.sha256(f"{cache.INLINE_CACHE_VERSION}:{links.normalize_post_url(url)}".encode("utf-8")).hexdigest()[:32]


def add_carousel_note_if_needed(caption: str, index: int, total: int) -> str:
    """An inline message carries exactly one medium, so when a single file of
    a carousel is sent that way, the caption spells out what else is in the
    post and how to get it."""
    if total <= 1:
        return caption

    note = (
        f"Файл {index + 1} из {total} в этой публикации. "
        "Повтори inline-запрос, чтобы отправить остальные."
    )
    return f"{caption}\n\n{escape(note)}"


def inline_item_result_id(url: str, index: int, total: int) -> str:
    """A single-file post keeps the bare per-URL id - the same one the
    placeholder uses - while a carousel appends the item's position, so every
    result in one answer stays distinct. The bare id is also how
    handle_chosen_inline_result tells the placeholder, which still needs its
    media swapped in, from a carousel result that is already final."""
    base_id = inline_result_id(url)
    return base_id if total == 1 else f"{base_id}-{index}"


CAROUSEL_BUTTON_TEXT = "Посмотреть карусель"


def carousel_keyboard(url: str, bot_username: str) -> Optional[InlineKeyboardMarkup]:
    """The button under an inline carousel message.

    An inline message carries a single medium, and Telegram never tells the
    bot which chat it was sent to - so the rest of the album cannot follow it
    there. The button opens a private chat with the bot instead, where the
    /start deep link delivers the whole post as an album."""
    payload = links.post_start_payload(url)
    if not payload or not bot_username:
        return None

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(CAROUSEL_BUTTON_TEXT, url=f"https://t.me/{bot_username}?start={payload}")]]
    )


CAROUSEL_SLIDESHOW_TITLE = "Вся карусель одним сообщением"


class InputRichMessageContent(InputMessageContent):
    """Bot API 10.1 InputRichMessageContent, which python-telegram-bot does
    not know about yet: it stops at Bot API 10.0, and the pull requests adding
    rich messages were closed unmerged.

    It rides on the library's own InlineQueryResultArticle and serialises to
    exactly {"rich_message": {...}}. answer_inline_query only reaches into the
    content of a result for parse_mode, which this has none of, so it passes
    through untouched. Replace it with the library's class once it has one."""

    __slots__ = ("rich_message",)

    def __init__(self, rich_message: dict[str, Any], *, api_kwargs: Optional[dict[str, Any]] = None) -> None:
        super().__init__(api_kwargs=api_kwargs)
        with self._unfrozen():
            self.rich_message = rich_message


def build_inline_results(url: str, cached_result: dict[str, Any], bot_username: str = "") -> list[InlineQueryResult]:
    """One inline result per file in the post. For a carousel the list opens
    with the whole post as a single slideshow message; the per-file results
    after it stay as the fallback for a client that renders rich messages
    poorly, each carrying a button to the whole album in a private chat with
    the bot."""
    items = cached_result.get("items") or []
    base_caption = cached_result.get("caption") or escape(links.normalize_post_url(url))
    title = cached_result.get("title") or "Instagram"
    total = len(items)
    reply_markup = carousel_keyboard(url, bot_username) if total > 1 else None

    results: list[InlineQueryResult] = []
    slideshow = delivery.carousel_slideshow_message(url, cached_result)
    if slideshow is not None:
        # No button: this one already is the whole carousel. Without a button
        # Telegram also sends no inline_message_id when it is chosen, so it
        # never reaches handle_chosen_inline_result.
        results.append(
            InlineQueryResultArticle(
                id=f"{inline_result_id(url)}-all",
                title=CAROUSEL_SLIDESHOW_TITLE,
                description=f"{title}, слайдов: {total}",
                input_message_content=InputRichMessageContent(slideshow),
            )
        )

    for index, item in enumerate(items):
        result_id = inline_item_result_id(url, index, total)
        item_title = title if total == 1 else f"{title} - {index + 1}/{total}"
        caption = add_carousel_note_if_needed(base_caption, index, total)
        file_id = item["file_id"]
        kind = item.get("type")

        if kind == "photo":
            results.append(
                InlineQueryResultCachedPhoto(
                    id=result_id,
                    photo_file_id=file_id,
                    title=item_title,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )
        elif kind == "document":
            results.append(
                InlineQueryResultCachedDocument(
                    id=result_id,
                    document_file_id=file_id,
                    title=item_title,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )
        else:
            results.append(
                InlineQueryResultCachedVideo(
                    id=result_id,
                    video_file_id=file_id,
                    title=item_title,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )

    return results


def build_inline_article(result_id: str, title: str, description: str, message_text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text),
    )


async def get_placeholder_photo_file_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Upload the "Готовлю видео..." placeholder image to the storage chat
    once per bot run and cache its file_id, so every not-yet-ready inline
    query can reuse it as InlineQueryResultCachedPhoto instead of re-sending
    the bytes each time."""
    file_id = context.application.bot_data.get("placeholder_photo_file_id")
    if file_id:
        return file_id

    if not config.STORAGE_CHAT_ID:
        return None

    # Inline queries arrive on every keystroke and are handled side by side,
    # so a burst of them could find no file_id and each upload the image.
    # The first one uploads; the rest wait and reuse its file_id.
    async with PLACEHOLDER_UPLOAD_LOCK:
        file_id = context.application.bot_data.get("placeholder_photo_file_id")
        if file_id:
            return file_id

        try:
            sent_message = await context.bot.send_photo(
                chat_id=delivery.parse_storage_chat_id(),
                photo=INLINE_PLACEHOLDER_IMAGE_BYTES,
            )
        except TelegramError:
            logger.exception("Failed to upload inline placeholder photo")
            return None

        if not sent_message.photo:
            return None

        file_id = sent_message.photo[-1].file_id
        context.application.bot_data["placeholder_photo_file_id"] = file_id
        return file_id


PLACEHOLDER_CAPTION = "Готовлю видео, подожди немного — сообщение обновится само..."


def placeholder_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Открыть в Instagram", url=links.normalize_post_url(url))]])


def build_inline_placeholder_result(url: str, photo_file_id: str) -> InlineQueryResultCachedPhoto:
    """A placeholder inline result shown while a Reel is being prepared. It
    carries a reply_markup so Telegram is guaranteed to report an
    inline_message_id in chosen_inline_result, which handle_chosen_inline_result
    then uses to swap this placeholder for the real video once it's ready -
    no need for the user to retype the query."""
    return InlineQueryResultCachedPhoto(
        id=inline_result_id(url),
        photo_file_id=photo_file_id,
        title="Готовлю видео...",
        description="Нажми, чтобы отправить — видео появится тут само через несколько секунд",
        caption=PLACEHOLDER_CAPTION,
        reply_markup=placeholder_keyboard(url),
    )


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return

    url = links.find_instagram_url(inline_query.query)
    if not url:
        await inline_query.answer(
            [
                build_inline_article(
                    "help",
                    "Пришли ссылку на Instagram",
                    "Напиши: @bot_username https://www.instagram.com/reel/...",
                    "Пришли ссылку на Reel или пост после имени бота.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    post_url = await preparation.resolve_link(url)
    if post_url is None:
        await inline_query.answer(
            [
                build_inline_article(
                    "share-unresolved",
                    "Не получилось открыть ссылку",
                    "Пришли обычную ссылку на пост",
                    "Не получилось понять, на какой пост ведёт эта ссылка. "
                    "Нужна обычная ссылка на пост - в Instagram это «Копировать ссылку».",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return
    url = post_url

    cached_result = cache.get_cached_inline_result(url)
    if cached_result:
        try:
            await inline_query.answer(build_inline_results(url, cached_result, await delivery.get_bot_username(context)), cache_time=0, is_personal=True)
            return
        except TelegramError as error:
            if not delivery.is_dead_file_id_error(error):
                raise
            # A refused answer does not use the query up, so it can still be
            # answered below - with the placeholder, while the post is
            # prepared again.
            logger.warning("Telegram no longer accepts the cached files of %s (%s), preparing it again", url, error)
            cache.forget_cached_inline_result(url)

    if not config.STORAGE_CHAT_ID:
        await inline_query.answer(
            [
                build_inline_article(
                    "setup-required",
                    "Нужно настроить STORAGE_CHAT_ID",
                    "Inline mode требует storage-чат для кэша файлов",
                    "Inline mode еще не настроен: добавь STORAGE_CHAT_ID в .env и перезапусти бота.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    task = preparation.get_or_create_prepare_task(url, context, reuse_failure=True)

    # A zero-cost check, not a wait: a task already done is one that failed
    # moments ago - kept on hand for that - or one that just finished, so
    # this query answers with its outcome right away. Otherwise - no
    # artificial delay - answer immediately with a self-updating
    # placeholder; handle_chosen_inline_result() swaps it for the real
    # video via editMessageMedia once the same task completes.
    if task.done():
        try:
            cached_result = task.result()
        except instagram.NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await inline_query.answer(
                [
                    build_inline_article(
                        inline_result_id(url),
                        "В посте нет медиа",
                        "Не нашлось ни видео, ни фото",
                        "В этой публикации нет ни видео, ни фото, которые я могу скачать.",
                    )
                ],
                cache_time=0,
                is_personal=True,
            )
            return
        except Exception as error:
            # The traceback is in the log once already, from the preparation;
            # this runs again on every keystroke of the query.
            logger.info("Answering the inline query for %s with its failed preparation: %s", url, preparation.describe_failure(error))
            await inline_query.answer(
                [
                    build_inline_article(
                        inline_result_id(url),
                        "Не получилось подготовить публикацию",
                        "Попробуй еще раз или отправь ссылку боту в личку",
                        "Не получилось подготовить файлы для inline-отправки.",
                    )
                ],
                cache_time=0,
                is_personal=True,
            )
            return

        await inline_query.answer(build_inline_results(url, cached_result, await delivery.get_bot_username(context)), cache_time=0, is_personal=True)
        return

    placeholder_photo_file_id = await get_placeholder_photo_file_id(context)
    if placeholder_photo_file_id:
        await inline_query.answer(
            [build_inline_placeholder_result(url, placeholder_photo_file_id)],
            cache_time=0,
            is_personal=True,
        )
    else:
        await inline_query.answer(
            [
                build_inline_article(
                    inline_result_id(url),
                    "Готовлю видео...",
                    "Через несколько секунд повтори inline-запрос",
                    "Видео готовится. Повтори inline-запрос через несколько секунд.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Once a user picks the placeholder result built by
    build_inline_placeholder_result(), swap it for the real media in place
    as soon as it's ready, using the same deduplicated prepare task the
    inline query itself started. Requires inline feedback collection to be
    enabled for the bot via @BotFather (/setinlinefeedback)."""
    chosen = update.chosen_inline_result
    if chosen is None or chosen.inline_message_id is None:
        return

    url = links.find_instagram_url(chosen.query)
    if not url:
        return
    # A share link was resolved for the query itself, so this is a lookup.
    url = await preparation.resolve_link(url)
    if url is None:
        return

    # Telegram reports an inline_message_id only for a result with a button.
    # That is the placeholder, which still needs its media swapped in - and
    # also, since they carry the carousel button, the per-file results of a
    # cached carousel, which are final already. Swapping one of those would
    # re-set the same file and strip the button the moment it was sent.
    if chosen.result_id != inline_result_id(url):
        return

    cached_result = cache.get_cached_inline_result(url)
    if cached_result is None:
        task = preparation.get_or_create_prepare_task(url, context, reuse_failure=True)
        # Meanwhile the placeholder's caption shows how the preparation goes,
        # as the status message under a link sent to the bot does.
        status = status_message.PlaceholderStatus(
            context.bot, chosen.inline_message_id, PLACEHOLDER_CAPTION, placeholder_keyboard(url)
        )
        status.follow(preparation.progress_of(task))
        try:
            cached_result = await task
        except Exception as error:
            logger.warning("Could not prepare %s for the chosen placeholder: %s", url, preparation.describe_failure(error))
            try:
                await status.fail("Не получилось подготовить публикацию. Попробуй еще раз.")
            except TelegramError:
                pass
            return
        # Before the swap: a progress edit landing after it would replace the
        # post's own caption.
        await status.settle()

    items = cached_result.get("items") or []
    if not items:
        return

    # A placeholder holds one medium, so a carousel resolves to its first
    # file here, with the button to the whole album underneath.
    item = items[0]
    caption = add_carousel_note_if_needed(cached_result.get("caption", ""), 0, len(items))
    media = delivery.build_input_media(item.get("type", "video"), item["file_id"], caption)
    reply_markup = carousel_keyboard(url, await delivery.get_bot_username(context)) if len(items) > 1 else None
    try:
        await context.bot.edit_message_media(
            inline_message_id=chosen.inline_message_id,
            media=media,
            reply_markup=reply_markup,
        )
    except TelegramError as error:
        logger.exception("Failed to swap placeholder for the prepared media (inline_message_id=%s)", chosen.inline_message_id)
        if delivery.is_dead_file_id_error(error):
            # This message stays the placeholder, but the next request for
            # the post prepares it again rather than failing the same way.
            cache.forget_cached_inline_result(url)
