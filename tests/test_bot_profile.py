"""The command menu and the descriptions the bot sets for itself at start.

Runs the library's real Bot with only the network layer replaced, so what is
checked is the requests that would reach Telegram.
"""
import asyncio
import json

from telegram import Bot
from telegram.constants import BotCommandLimit, BotDescriptionLimit
from telegram.request import BaseRequest

from nonnus import app


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


def test_the_menu_and_both_descriptions_are_set():
    sent = publish(RecordingRequest())

    assert json.loads(sent["setMyCommands"]["commands"]) == [
        {"command": "start", "description": "Как пользоваться ботом"}
    ]
    assert sent["setMyShortDescription"]["short_description"] == app.BOT_SHORT_DESCRIPTION
    assert sent["setMyDescription"]["description"] == app.BOT_DESCRIPTION


def test_the_setup_command_stays_out_of_the_menu():
    # /chatid is for configuring the storage chat, not for users.
    assert [command.command for command in app.BOT_COMMANDS] == ["start"]


def test_one_refusal_does_not_stop_the_rest():
    request = RecordingRequest(refuse={"setMyCommands"})

    sent = publish(request)

    assert set(sent) == {"setMyCommands", "setMyShortDescription", "setMyDescription"}


def test_the_texts_fit_telegram_limits():
    # Over a limit, Telegram refuses the call - and the failure would only
    # show up as a warning in the log at start.
    assert len(app.BOT_SHORT_DESCRIPTION) <= BotDescriptionLimit.MAX_SHORT_DESCRIPTION_LENGTH
    assert len(app.BOT_DESCRIPTION) <= BotDescriptionLimit.MAX_DESCRIPTION_LENGTH
    assert all(len(command.description) <= BotCommandLimit.MAX_DESCRIPTION for command in app.BOT_COMMANDS)


def test_the_profile_is_set_at_start(monkeypatch):
    published = []

    async def publish_bot_profile(bot):
        published.append(bot)

    monkeypatch.setattr(app, "publish_bot_profile", publish_bot_profile)
    monkeypatch.setattr(app.canary, "start", lambda bot: None)
    application = type("Application", (), {"bot": object(), "bot_data": {}})()

    asyncio.run(app.start_background_jobs(application))

    assert published == [application.bot]
