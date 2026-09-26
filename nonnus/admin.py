"""Commands for the bot's admins: who has which role, and changing it.

They are answered only to an admin, and only in a private chat with the bot.
Anyone else gets no answer at all, so the commands give nothing away - not
even that they exist - and in a group a list of users would be read by
everyone in it.
"""

import logging
import sqlite3
from typing import Any, Optional

from telegram import Update
from telegram.ext import ContextTypes

from nonnus import config, menu, users


logger = logging.getLogger(__name__)


ROLE_NAMES = {
    "admin": users.ADMIN,
    "админ": users.ADMIN,
    "premium": users.PREMIUM,
    "премиум": users.PREMIUM,
    "regular": users.REGULAR,
    "обычный": users.REGULAR,
}

ROLE_TITLES = {users.ADMIN: "админ", users.PREMIUM: "премиум", users.REGULAR: "обычный"}

ROLE_USAGE_TEXT = (
    "Как пользоваться:\n"
    "/role @username — статус пользователя\n"
    "/role @username premium — сменить статус\n\n"
    "Вместо @username можно указать числовой ID. "
    "Статусы: admin, premium, regular — или админ, премиум, обычный."
)

DATABASE_FAILED_TEXT = "Не получилось обратиться к базе пользователей, подробности в логе."

# Telegram's limit on a message is 4096 characters.
MESSAGE_LIMIT = 4000


def admin_message(update: Update) -> Any:
    """The message, when an admin wrote it in a private chat; None otherwise."""
    message = update.message
    if message is None or message.chat.type != "private" or not users.is_admin(message.from_user):
        return None
    return message


def describe(user_id: int, record: Optional[users.UserRecord]) -> str:
    """"@username (Имя), ID 123", with whatever of it is known."""
    name = ""
    if record is not None and record.username:
        name = f"@{record.username}" + (f" ({record.first_name})" if record.first_name else "")
    elif record is not None and record.first_name:
        name = record.first_name
    return f"{name}, ID {user_id}" if name else f"ID {user_id}"


def find_user(reference: str) -> tuple[Optional[int], Optional[users.UserRecord]]:
    """The user an admin means: a numeric id, known to the bot or not, or a
    username of someone who has written to it - there is no other way to learn
    who a username belongs to."""
    if reference.isdigit():
        user_id = int(reference)
        return user_id, users.USER_STORE.get(user_id)

    record = users.USER_STORE.find_by_username(reference.removeprefix("@"))
    return (record.user_id, record) if record else (None, None)


async def role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = admin_message(update)
    if message is None:
        return

    users.remember(message.from_user)
    args = context.args or []
    if not 1 <= len(args) <= 2:
        await message.reply_text(ROLE_USAGE_TEXT)
        return

    new_role = None
    if len(args) == 2:
        new_role = ROLE_NAMES.get(args[1].lower())
        if new_role is None:
            await message.reply_text(f"Не знаю статуса «{args[1]}».\n\n{ROLE_USAGE_TEXT}")
            return

    try:
        user_id, record = find_user(args[0])
        if user_id is None:
            await message.reply_text(
                f"Не знаю пользователя {args[0]}: он ещё не писал боту. Можно указать его числовой ID."
            )
            return

        who = describe(user_id, record)
        if new_role is None:
            await message.reply_text(f"{who} — {ROLE_TITLES[users.role_of(user_id)]}.\n{usage_line(user_id)}")
            return

        if user_id in config.ADMIN_USER_IDS:
            await message.reply_text(
                f"{who} — админ из настроек бота (ADMIN_USER_IDS). Командой этот статус не меняется."
            )
            return

        old_role = users.role_of(user_id)
        users.USER_STORE.set_role(user_id, new_role)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to look up or change a role")
        await message.reply_text(DATABASE_FAILED_TEXT)
        return

    logger.info("Admin %s set the role of user %s to %s", message.from_user.id, user_id, new_role)
    await menu.after_role_change(context.bot, user_id, old_role, new_role)
    await message.reply_text(f"Готово: {who} — теперь {ROLE_TITLES[new_role]}.")


def usage_line(user_id: int) -> str:
    limit = users.daily_limit_of(user_id)
    downloaded = users.downloads_today(user_id)
    if limit is None:
        return f"Новых публикаций сегодня: {downloaded}, без ограничений."
    return f"Новых публикаций сегодня: {downloaded} из {limit}."


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = admin_message(update)
    if message is None:
        return

    users.remember(message.from_user)
    try:
        privileged = users.USER_STORE.with_roles((users.ADMIN, users.PREMIUM))
        total = users.USER_STORE.count()
        # Admins from the settings may never have written to the bot, and
        # the database may say anything about them - the settings win.
        known = {record.user_id: record for record in privileged}
        for user_id in config.ADMIN_USER_IDS:
            if user_id not in known:
                known[user_id] = users.USER_STORE.get(user_id)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to list the users")
        await message.reply_text(DATABASE_FAILED_TEXT)
        return

    admins = sorted(user_id for user_id in known if users.role_of(user_id) == users.ADMIN)
    premium = sorted(user_id for user_id in known if users.role_of(user_id) == users.PREMIUM)

    lines = ["Админы:"]
    for user_id in admins:
        origin = " — из настроек" if user_id in config.ADMIN_USER_IDS else ""
        lines.append(f"• {describe(user_id, known[user_id])}{origin}")
    lines.append("")
    lines.append("Премиум:" if premium else "Премиум: нет")
    lines.extend(f"• {describe(user_id, known[user_id])}" for user_id in premium)
    lines.append("")
    lines.append(f"Всего пользователей в базе: {total}")

    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
    await message.reply_text(text)
