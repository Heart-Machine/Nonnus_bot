"""The command menu Telegram shows by the message box: /start for everyone,
the admin commands on top of it for admins only.

Telegram picks the menu of a private chat by the most specific scope that has
one - that chat's own list first, the default one after - and in a private
chat the chat's id is the user's. So an admin gets a list set for their chat
alone, everyone else sees the default, and taking an admin's list away puts
the default back.

The menu is a hint, not a lock: anyone can type /role, and admin.py answers
only an admin whatever the menu shows.
"""

import logging
import sqlite3
from typing import Any

from telegram import BotCommand, BotCommandScopeChat
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from nonnus import config, users


logger = logging.getLogger(__name__)


# /chatid stays out of the menu everyone sees: it is for setting up the
# storage chat, not something users need. Admins do.
EVERYONE = [BotCommand("start", "Как пользоваться ботом")]

ADMIN = EVERYONE + [
    BotCommand("role", "Статус пользователя: посмотреть или сменить"),
    BotCommand("users", "Админы и премиум-пользователи"),
    BotCommand("chatid", "ID этого чата"),
]


async def show_admin_menu(bot: Any, user_id: int) -> None:
    """Give an admin their menu. Telegram refuses it for someone who has
    never opened a chat with the bot - there is no chat to set it for - so a
    failure is only logged: /start sets it again, once they write."""
    try:
        await bot.set_my_commands(ADMIN, scope=BotCommandScopeChat(user_id))
    except TelegramError as error:
        logger.warning("Could not set the admin menu for user %s: %s", user_id, error)


async def hide_admin_menu(bot: Any, user_id: int) -> None:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(user_id))
    except TelegramError as error:
        logger.warning("Could not take the admin menu from user %s: %s", user_id, error)


async def show_admin_menus(bot: Any) -> None:
    """Give every admin their menu - at start, since roles can change in the
    database by hand while the bot is down."""
    admin_ids = set(config.ADMIN_USER_IDS)
    try:
        admin_ids.update(record.user_id for record in users.USER_STORE.with_roles((users.ADMIN,)))
    except (sqlite3.Error, OSError):
        logger.exception("Failed to read the admins for their menus")

    for user_id in sorted(admin_ids):
        await show_admin_menu(bot, user_id)


async def after_role_change(bot: Any, user_id: int, old_role: str, new_role: str) -> None:
    if new_role == users.ADMIN and old_role != users.ADMIN:
        await show_admin_menu(bot, user_id)
    elif old_role == users.ADMIN and new_role != users.ADMIN:
        await hide_admin_menu(bot, user_id)


async def on_start(update: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start from an admin in private sets their menu: the first chance for
    one who had never written to the bot when it started."""
    message = update.message
    if message is None or message.chat.type != "private" or not users.is_admin(message.from_user):
        return
    await show_admin_menu(context.bot, message.from_user.id)
