"""The daily check that the bot can still download from Instagram: what it
runs, when the owner hears about it, and that it never holds up a shutdown.

The download itself is a stand-in; the delays are cut to nothing.
"""
import asyncio
from types import SimpleNamespace

import pytest

from nonnus import app, canary, config, instagram, media, preparation

POST_URL = "https://www.instagram.com/p/ABC123/"


class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

    # Set at start next to the canary; not what these tests look at.
    async def set_my_commands(self, *args, **kwargs):
        pass

    async def set_my_short_description(self, *args, **kwargs):
        pass

    async def set_my_description(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(canary, "FIRST_CHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(canary, "CHECK_INTERVAL_SECONDS", 0)

    async def no_alert(context):
        pass

    monkeypatch.setattr(preparation, "alert_if_cookies_rejected", no_alert)


# --- one check ------------------------------------------------------------


def test_a_post_that_downloads_passes_the_check_and_leaves_nothing_behind(monkeypatch):
    seen_dirs = []

    def download_post(url, download_dir):
        seen_dirs.append(download_dir)
        path = download_dir / "photo.jpg"
        path.write_bytes(b"photo")
        return [media.MediaItem(path, "photo")], "caption"

    monkeypatch.setattr(instagram, "download_post", download_post)

    assert asyncio.run(canary.check_once(RecordingBot(), POST_URL)) is None
    assert seen_dirs and not seen_dirs[0].exists()


def test_a_post_that_fails_to_download_fails_the_check(monkeypatch):
    error = instagram.IncompletePostError("file 2 of 3")

    def download_post(url, download_dir):
        raise error

    monkeypatch.setattr(instagram, "download_post", download_post)

    assert asyncio.run(canary.check_once(RecordingBot(), POST_URL)) is error


def test_the_check_goes_through_the_conversion_too(monkeypatch):
    # Compression and photo conversion are ffmpeg, which can break as well.
    calls = []
    monkeypatch.setattr(instagram, "download_post", lambda url, download_dir: ([], "caption"))
    monkeypatch.setattr(media, "prepare_items_for_upload", lambda items, work_dir: calls.append(items) or (items, False))

    asyncio.run(canary.check_once(RecordingBot(), POST_URL))

    assert calls == [[]]


# --- telling the owner ----------------------------------------------------


def run_checks(monkeypatch, outcomes):
    """Run the loop over the given outcomes - None for a pass, an error for a
    failure - and return what the owner was sent."""
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    remaining = list(outcomes)

    async def check_once(bot, url):
        if not remaining:
            raise asyncio.CancelledError
        return remaining.pop(0)

    monkeypatch.setattr(canary, "check_once", check_once)
    bot = RecordingBot()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await canary.run(bot, POST_URL)

    asyncio.run(run())
    return [message["text"] for message in bot.sent]


def test_the_owner_hears_once_when_it_breaks_and_once_when_it_is_back(monkeypatch):
    broken = RuntimeError("Failed to parse JSON")

    sent = run_checks(monkeypatch, [None, broken, broken, broken, None, None])

    assert len(sent) == 2
    assert "не смогла скачать контрольный пост" in sent[0]
    assert sent[1] == canary.RECOVERY_TEXT


def test_passing_checks_say_nothing(monkeypatch):
    assert run_checks(monkeypatch, [None, None, None]) == []


def test_a_failure_right_after_start_is_reported(monkeypatch):
    # The first check is a few minutes after start - after a deploy.
    sent = run_checks(monkeypatch, [RuntimeError("broken by this deploy")])

    assert len(sent) == 1


def test_the_alert_says_what_failed_and_what_to_do():
    text = canary.failure_text(POST_URL, RuntimeError("x" * 1000))

    assert POST_URL in text
    assert "RuntimeError: " + "x" * canary.ERROR_EXCERPT_LENGTH + "\n" in text
    assert "yt-dlp" in text and "CANARY_POST_URL" in text


def test_without_a_storage_chat_the_result_is_only_logged(monkeypatch):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "")
    bot = RecordingBot()

    asyncio.run(canary.tell_owner(bot, "text"))

    assert bot.sent == []


# --- starting and stopping ------------------------------------------------


@pytest.mark.parametrize("value", ["", "https://example.com/p/ABC123/", "not a link"])
def test_no_post_no_check(monkeypatch, value):
    monkeypatch.setattr(config, "CANARY_POST_URL", value)

    async def run():
        return canary.start(RecordingBot())

    assert asyncio.run(run()) is None


def test_the_check_starts_with_the_bot_and_stops_with_it(monkeypatch):
    # It never ends by itself, so stopping must cancel it - the application
    # would otherwise wait on it, and every deploy would hang on shutdown.
    monkeypatch.setattr(config, "CANARY_POST_URL", POST_URL)
    monkeypatch.setattr(canary, "FIRST_CHECK_DELAY_SECONDS", 3600)

    async def run():
        loop = asyncio.get_running_loop()
        application = SimpleNamespace(bot=RecordingBot(), bot_data={})
        await app.start_background_jobs(application)
        task = application.bot_data["canary_task"]
        running = not task.done()
        started = loop.time()
        await asyncio.wait_for(app.stop_background_jobs(application), timeout=1)
        return running, task.cancelled(), loop.time() - started, application.bot_data

    running, cancelled, took, bot_data = asyncio.run(run())

    assert running and cancelled
    assert took < 0.5
    assert bot_data == {}


def test_stopping_the_check_does_not_swallow_a_cancellation_of_the_stop_itself(monkeypatch):
    async def never_ending():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)  # a check slow to wind down

    async def run():
        task = asyncio.get_running_loop().create_task(never_ending())
        await asyncio.sleep(0)
        stopping = asyncio.get_running_loop().create_task(canary.stop(task))
        await asyncio.sleep(0.05)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        task.cancel()

    asyncio.run(run())


def test_the_application_starts_and_stops_the_background_jobs():
    application = app.build_application("1:test")

    assert application.post_init is app.start_background_jobs
    assert application.post_stop is app.stop_background_jobs
