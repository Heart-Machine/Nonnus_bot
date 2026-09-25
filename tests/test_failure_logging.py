"""A failed preparation is logged in full once - by the application's error
handler, as the background task it is - and in one line by everyone who was
waiting on it. It used to be a traceback per waiter, and one more for every
keystroke of an inline query answered from the remembered failure.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from nonnus import config, handlers, inline, instagram, preparation

POST_URL = "https://www.instagram.com/p/Dbn6eI6CvJn/"
YT_DLP_ERROR = (
    "ERROR: [Instagram] Dbn6eI6CvJn: Instagram sent an empty media response. Check if this post is accessible\n"
    "in your browser without being logged-in. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ for more."
)


@pytest.fixture
def failing_post(monkeypatch):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")

    def download_post(url, download_dir):
        raise RuntimeError(YT_DLP_ERROR)

    async def placeholder(context, kind):
        return "placeholder"

    async def no_alert(context):
        pass

    monkeypatch.setattr(instagram, "download_post", download_post)
    monkeypatch.setattr(inline, "get_placeholder_photo_file_id", placeholder)
    monkeypatch.setattr(preparation, "alert_if_cookies_rejected", no_alert)


def new_context(loop):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"bot_username": "nonnus_bot"}, create_task=loop.create_task),
        bot=SimpleNamespace(send_chat_action=_no_action),
    )


async def _no_action(**kwargs):
    pass


async def settle(context):
    for task in list(context.application.bot_data.get("inline_tasks", {}).values()):
        await asyncio.gather(task, return_exceptions=True)


class InlineQuery:
    query = POST_URL

    async def answer(self, results, **kwargs):
        pass


def test_the_failure_carries_the_link_into_its_one_full_traceback(failing_post):
    # The application's error handler logs it knowing nothing of the post.
    async def run():
        context = new_context(asyncio.get_running_loop())
        task = preparation.get_or_create_prepare_task(POST_URL, context)
        await asyncio.gather(task, return_exceptions=True)
        return task.exception()

    error = asyncio.run(run())

    assert f"While preparing {POST_URL}" in error.__notes__


def test_inline_queries_answered_from_a_remembered_failure_log_one_line_each(failing_post, caplog):
    async def run():
        context = new_context(asyncio.get_running_loop())
        for _ in range(3):
            await inline.handle_inline_query(SimpleNamespace(inline_query=InlineQuery()), context)
            await settle(context)

    with caplog.at_level(logging.INFO, logger="nonnus.inline"):
        asyncio.run(run())

    lines = [record for record in caplog.records if record.name == "nonnus.inline"]
    assert len(lines) == 2  # the first query got the placeholder
    assert all(record.exc_info is None for record in lines)
    assert all("\n" not in record.getMessage() and POST_URL in record.getMessage() for record in lines)


def test_a_link_sent_to_the_bot_logs_one_line_for_a_failed_preparation(failing_post, caplog):
    class Status:
        async def edit_text(self, text, **kwargs):
            pass

        async def delete(self):
            pass

    class Message:
        chat_id = 1

        async def reply_text(self, text, **kwargs):
            return Status()

    async def run():
        await handlers.deliver_post(Message(), POST_URL, new_context(asyncio.get_running_loop()))

    with caplog.at_level(logging.INFO, logger="nonnus.handlers"):
        asyncio.run(run())

    lines = [record for record in caplog.records if record.name == "nonnus.handlers"]
    assert len(lines) == 1
    assert lines[0].exc_info is None
    assert "empty media response" in lines[0].getMessage()


@pytest.mark.parametrize(
    "error, expected",
    [
        (RuntimeError(YT_DLP_ERROR), "RuntimeError: ERROR: [Instagram] Dbn6eI6CvJn: Instagram sent an empty media"),
        (ValueError("x" * 500), "ValueError: " + "x" * 200),
        (TimeoutError(), "TimeoutError: "),
    ],
)
def test_a_failure_is_described_in_one_short_line(error, expected):
    described = preparation.describe_failure(error)

    assert described.startswith(expected)
    assert "\n" not in described
    assert len(described) <= len(type(error).__name__) + 2 + 200
