"""The admin commands: who may use them, and what /role and /users do."""
import asyncio
from types import SimpleNamespace

import pytest

from nonnus import admin, app, config, users

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


def command(handler, sender, *args, chat_type="private"):
    message = Message(sender, chat_type)
    asyncio.run(handler(SimpleNamespace(message=message), SimpleNamespace(args=list(args))))
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


def test_the_application_has_the_admin_commands():
    commands = {
        command_name
        for group in app.build_application("1:test").handlers.values()
        for handler in group
        for command_name in getattr(handler, "commands", ())
    }

    assert {"role", "users"} <= commands
