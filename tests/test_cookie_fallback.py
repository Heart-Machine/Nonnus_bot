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


def reject_in_a_row(session):
    """As many rejections in a row as it takes to set the cookies aside."""
    for _ in range(session.rejections_to_suspend):
        session.mark_rejected()


# --- the session state ----------------------------------------------------


def test_cookies_are_used_while_trusted(session):
    assert session.use_cookies() is True


def test_no_cookie_file_means_no_cookies(clock):
    assert instagram.InstagramSession("", clock=clock).use_cookies() is False


def test_it_takes_two_rejections_in_a_row(session):
    assert session.rejections_to_suspend == 2

    assert session.mark_rejected() is False
    assert session.use_cookies() is True
    assert session.take_alert() is False

    assert session.mark_rejected() is True
    assert session.use_cookies() is False


def test_a_request_the_cookies_worked_for_starts_the_count_over(session):
    # A one-off failure, then the cookies work, then another one-off: two
    # rejections, but not in a row.
    session.mark_rejected()
    session.mark_accepted()
    session.mark_rejected()

    assert session.use_cookies() is True
    assert session.take_alert() is False


def test_rejections_in_a_row_suspend_the_cookies_for_a_while(session, clock):
    reject_in_a_row(session)
    assert session.use_cookies() is False

    clock.now += 59
    assert session.use_cookies() is False

    clock.now += 1
    assert session.use_cookies() is True


def test_once_tried_again_it_takes_a_full_count_to_set_them_aside_again(session, clock):
    reject_in_a_row(session)
    clock.now += 60

    session.mark_rejected()

    assert session.use_cookies() is True


def test_setting_the_cookies_aside_raises_the_alert_exactly_once(session):
    reject_in_a_row(session)

    assert session.take_alert() is True
    assert session.take_alert() is False


def test_alerts_are_rate_limited(session, clock):
    reject_in_a_row(session)
    session.take_alert()

    clock.now += 300
    reject_in_a_row(session)
    assert session.take_alert() is False

    clock.now += 300
    reject_in_a_row(session)
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


def test_turned_down_cookies_fall_back_but_are_kept_after_one_rejection(monkeypatch, session, tmp_path):
    # A dead session surfaces from yt-dlp as a JSON parse error, not as
    # anything about logging in - which is why nothing keys off the wording.
    tried = fake_probe(
        monkeypatch,
        with_cookies=DownloadError("ERROR: [Instagram] X: Failed to parse JSON"),
        without_cookies=INFO,
    )

    assert instagram.probe_post_with_fallback("https://x/", tmp_path) == (INFO, False)
    assert tried == ["cookies", "anonymous"]
    # It may have been a timeout or a rate limit that cleared in between.
    assert session.use_cookies() is True
    assert session.take_alert() is False


def test_cookies_turned_down_on_two_requests_in_a_row_are_set_aside_with_an_alert(monkeypatch, session, tmp_path):
    tried = fake_probe(
        monkeypatch,
        with_cookies=DownloadError("ERROR: [Instagram] X: Failed to parse JSON"),
        without_cookies=INFO,
    )

    for _ in range(3):
        assert instagram.probe_post_with_fallback("https://x/", tmp_path) == (INFO, False)

    # The third request no longer tries the cookies at all.
    assert tried == ["cookies", "anonymous", "cookies", "anonymous", "anonymous"]
    assert session.use_cookies() is False
    assert session.take_alert() is True


def test_cookies_that_work_in_between_are_not_set_aside(monkeypatch, session, tmp_path):
    outcomes = iter([DownloadError("timed out"), INFO, DownloadError("rate limited")])

    def probe_post(url, download_dir, use_cookies=True):
        if not use_cookies:
            return INFO
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(instagram, "probe_post", probe_post)

    for _ in range(3):
        instagram.probe_post_with_fallback("https://x/", tmp_path)

    assert session.use_cookies() is True
    assert session.take_alert() is False


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
    reject_in_a_row(session)
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
    # Probing fell back to the logged-out route. The cookies are still in use
    # after one rejection, but not for this post: trying them again on its
    # videos would only fail again, and count a second time for one post.
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
    assert session.use_cookies() is True


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
    # The probe worked on the cookies and the video pass did not: one
    # rejection, not two in a row.
    assert session.use_cookies() is True
    assert session.take_alert() is False


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

    reject_in_a_row(session)
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
        reject_in_a_row(instagram.INSTAGRAM_SESSION)
        raise instagram.NoMediaInPostError("nothing to download")

    monkeypatch.setattr(instagram, "download_post", download_post)

    with pytest.raises(instagram.NoMediaInPostError):
        asyncio.run(preparation.download_post_in_thread("https://x/", Path("."), SimpleNamespace(bot=telegram)))

    assert len(telegram.sent) == 1
