"""Handling updates side by side: the application is built to, the heavy
work still takes turns, and a burst of inline queries uploads the placeholder
once.

The downloads here are stand-ins that only sleep and count, so what gets
checked is how many run at once, not what they fetch.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import bot


def test_the_application_handles_updates_concurrently():
    assert bot.build_application("1:test").concurrent_updates > 1


class ConcurrencyMeter:
    """A blocking stand-in for a download: sleeps in its worker thread and
    records the most calls that were in progress at the same time."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def __call__(self, *args):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.05)
        with self._lock:
            self.running -= 1
        return [], "caption"


@pytest.fixture
def two_slots(monkeypatch):
    monkeypatch.setattr(bot, "DOWNLOAD_SLOTS", asyncio.Semaphore(2))

    async def no_alert(context):
        pass

    monkeypatch.setattr(bot, "alert_if_cookies_rejected", no_alert)


def run_side_by_side(count, make_call):
    async def all_at_once():
        await asyncio.gather(*(make_call() for _ in range(count)))

    asyncio.run(all_at_once())


def test_downloads_take_turns_past_the_cap(monkeypatch, tmp_path, two_slots):
    meter = ConcurrencyMeter()
    monkeypatch.setattr(bot, "download_post", meter)

    run_side_by_side(6, lambda: bot.download_post_in_thread("url", tmp_path, None))

    assert meter.peak == 2


def test_downloads_under_the_cap_run_together(monkeypatch, tmp_path, two_slots):
    meter = ConcurrencyMeter()
    monkeypatch.setattr(bot, "download_post", meter)

    run_side_by_side(2, lambda: bot.download_post_in_thread("url", tmp_path, None))

    assert meter.peak == 2


def test_compression_takes_the_same_slots(monkeypatch, tmp_path, two_slots):
    meter = ConcurrencyMeter()
    monkeypatch.setattr(bot, "prepare_items_for_upload", meter)

    run_side_by_side(6, lambda: bot.prepare_items_in_thread([], tmp_path))

    assert meter.peak == 2


def test_a_burst_of_inline_queries_uploads_the_placeholder_once(monkeypatch):
    monkeypatch.setattr(bot, "STORAGE_CHAT_ID", "-100")
    monkeypatch.setattr(bot, "PLACEHOLDER_UPLOAD_LOCK", asyncio.Lock())
    uploads = []

    async def send_photo(**kwargs):
        uploads.append(kwargs)
        await asyncio.sleep(0.01)
        return SimpleNamespace(photo=[SimpleNamespace(file_id="placeholder")])

    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={}),
        bot=SimpleNamespace(send_photo=send_photo),
    )

    async def burst():
        return await asyncio.gather(*(bot.get_placeholder_photo_file_id(context) for _ in range(5)))

    assert asyncio.run(burst()) == ["placeholder"] * 5
    assert len(uploads) == 1
