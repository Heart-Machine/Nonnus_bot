"""The message that shows someone waiting on a post how its preparation goes.

Two kinds: a message of its own under a link sent to the bot, and the caption
of the inline placeholder that is swapped for the post once it is ready. Both
show the same stages and follow the same rules: edits thinned out, never
waited on by the preparation, and none arriving after the message has been
settled.
"""
import asyncio
import logging
import time
from typing import Any, Optional

from telegram.error import TelegramError

from nonnus import instagram, links, media, progress


logger = logging.getLogger(__name__)


DOWNLOADING_TEXT = "Скачиваю публикацию..."


# What is being downloaded, as it goes after "Скачиваю". "Публикация" until
# the post has been looked at and its kind is known.
KIND_NOUNS = {
    progress.REEL: "рилс",
    progress.PHOTO: "фото",
    progress.CAROUSEL: "карусель",
    progress.STORY: "сторис",
    progress.HIGHLIGHT: "хайлайт",
}


def progress_text(stage: str, done: int = 0, total: int = 0, kind: Optional[str] = None) -> str:
    if stage == progress.QUEUED:
        return "Жду своей очереди: сейчас скачиваются другие публикации..."
    if stage == progress.COMPRESSING:
        return "Видео большое, сжимаю перед отправкой..."
    if stage == progress.UPLOADING:
        return "Загружаю в Telegram..."
    noun = KIND_NOUNS.get(kind, "публикацию")
    if total > 1:
        return f"Скачиваю {noun}: {done} из {total}..."
    return f"Скачиваю {noun}..."


INCOMPLETE_POST_TEXT = (
    "Не получилось скачать публикацию целиком: часть файлов не загрузилась. "
    "Попробуй ещё раз чуть позже."
)


UPLOAD_FAILED_TEXT = (
    "Публикация скачалась, но загрузить её в Telegram не получилось. Попробуй ещё раз чуть позже."
)


def no_media_text(url: str) -> str:
    """What to say when Instagram gave nothing to download. For a story that
    mostly means it is gone: they last a day."""
    kind = links.story_kind(url)
    if kind == links.STORY:
        return "Этой сторис больше нет: сторис живут сутки."
    if kind == links.STORIES:
        return "Сейчас у этого пользователя нет сторис."
    if kind == links.HIGHLIGHT:
        return "В этом хайлайте нет ни видео, ни фото, которые я могу скачать."
    return "В этой публикации нет ни видео, ни фото, которые я могу скачать."


def download_failed_text(url: str) -> str:
    kind = links.story_kind(url)
    if kind == links.STORY:
        return "Не получилось скачать сторис. Возможно, она уже исчезла - сторис живут сутки - или аккаунт закрытый."
    if kind == links.STORIES:
        return "Не получилось скачать сторис. Возможно, сейчас их нет или аккаунт закрытый."
    if kind == links.HIGHLIGHT:
        return "Не получилось скачать хайлайт. Возможно, его удалили или аккаунт закрытый."
    return (
        "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
        "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
    )


def too_large_text(error: media.MediaTooLargeError) -> str:
    """Name the limit that was actually crossed: a photo's is five times
    lower than a video's, and "larger than 50 MB" about a 12 MB photo would be
    plainly wrong."""
    what = "видео" if error.kind == "video" else "фото"
    return f"Публикация скачалась, но {what} в ней больше {error.limit_mb} МБ, а Telegram такие не принимает."


def failure_text(url: str, error: BaseException) -> str:
    """What a status ends with when getting `url` ready failed with `error`:
    the same words under a link sent to the bot and on an inline placeholder.

    The one thing in a preparation that talks to Telegram is the upload to the
    storage chat, so a TelegramError means the post itself came through -
    blaming it, private or deleted, would send the user looking in the wrong
    place."""
    if isinstance(error, instagram.NoMediaInPostError):
        return no_media_text(url)
    if isinstance(error, instagram.IncompletePostError):
        return INCOMPLETE_POST_TEXT
    if isinstance(error, media.MediaTooLargeError):
        return too_large_text(error)
    if isinstance(error, TelegramError):
        return UPLOAD_FAILED_TEXT
    return download_failed_text(url)


# The least time between two edits of a status message. Telegram limits how
# often a message can be edited, and a count ticking up every half second is
# no more use than one every two.
STATUS_EDIT_INTERVAL_SECONDS = 2.0


class StatusMessage:
    """A text kept up to date while the post is prepared, then settled once.

    Progress edits are thinned out to one per STATUS_EDIT_INTERVAL_SECONDS,
    latest text winning, and run in the background so that the preparation
    never waits on one. Settling cancels whatever edit is still pending, so a
    late "3 из 10" cannot overwrite what comes after it - the error, or the
    post itself.

    Subclasses say how the text is edited."""

    def __init__(self, text: str) -> None:
        self._shown = text
        self._wanted = text
        self._last_edit = time.monotonic()
        self._pending: Optional[asyncio.Task] = None
        self._settled = False

    async def _edit(self, text: str) -> None:
        raise NotImplementedError

    def show(self, text: str) -> None:
        if self._settled:
            return
        self._wanted = text
        if self._pending is None or self._pending.done():
            self._pending = asyncio.get_running_loop().create_task(self._edit_when_due())

    def follow(self, tracker: Optional[progress.Progress]) -> None:
        if tracker is not None:
            tracker.subscribe(lambda stage, done, total, kind: self.show(progress_text(stage, done, total, kind)))

    async def _edit_when_due(self) -> None:
        delay = self._last_edit + STATUS_EDIT_INTERVAL_SECONDS - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._settled or self._wanted == self._shown:
            return

        text = self._wanted
        try:
            await self._edit(text)
            self._shown = text
        except TelegramError:
            # A progress line is not worth failing over; the final edit is.
            logger.warning("Failed to update a status message", exc_info=True)
        finally:
            self._last_edit = time.monotonic()

    async def settle(self) -> None:
        """Stop the progress edits, the pending one included. Whatever is done
        to the message next is the last word on it."""
        self._settled = True
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            # gather returns the edit's own CancelledError instead of
            # raising it, while a cancellation of this handler still goes
            # through - suppressing CancelledError here would swallow that.
            await asyncio.gather(self._pending, return_exceptions=True)

    async def fail(self, text: str) -> None:
        await self.settle()
        await self._edit(text)


class ReplyStatus(StatusMessage):
    """The "Скачиваю публикацию..." message under a link: deleted when the post
    has gone out, or turned into what went wrong."""

    def __init__(self, message, text: str) -> None:
        super().__init__(text)
        self._message = message

    @classmethod
    async def send(cls, reply_to, text: str = DOWNLOADING_TEXT) -> "ReplyStatus":
        return cls(await reply_to.reply_text(text), text)

    async def _edit(self, text: str) -> None:
        await self._message.edit_text(text)

    async def done(self) -> None:
        await self.settle()
        await self._message.delete()


class PlaceholderStatus(StatusMessage):
    """The caption of an inline placeholder someone has sent, while the post
    behind it is prepared. Settled before the placeholder is swapped for the
    post, which brings its own caption.

    Every edit carries the placeholder's keyboard again: an edit without
    reply_markup takes the keyboard off the message."""

    def __init__(self, bot, inline_message_id: str, text: str, reply_markup: Any) -> None:
        super().__init__(text)
        self._bot = bot
        self._inline_message_id = inline_message_id
        self._reply_markup = reply_markup

    async def _edit(self, text: str) -> None:
        await self._bot.edit_message_caption(
            inline_message_id=self._inline_message_id,
            caption=text,
            reply_markup=self._reply_markup,
        )
