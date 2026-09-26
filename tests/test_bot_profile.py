"""The command menus and the descriptions the bot sets for itself at start.

Runs the library's real Bot with only the network layer replaced, so what is
checked is the requests that would reach Telegram.
"""
import asyncio
import json

from telegram import Bot
from telegram.constants import BotCommandLimit, BotDescriptionLimit
from telegram.request import BaseRequest

from nonnus import app, config, menu, users


class RecordingRequest(BaseRequest):
    """Records each call; refuses the endpoints listed in `refuse`."""

    def __init__(self, refuse=()):
        self.sent = []
        self.refuse = set(refuse)

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **timeouts):
        endpoint = url.rsplit("/", 1)[-1]
        self.sent.append((endpoint, request_data.json_parameters if request_data else {}))
        if endpoint in self.refuse:
            return 400, json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: nope"}).encode()
        return 200, json.dumps({"ok": True, "result": True}).encode()


def publish(request):
    bot = Bot("1:test", request=request, get_updates_request=RecordingRequest())
    asyncio.run(app.publish_bot_profile(bot))
    return {endpoint: parameters for endpoint, parameters in request.sent}


def menus_set(request):
    """The command lists sent, by the chat they were set for - None for the
    default one."""
    return {
        json.loads(parameters["scope"])["chat_id"] if "scope" in parameters else None: [
            command["command"] for command in json.loads(parameters["commands"])
        ]
        for endpoint, parameters in request.sent
        if endpoint == "setMyCommands"
    }


def test_the_menu_and_both_descriptions_are_set():
    sent = publish(RecordingRequest())

    assert json.loads(sent["setMyCommands"]["commands"]) == [
        {"command": "start", "description": "Как пользоваться ботом"}
    ]
    assert sent["setMyShortDescription"]["short_description"] == app.BOT_SHORT_DESCRIPTION
    assert sent["setMyDescription"]["description"] == app.BOT_DESCRIPTION


def test_the_setup_command_stays_out_of_the_menu():
    # /chatid is for configuring the storage chat, not for users.
    assert [command.command for command in menu.EVERYONE] == ["start"]


def test_the_admin_menu_is_everyone_s_and_the_admin_commands():
    assert [command.command for command in menu.ADMIN] == ["start", "role", "users", "chatid"]


def test_every_admin_gets_their_own_menu_at_start(monkeypatch):
    # 100 from the settings, 200 made an admin in the database; the premium
    # user gets nothing of their own.
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({100}))
    users.USER_STORE.set_role(200, users.ADMIN)
    users.USER_STORE.set_role(300, users.PREMIUM)
    request = RecordingRequest()

    publish(request)

    assert menus_set(request) == {
        None: ["start"],
        100: ["start", "role", "users", "chatid"],
        200: ["start", "role", "users", "chatid"],
    }
    scopes = [json.loads(parameters["scope"]) for endpoint, parameters in request.sent if "scope" in parameters]
    assert scopes == [{"type": "chat", "chat_id": 100}, {"type": "chat", "chat_id": 200}]


def test_taking_the_admin_menu_away_deletes_the_chat_s_own_list():
    request = RecordingRequest()
    bot = Bot("1:test", request=request, get_updates_request=RecordingRequest())

    asyncio.run(menu.hide_admin_menu(bot, 200))

    [(endpoint, parameters)] = request.sent
    assert endpoint == "deleteMyCommands"
    assert json.loads(parameters["scope"]) == {"type": "chat", "chat_id": 200}


def test_one_refusal_does_not_stop_the_rest():
    request = RecordingRequest(refuse={"setMyCommands"})

    sent = publish(request)

    assert set(sent) == {"setMyCommands", "setMyShortDescription", "setMyDescription"}


def test_the_texts_fit_telegram_limits():
    # Over a limit, Telegram refuses the call - and the failure would only
    # show up as a warning in the log at start.
    assert len(app.BOT_SHORT_DESCRIPTION) <= BotDescriptionLimit.MAX_SHORT_DESCRIPTION_LENGTH
    assert len(app.BOT_DESCRIPTION) <= BotDescriptionLimit.MAX_DESCRIPTION_LENGTH
    assert all(len(command.description) <= BotCommandLimit.MAX_DESCRIPTION for command in menu.ADMIN)


def test_the_profile_is_set_at_start(monkeypatch):
    published = []

    async def publish_bot_profile(bot):
        published.append(bot)

    monkeypatch.setattr(app, "publish_bot_profile", publish_bot_profile)
    monkeypatch.setattr(app.canary, "start", lambda bot: None)
    application = type("Application", (), {"bot": object(), "bot_data": {}})()

    asyncio.run(app.start_background_jobs(application))

    assert published == [application.bot]
