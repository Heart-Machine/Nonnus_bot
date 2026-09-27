"""Which Bot API server the bot talks to - Telegram's cloud one at
api.telegram.org, or a server of its own (config.TELEGRAM_API_URL) - and the
move from one to the other."""

from datetime import datetime, timezone
import logging
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from nonnus import config


logger = logging.getLogger(__name__)


def base_urls() -> Optional[tuple[str, str]]:
    """base_url and base_file_url for the bot's own server; None for the
    cloud one, which python-telegram-bot talks to by default."""
    if not config.TELEGRAM_API_URL:
        return None
    return f"{config.TELEGRAM_API_URL}/bot", f"{config.TELEGRAM_API_URL}/file/bot"


async def settle_on_server(token: str) -> None:
    """Log the bot out of the cloud Bot API the first time it runs on its own
    server, and forget that it did once it is back on the cloud.

    Telegram asks for the logout before a bot is run on a server of its own:
    without it there is no guarantee the bot gets its updates. It is done
    once - the marker file says so - since it is a move, not a routine: once
    logged out, the bot cannot log back in to the cloud for 10 minutes, and
    what the cloud answers a bot that has already left is not documented.
    A failed logout leaves no marker, so the next start tries again.

    Back on the cloud there is nothing to call - the bot logs in there by
    using it, 10 minutes after the logout at the earliest - but the marker
    goes, so that a later move to the bot's own server logs out again."""
    marker = config.CLOUD_LOGOUT_MARKER
    if not config.TELEGRAM_API_URL:
        if marker.exists():
            marker.unlink()
            logger.info("Back on the cloud Bot API; a later move to a server of the bot's own will log out again")
        return

    if marker.exists():
        return

    try:
        async with Bot(token) as cloud:
            await cloud.log_out()
    except TelegramError:
        logger.exception("Could not log the bot out of the cloud Bot API; the next start tries again")
        return

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    logger.info(
        "Logged the bot out of the cloud Bot API to run it on %s; it can go back no sooner than 10 minutes from now",
        config.TELEGRAM_API_URL,
    )
