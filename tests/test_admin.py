"""The admin commands: who may use them, and what /role and /users do."""
import asyncio
from types import SimpleNamespace

import pytest

from telegram.error import BadRequest

from nonnus import admin, app, config, handlers, menu, users

OWNER = SimpleNamespace(id=100, username="owner", first_name="Owner")
HELPER = SimpleNamespace(id=200, username="helper", first_name="Helper")
SOMEONE = SimpleNamespace(id=300, username="Someone", first_name="Some")


class Message:
    def __init__(self, sender, chat_type="private"):
        self.from_user = sender
        self.chat = SimpleNamespace(type=chat_type)
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class MenuBot:
    """Records the menus set and taken away, by chat; refuses the chats in
    `refuse` the way Telegram refuses one the user never opened."""

    def __init__(self, refuse=()):
        self.menus = []
        self.refuse = set(refuse)

    async def set_my_commands(self, commands, scope=None):
        if scope is not None and scope.chat_id in self.refuse:
            raise BadRequest("Chat not found")
        self.menus.append(("set", scope.chat_id, [command.command for command in commands]))

    async def delete_my_commands(self, scope=None):
        self.menus.append(("delete", scope.chat_id, None))


def command(handler, sender, *args, chat_type="private", bot=None):
    message = Message(sender, chat_type)
    context = SimpleNamespace(args=list(args), bot=bot or MenuBot())
    asyncio.run(handler(SimpleNamespace(message=message), context))
    return message.replies


@pytest.fixture(autouse=True)
def owner_is_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({OWNER.id}))
    users.remember(SOMEONE)


def test_someone_who_is_not_an_admin_gets_no_answer():
    assert command(admin.role, SOMEONE, "@someone", "premium") == []
    assert command(admin.list_users, SOMEONE) == []
    assert users.role_of(SOMEONE.id) == users.REGULAR


def test_an_admin_in_a_group_gets_no_answer_either():
    assert command(admin.role, OWNER, "@someone", "premium", chat_type="supergroup") == []
    assert command(admin.list_users, OWNER, chat_type="supergroup") == []
    assert users.role_of(SOMEONE.id) == users.REGULAR


@pytest.mark.parametrize("name, role", [("premium", users.PREMIUM), ("Премиум", users.PREMIUM), ("admin", users.ADMIN)])
def test_an_admin_sets_a_role_by_username(name, role):
    [reply] = command(admin.role, OWNER, "@SOMEONE", name)

    assert users.role_of(SOMEONE.id) == role
    assert reply.startswith("Готово: @Someone (Some), ID 300")


def test_a_role_can_be_given_by_id_to_someone_the_bot_has_not_seen():
    command(admin.role, OWNER, "555", "premium")

    assert users.role_of(555) == users.PREMIUM


def test_a_username_nobody_used_the_bot_with_is_explained():
    [reply] = command(admin.role, OWNER, "@nobody", "premium")

    assert "не писал боту" in reply


def test_an_unknown_role_is_explained():
    [reply] = command(admin.role, OWNER, "@someone", "vip")

    assert "Не знаю статуса «vip»" in reply
    assert users.role_of(SOMEONE.id) == users.REGULAR


def test_without_a_role_it_shows_the_current_one_and_the_day_so_far():
    users.take_download(SOMEONE)

    [reply] = command(admin.role, OWNER, "@someone")

    assert "обычный" in reply
    assert "1 из 5" in reply


def test_an_admin_from_the_settings_cannot_be_changed_by_command():
    users.remember(OWNER)

    [reply] = command(admin.role, OWNER, "@owner", "regular")

    assert "ADMIN_USER_IDS" in reply
    assert users.role_of(OWNER.id) == users.ADMIN


def test_an_admin_made_by_command_can_use_the_commands():
    command(admin.role, OWNER, "200", "admin")

    [reply] = command(admin.role, HELPER, "@someone", "premium")

    assert reply.startswith("Готово")
    assert users.role_of(SOMEONE.id) == users.PREMIUM


def test_no_arguments_get_the_usage():
    [reply] = command(admin.role, OWNER)

    assert reply.startswith("Как пользоваться")


def test_users_lists_the_admins_and_premium_users(monkeypatch):
    # 400 is an admin in the settings who has never written to the bot: known
    # by id alone.
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({OWNER.id, 400}))
    command(admin.role, OWNER, "@someone", "premium")
    command(admin.role, OWNER, "200", "admin")

    [reply] = command(admin.list_users, OWNER)

    admins, premium, total = reply.split("\n\n")
    assert admins == "Админы:\n• @owner (Owner), ID 100 — из настроек\n• ID 200\n• ID 400 — из настроек"
    assert premium == "Премиум:\n• @Someone (Some), ID 300"
    assert total == "Всего пользователей в базе: 3"


# --- the admin menu ----------------------------------------------------------


ADMIN_MENU = ["start", "role", "users", "chatid"]


def test_making_someone_an_admin_gives_them_the_admin_menu():
    bot = MenuBot()

    command(admin.role, OWNER, "@someone", "admin", bot=bot)

    assert bot.menus == [("set", SOMEONE.id, ADMIN_MENU)]


def test_an_admin_made_something_else_loses_it():
    users.USER_STORE.set_role(SOMEONE.id, users.ADMIN)
    bot = MenuBot()

    command(admin.role, OWNER, "@someone", "premium", bot=bot)

    assert bot.menus == [("delete", SOMEONE.id, None)]


@pytest.mark.parametrize("before, after", [(users.REGULAR, "premium"), (users.PREMIUM, "regular"), (users.ADMIN, "admin")])
def test_other_changes_leave_the_menu_alone(before, after):
    users.USER_STORE.set_role(SOMEONE.id, before)
    bot = MenuBot()

    command(admin.role, OWNER, "@someone", after, bot=bot)

    assert bot.menus == []


def test_a_menu_telegram_refuses_is_not_an_error():
    # Someone who never wrote to the bot has no chat to set a menu for.
    [reply] = command(admin.role, OWNER, "555", "admin", bot=MenuBot(refuse={555}))

    assert reply.startswith("Готово")


def test_start_gives_an_admin_their_menu_and_nobody_else(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({OWNER.id}))
    bot = MenuBot()

    for sender, chat_type in ((OWNER, "private"), (SOMEONE, "private"), (OWNER, "supergroup")):
        command(menu.on_start, sender, chat_type=chat_type, bot=bot)

    assert bot.menus == [("set", OWNER.id, ADMIN_MENU)]


def test_one_admin_telegram_refuses_does_not_keep_the_others_from_their_menus(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({100, 200}))
    bot = MenuBot(refuse={100})

    asyncio.run(menu.show_admin_menus(bot))

    assert bot.menus == [("set", 200, ADMIN_MENU)]


def test_start_goes_to_both_the_help_and_the_menu():
    # Two handlers for one command run only from different groups: in one
    # group the first that matches is the only one.
    groups = {
        handler.callback: group
        for group, group_handlers in app.build_application("1:test").handlers.items()
        for handler in group_handlers
        if "start" in getattr(handler, "commands", ())
    }

    assert set(groups) == {handlers.start, menu.on_start}
    assert groups[handlers.start] != groups[menu.on_start]


def test_the_application_has_the_admin_commands():
    commands = {
        command_name
        for group in app.build_application("1:test").handlers.values()
        for handler in group
        for command_name in getattr(handler, "commands", ())
    }

    assert {"role", "users"} <= commands
