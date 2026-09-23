"""A daily check that the bot can still download from Instagram.

Instagram changes its pages and endpoints often, and every change can break
yt-dlp. Without a check the owner finds out from users - or not at all, if
they just stop sending links. So once a day, and a few minutes after every
start (which is to say every deploy), the bot downloads a known public post,
CANARY_POST_URL, the whole way: probe, videos, photos, conversion - through
the same slots as everyone else, into a temporary directory, without the
cache and without uploading anything.

The owner hears about it in the storage chat when the check starts failing
and again when it passes after that - once per change, not every day of a
long outage.
"""
import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from telegram.error import TelegramError

from nonnus import config, links, delivery, preparation


logger = logging.getLogger(__name__)


# A check a few minutes after start doubles as a check of the deploy that
# started the bot; after that, once a day.
FIRST_CHECK_DELAY_SECONDS = 5 * 60
CHECK_INTERVAL_SECONDS = 24 * 60 * 60

# How much of the error goes into the alert: enough to recognise it, short
# enough to read on a phone.
ERROR_EXCERPT_LENGTH = 300


def failure_text(url: str, error: BaseException) -> str:
    excerpt = str(error).strip()[:ERROR_EXCERPT_LENGTH]
    return (
        f"Проверка бота не смогла скачать контрольный пост {url}\n\n"
        f"Ошибка: {type(error).__name__}: {excerpt}\n\n"
        "Чаще всего это значит, что Instagram что-то поменял и нужно обновить yt-dlp: "
        "смержи свежий PR от Dependabot с обновлением yt-dlp или подними версию в requirements.txt. "
        "Если сам пост удалён или стал закрытым - поменяй переменную CANARY_POST_URL.\n\n"
        "Следующее сообщение придёт, когда проверка снова пройдёт."
    )


RECOVERY_TEXT = "Контрольный пост снова скачивается - с загрузкой из Instagram всё в порядке."


async def check_once(bot: Any, url: str) -> Optional[BaseException]:
    """Download the post once; the error if that failed, None if it came
    through whole."""
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_canary_"))
    try:
        # download_post_in_thread only needs the bot from a context - to
        # send the cookie alert, which a check can trigger like any request.
        context = SimpleNamespace(bot=bot)
        items, _ = await preparation.download_post_in_thread(url, temp_dir, context)
        await preparation.prepare_items_in_thread(items, temp_dir)
        return None
    except Exception as error:
        return error
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def tell_owner(bot: Any, text: str) -> None:
    if not config.STORAGE_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=delivery.parse_storage_chat_id(), text=text, disable_web_page_preview=True)
    except TelegramError:
        logger.exception("Failed to send the canary result to the storage chat")


async def run(bot: Any, url: str) -> None:
    """Check forever, telling the owner when the outcome changes."""
    failing = False
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        error = await check_once(bot, url)
        if error is None:
            logger.info("Canary: %s downloaded fine", url)
            if failing:
                await tell_owner(bot, RECOVERY_TEXT)
            failing = False
        else:
            logger.error("Canary: could not download %s", url, exc_info=error)
            if not failing:
                await tell_owner(bot, failure_text(url, error))
            failing = True
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start(bot: Any) -> Optional[asyncio.Task]:
    """Start the checks if CANARY_POST_URL names a post.

    A plain asyncio task, not Application.create_task: the application waits
    for its own tasks when it stops, and this one never ends by itself - it
    would hold up every shutdown, and so every deploy. stop() cancels it."""
    if not config.CANARY_POST_URL:
        logger.info("Canary is off: CANARY_POST_URL is not set")
        return None

    url = links.find_instagram_url(config.CANARY_POST_URL)
    if not url:
        logger.error("Canary is off: CANARY_POST_URL is not a link to an Instagram post")
        return None

    logger.info("Canary on: checking %s every %d h", url, CHECK_INTERVAL_SECONDS // 3600)
    return asyncio.get_running_loop().create_task(run(bot, url), name="canary")


async def stop(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.cancel()
    # gather hands the check's own CancelledError back as a result rather
    # than raising it - while a cancellation of stop() itself still goes
    # through, which suppressing CancelledError around `await task` would
    # swallow.
    await asyncio.gather(task, return_exceptions=True)
