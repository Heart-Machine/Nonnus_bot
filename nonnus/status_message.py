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

from nonnus import progress


logger = logging.getLogger(__name__)


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
            tracker.subscribe(lambda stage, done, total: self.show(progress_text(stage, done, total)))

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
