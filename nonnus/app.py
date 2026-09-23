"""Wiring the handlers into the application."""

import logging

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

from nonnus import config, inline, handlers, canary


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)


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


async def start_background_jobs(application: Application) -> None:
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
    app.add_handler(CommandHandler("chatid", handlers.chatid))
    app.add_handler(InlineQueryHandler(inline.handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(inline.handle_chosen_inline_result))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env or environment variables")

    build_application(config.BOT_TOKEN).run_polling(allowed_updates=Update.ALL_TYPES)
