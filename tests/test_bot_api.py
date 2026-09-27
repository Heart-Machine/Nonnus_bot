"""A Bot API server of the bot's own: the addresses the library is given, and
the one logout from the cloud Bot API the move takes.

Telegram is never asked: the cloud Bot API the logout goes to is a stand-in,
so what is checked is when it is asked, and what the bot remembers of it.
"""
import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest
from telegram.error import NetworkError

from nonnus import app, bot_api, config, delivery, media

TOKEN = "1234567890:AAH-made-up-token-for-tests_0123456789"
SERVER = "http://telegram-bot-api:8081"


# --- the addresses ----------------------------------------------------------------


def test_the_cloud_bot_api_is_the_default():
    assert bot_api.base_urls() is None
    assert app.build_application(TOKEN).bot.base_url == f"https://api.telegram.org/bot{TOKEN}"


def test_the_bot_s_own_server_is_where_it_sends_everything(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)

    bot = app.build_application(TOKEN).bot

    assert bot.base_url == f"{SERVER}/bot{TOKEN}"
    assert bot.base_file_url == f"{SERVER}/file/bot{TOKEN}"
    # Files go as the bot hands them over - open files, uploaded - which the
    # server takes as they are.
    assert bot.local_mode is False


# --- the logout from the cloud -----------------------------------------------------


class CloudBotApi:
    """Stands in for telegram.Bot as the logout uses it."""

    def __init__(self, fails=False):
        self.created = []
        self.logouts = 0
        self.fails = fails

    def __call__(self, token, **kwargs):
        # Only the token: no base_url, so the logout goes to the cloud.
        self.created.append((token, kwargs))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def log_out(self):
        if self.fails:
            raise NetworkError("the cloud did not answer")
        self.logouts += 1
        return True


@pytest.fixture
def cloud(monkeypatch):
    stand_in = CloudBotApi()
    monkeypatch.setattr(bot_api, "Bot", stand_in)
    return stand_in


def test_on_the_cloud_nothing_is_logged_out(cloud):
    asyncio.run(bot_api.settle_on_server(TOKEN))

    assert cloud.created == []
    assert not config.CLOUD_LOGOUT_MARKER.exists()


def test_the_first_start_on_the_bot_s_own_server_logs_out_of_the_cloud(monkeypatch, cloud):
    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)

    asyncio.run(bot_api.settle_on_server(TOKEN))

    assert cloud.created == [(TOKEN, {})]
    assert cloud.logouts == 1
    assert config.CLOUD_LOGOUT_MARKER.exists()


def test_the_logout_is_done_once(monkeypatch, cloud):
    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)

    asyncio.run(bot_api.settle_on_server(TOKEN))
    asyncio.run(bot_api.settle_on_server(TOKEN))

    assert cloud.logouts == 1


def test_a_failed_logout_is_tried_again_on_the_next_start(monkeypatch, caplog):
    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)
    failing = CloudBotApi(fails=True)
    monkeypatch.setattr(bot_api, "Bot", failing)

    with caplog.at_level(logging.ERROR, logger="nonnus.bot_api"):
        asyncio.run(bot_api.settle_on_server(TOKEN))

    assert not config.CLOUD_LOGOUT_MARKER.exists()
    assert "next start tries again" in caplog.text

    working = CloudBotApi()
    monkeypatch.setattr(bot_api, "Bot", working)
    asyncio.run(bot_api.settle_on_server(TOKEN))

    assert working.logouts == 1
    assert config.CLOUD_LOGOUT_MARKER.exists()


def test_back_on_the_cloud_the_next_move_logs_out_again(monkeypatch, cloud):
    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)
    asyncio.run(bot_api.settle_on_server(TOKEN))

    # Back on the cloud: nothing to call, the marker goes.
    monkeypatch.setattr(config, "TELEGRAM_API_URL", "")
    asyncio.run(bot_api.settle_on_server(TOKEN))
    assert not config.CLOUD_LOGOUT_MARKER.exists()

    # And on the bot's own server once more, it logs out again.
    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)
    asyncio.run(bot_api.settle_on_server(TOKEN))
    assert cloud.logouts == 2


def test_main_leaves_the_cloud_before_the_bot_starts(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)

    async def settle_on_server(token):
        calls.append(("settle", token))

    class Application:
        def run_polling(self, **kwargs):
            calls.append("polling")

    monkeypatch.setattr(bot_api, "settle_on_server", settle_on_server)
    monkeypatch.setattr(app, "build_application", lambda token: Application())

    app.main()

    assert calls == ["logging", ("settle", TOKEN), "polling"]


# --- how large a file may be ---------------------------------------------------------


def size_settings(**env):
    """MAX_FILE_SIZE_MB, VIDEO_COMPRESSION_TARGET_MB and UPLOAD_TIMEOUT_SECONDS
    as the bot works them out from this environment - in an interpreter of
    their own, since config reads the environment once, on import. Empty is
    what the deploy writes for a setting that is not set."""
    environment = {
        **os.environ,
        "TELEGRAM_API_URL": "",
        "MAX_FILE_SIZE_MB": "",
        "VIDEO_COMPRESSION_TARGET_MB": "",
        "UPLOAD_TIMEOUT_SECONDS": "",
        **env,
    }
    script = (
        "from nonnus import config; "
        "print(config.MAX_FILE_SIZE_MB, config.VIDEO_COMPRESSION_TARGET_MB, config.UPLOAD_TIMEOUT_SECONDS)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment, cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True, check=True,
    )
    return tuple(int(value) for value in result.stdout.split())


def test_on_the_cloud_a_file_is_up_to_50_mb():
    assert size_settings() == (50, 49, 180)


def test_on_the_bot_s_own_server_a_file_is_up_to_2000_mb():
    # What the server takes with --local; a longer wait for the answer, which
    # comes once the server has passed the file on to Telegram.
    assert size_settings(TELEGRAM_API_URL=SERVER) == (2000, 1999, 900)


def test_a_limit_that_is_set_wins_and_compression_follows_it():
    assert size_settings(TELEGRAM_API_URL=SERVER, MAX_FILE_SIZE_MB="300") == (300, 299, 900)
    assert size_settings(MAX_FILE_SIZE_MB="40", UPLOAD_TIMEOUT_SECONDS="60") == (40, 39, 60)


# --- what a request may carry -------------------------------------------------------


def test_a_server_of_the_bot_s_own_takes_an_album_of_any_size(monkeypatch, tmp_path):
    # Ten stories of 8 MiB are 80 MiB: past what the cloud takes in one
    # request, not what the bot's own server does.
    items = []
    for n in range(10):
        path = tmp_path / f"story-{n:02d}.jpg"
        with path.open("wb") as file:
            file.truncate(8 * 1024 * 1024)
        items.append(media.MediaItem(path, "photo"))

    assert [len(group) for group in delivery.upload_groups(items)] == [7, 3]

    monkeypatch.setattr(config, "TELEGRAM_API_URL", SERVER)

    assert delivery.upload_groups(items) == [items]
