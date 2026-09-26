"""Wiring the handlers into the application."""

import logging
import re
from typing import Any

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from nonnus import config, inline, handlers, admin, canary, menu


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# A Telegram bot token: the bot's numeric id, a colon, and 35 characters of
# secret. It travels in the URL of every Bot API request - /bot<token>/method -
# so anything that logs a URL logs the token with it.
BOT_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
REDACTED_TOKEN = "<BOT_TOKEN>"


def redact(text: str) -> str:
    text = BOT_TOKEN_RE.sub(REDACTED_TOKEN, text)
    # The configured token too, whatever its shape, in case one ever does not
    # match the pattern.
    if config.BOT_TOKEN:
        text = text.replace(config.BOT_TOKEN, REDACTED_TOKEN)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats a record as usual, then takes bot tokens out of the result -
    the message, its arguments and any traceback alike, since it works on the
    finished line rather than on any one part of the record."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging() -> None:
    """Log to stderr, without the bot token.

    httpx, which python-telegram-bot sends its requests through, logs every
    one at INFO - URL, token and all, a line for every poll of getUpdates.
    Those lines go: WARNING is where httpx has something to say. The
    formatter is the backstop for a token turning up anywhere else, such as
    in the text of a network error.

    Called from main() rather than on import, so that importing the package -
    in the tests, in the image build check - leaves the logging setup of
    whoever imports it alone."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for chatty in ("httpx", "httpcore"):
        logging.getLogger(chatty).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The last resort for an exception no handler dealt with: log it, and in a
    private chat tell the person something went wrong instead of leaving them
    waiting on an answer that is not coming.

    Only in a private chat. In a group the failing update may be a message
    that was never meant for the bot - checking whether it was is itself a
    request that can fail - and a reply to it would be noise. Updates with no
    message, inline queries and the preparations run as background tasks, are
    only logged."""
    if not isinstance(update, Update):
        logger.error("Error in a background task", exc_info=context.error)
        return

    logger.error("Unhandled error while handling update %s", update.update_id, exc_info=context.error)
    message = update.effective_message
    if message is None or update.effective_chat is None or update.effective_chat.type != "private":
        return

    try:
        await message.reply_text(handlers.UNEXPECTED_ERROR_TEXT)
    except TelegramError:
        logger.warning("Could not tell the user about the error either", exc_info=True)


# What Telegram shows about the bot, kept here rather than typed into BotFather
# so it lives in the repository with everything else. It is set on every
# start, which also means an edit made in BotFather lasts until the next one.
# The command menus are in menu.py.
BOT_SHORT_DESCRIPTION = "Пришли ссылку на рилс, фото, карусель, сторис или хайлайт из Instagram - пришлю их сюда."

BOT_DESCRIPTION = (
    "Скачиваю из Instagram рилсы, посты с фото, карусели, сторис и хайлайты. "
    "Карусель приходит одним сообщением, которое можно листать.\n\n"
    "Как пользоваться:\n"
    "• пришли мне ссылку в личку;\n"
    "• в группе упомяни меня вместе со ссылкой;\n"
    "• в любом чате набери моё имя через @ и вставь ссылку.\n\n"
    "Работает с публичными публикациями."
)


async def publish_bot_profile(bot: Any) -> None:
    """Set the command menus and the descriptions. Each on its own, and a
    failure only logged: a bot with a stale description still works, a bot
    that would not start over one does not."""
    for what, call in (
        ("command menu", lambda: bot.set_my_commands(menu.EVERYONE)),
        ("short description", lambda: bot.set_my_short_description(BOT_SHORT_DESCRIPTION)),
        ("description", lambda: bot.set_my_description(BOT_DESCRIPTION)),
    ):
        try:
            await call()
        except TelegramError:
            logger.warning("Failed to set the bot's %s", what, exc_info=True)

    await menu.show_admin_menus(bot)


async def start_background_jobs(application: Application) -> None:
    await publish_bot_profile(application.bot)
    application.bot_data["canary_task"] = canary.start(application.bot)


async def stop_background_jobs(application: Application) -> None:
    await canary.stop(application.bot_data.pop("canary_task", None))


def build_application(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .read_timeout(config.UPLOAD_TIMEOUT_SECONDS)
        .write_timeout(config.UPLOAD_TIMEOUT_SECONDS)
        .connect_timeout(30)
        .pool_timeout(30)
        # Without this the library handles one update at a time, and every
        # user waits while anyone's post downloads.
        .concurrent_updates(True)
        .post_init(start_background_jobs)
        .post_stop(stop_background_jobs)
        .build()
    )
    app.add_handler(CommandHandler("start", handlers.start))
    # A group of its own, so it runs as well as handlers.start rather than
    # instead of it: in one group only the first handler that matches does.
    app.add_handler(CommandHandler("start", menu.on_start), group=1)
    app.add_handler(CommandHandler("chatid", handlers.chatid))
    app.add_handler(CommandHandler("role", admin.role))
    app.add_handler(CommandHandler("users", admin.list_users))
    app.add_handler(InlineQueryHandler(inline.handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(inline.handle_chosen_inline_result))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    configure_logging()
    if not config.BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env or environment variables")

    build_application(config.BOT_TOKEN).run_polling(allowed_updates=Update.ALL_TYPES)
