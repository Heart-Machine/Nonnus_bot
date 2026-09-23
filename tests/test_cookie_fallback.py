"""Falling back from the session cookies to logged-out downloads, and the
alert that tells the owner the cookies stopped working.

yt-dlp is replaced by a stand-in for probe_post, so what gets checked is the
routing: which route is tried, in what order, what the session state becomes,
and when the owner hears about it.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp.utils import DownloadError

from nonnus import config, media, instagram, preparation

INFO = {"id": "post"}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session(monkeypatch, clock):
    instance = instagram.InstagramSession("cookies.txt", retry_after=60, alert_every=600, clock=clock)
    monkeypatch.setattr(instagram, "INSTAGRAM_SESSION", instance)
    return instance


# --- the session state ----------------------------------------------------


def test_cookies_are_used_while_trusted(session):
    assert session.use_cookies() is True


def test_no_cookie_file_means_no_cookies(clock):
    assert instagram.InstagramSession("", clock=clock).use_cookies() is False


def test_a_rejection_suspends_the_cookies_for_a_while(session, clock):
    session.mark_rejected()
    assert session.use_cookies() is False

    clock.now += 59
    assert session.use_cookies() is False

    clock.now += 1
    assert session.use_cookies() is True


def test_a_rejection_raises_the_alert_exactly_once(session):
    session.mark_rejected()

    assert session.take_alert() is True
    assert session.take_alert() is False


def test_alerts_are_rate_limited(session, clock):
    session.mark_rejected()
    session.take_alert()

    clock.now += 300
    session.mark_rejected()
    assert session.take_alert() is False

    clock.now += 300
    session.mark_rejected()
    assert session.take_alert() is True


# --- probing with the fallback ------------------------------------------


def fake_probe(monkeypatch, *, with_cookies, without_cookies):
    """Stand in for probe_post. Each outcome is an info dict to return or an
    exception to raise. Returns the list of routes tried, in order."""
    tried = []

    def probe_post(url, download_dir, use_cookies=True):
        tried.append("cookies" if use_cookies else "anonymous")
        outcome = with_cookies if use_cookies else without_cookies
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(instagram, "probe_post", probe_post)
    return tried


def test_working_cookies_are_used_and_nothing_else_is_tried(monkeypatch, session, tmp_path):
    tried = fake_probe(monkeypatch, with_cookies=INFO, without_cookies=AssertionError("not reached"))

    assert instagram.probe_post_with_fallback("https://x/", tmp_path) == (INFO, True)
    assert tried == ["cookies"]
    assert session.take_alert() is False


def test_turned_down_cookies_fall_back_and_raise_the_alert(monkeypatch, session, tmp_path):
    # A dead session surfaces from yt-dlp as a JSON parse error, not as
    # anything about logging in - which is why nothing keys off the wording.
    tried = fake_probe(
        monkeypatch,
        with_cookies=DownloadError("ERROR: [Instagram] X: Failed to parse JSON"),
        without_cookies=INFO,
    )

    assert instagram.probe_post_with_fallback("https://x/", tmp_path) == (INFO, False)
    assert tried == ["cookies", "anonymous"]
    assert session.use_cookies() is False
    assert session.take_alert() is True


def test_a_post_failing_both_ways_is_blamed_on_the_post(monkeypatch, session, tmp_path):
    cookie_error = DownloadError("private")
    fake_probe(monkeypatch, with_cookies=cookie_error, without_cookies=DownloadError("login required"))

    with pytest.raises(DownloadError) as raised:
        instagram.probe_post_with_fallback("https://x/", tmp_path)

    # The error of the primary route is the one reported, and the cookies
    # keep their standing: a private or deleted post says nothing about them.
    assert raised.value is cookie_error
    assert session.use_cookies() is True
    assert session.take_alert() is False


def test_suspended_cookies_are_not_even_tried(monkeypatch, session, tmp_path):
    session.mark_rejected()
    session.take_alert()
    tried = fake_probe(monkeypatch, with_cookies=AssertionError("not reached"), without_cookies=INFO)

    assert instagram.probe_post_with_fallback("https://x/", tmp_path) == (INFO, False)
    assert tried == ["anonymous"]


def test_without_a_cookie_file_only_the_logged_out_route_is_tried(monkeypatch, clock, tmp_path):
    monkeypatch.setattr(instagram, "INSTAGRAM_SESSION", instagram.InstagramSession("", clock=clock))
    tried = fake_probe(monkeypatch, with_cookies=AssertionError("not reached"), without_cookies=INFO)

    assert instagram.probe_post_with_fallback("https://x/", tmp_path) == (INFO, False)
    assert tried == ["anonymous"]


def test_the_video_pass_takes_the_route_the_probe_took(monkeypatch, session, tmp_path):
    # Probing fell back to the logged-out route; downloading the videos with
    # the dead cookies again would fail all over.
    fake_probe(
        monkeypatch,
        with_cookies=DownloadError("dead session"),
        without_cookies={"entries": [{"id": "v", "formats": [{"url": "https://cdn/v.mp4"}]}]},
    )
    routes = []

    def download_post_videos(url, download_dir, entries, video_indices, use_cookies=True):
        routes.append(use_cookies)
        video = download_dir / "v.mp4"
        video.write_bytes(b"video")
        return {0: video}

    monkeypatch.setattr(instagram, "download_post_videos", download_post_videos)
    monkeypatch.setattr(media, "ensure_h264_video", lambda path, work_dir: path)
    monkeypatch.setattr(media, "add_audio_warning_if_needed", lambda caption, path: caption)

    instagram.download_post("https://www.instagram.com/p/ABC123/", tmp_path)

    assert routes == [False]


def test_the_video_pass_falls_back_on_its_own(monkeypatch, session, tmp_path):
    # Instagram does not answer a dead session the same way twice: the probe
    # can come through on the cookies - yt-dlp quietly dropping them itself -
    # and the video pass right after still fail on them.
    fake_probe(
        monkeypatch,
        with_cookies={"entries": [{"id": "v", "formats": [{"url": "https://cdn/v.mp4"}]}]},
        without_cookies=AssertionError("not reached"),
    )
    routes = []

    def download_post_videos(url, download_dir, entries, video_indices, use_cookies=True):
        routes.append(use_cookies)
        if use_cookies:
            raise DownloadError("ERROR: [Instagram] v: Failed to parse JSON")
        video = download_dir / "v.mp4"
        video.write_bytes(b"video")
        return {0: video}

    monkeypatch.setattr(instagram, "download_post_videos", download_post_videos)
    monkeypatch.setattr(media, "ensure_h264_video", lambda path, work_dir: path)
    monkeypatch.setattr(media, "add_audio_warning_if_needed", lambda caption, path: caption)

    items, _ = instagram.download_post("https://www.instagram.com/p/ABC123/", tmp_path)

    assert routes == [True, False]
    assert [item.kind for item in items] == ["video"]
    assert session.take_alert() is True


# --- yt-dlp options -------------------------------------------------------


def test_build_ydl_opts_leaves_the_cookies_out_when_told(monkeypatch, tmp_path):
    source = tmp_path / "source" / "cookies.txt"
    source.parent.mkdir()
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(config, "COOKIES_FILE", str(source))

    assert "cookiefile" in instagram.build_ydl_opts(work)
    assert "cookiefile" not in instagram.build_ydl_opts(work, use_cookies=False)


def test_build_ydl_opts_keeps_progress_bars_out_of_the_log(tmp_path):
    assert instagram.build_ydl_opts(tmp_path)["noprogress"] is True


# --- the alert ------------------------------------------------------------


class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


def test_the_alert_goes_to_the_storage_chat_once(monkeypatch, session):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100123")
    telegram = RecordingBot()
    context = SimpleNamespace(bot=telegram)

    session.mark_rejected()
    asyncio.run(preparation.alert_if_cookies_rejected(context))
    asyncio.run(preparation.alert_if_cookies_rejected(context))

    assert len(telegram.sent) == 1
    assert telegram.sent[0]["chat_id"] == -100123
    assert "INSTAGRAM_COOKIES_B64" in telegram.sent[0]["text"]


def test_no_alert_without_a_rejection(monkeypatch, session):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100123")
    telegram = RecordingBot()

    asyncio.run(preparation.alert_if_cookies_rejected(SimpleNamespace(bot=telegram)))

    assert telegram.sent == []


def test_the_alert_goes_out_even_when_the_post_then_fails(monkeypatch, session):
    # The cookies were turned down, the logged-out route answered, and the
    # post still failed further on. The owner should hear about the cookies
    # regardless of how that one post ended.
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100123")
    telegram = RecordingBot()

    def download_post(url, download_dir):
        instagram.INSTAGRAM_SESSION.mark_rejected()
        raise instagram.NoMediaInPostError("nothing to download")

    monkeypatch.setattr(instagram, "download_post", download_post)

    with pytest.raises(instagram.NoMediaInPostError):
        asyncio.run(preparation.download_post_in_thread("https://x/", Path("."), SimpleNamespace(bot=telegram)))

    assert len(telegram.sent) == 1
