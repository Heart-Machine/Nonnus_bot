"""Wiring the handlers into the application."""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from nonnus import config, inline, handlers


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)


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
        .build()
    )
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("chatid", handlers.chatid))
    app.add_handler(InlineQueryHandler(inline.handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(inline.handle_chosen_inline_result))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    return app


def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env or environment variables")

    build_application(config.BOT_TOKEN).run_polling(allowed_updates=Update.ALL_TYPES)
