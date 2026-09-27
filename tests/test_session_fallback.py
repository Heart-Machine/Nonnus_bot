"""Logged out first, the Instagram session as the fallback: which route a
post, a story and a highlight take, in what order, and that the videos follow
the route the probe came through.

yt-dlp is replaced by a stand-in for probe_post, so what gets checked is the
routing. Whether the session itself still works is the daily check's
business (test_canary).
"""
import logging

import pytest
from yt_dlp.utils import DownloadError

from nonnus import config, media, instagram

INFO = {"id": "post"}
POST = "https://www.instagram.com/p/ABC123/"
STORY = "https://www.instagram.com/stories/some.one/3994224585897789830/"
STORIES = "https://www.instagram.com/stories/some.one/"
HIGHLIGHT = "https://www.instagram.com/stories/highlights/18000000000000042/"


@pytest.fixture
def session(monkeypatch):
    """A bot deployed with cookies."""
    monkeypatch.setattr(config, "COOKIES_FILE", "cookies.txt")


def fake_probe(monkeypatch, *, with_session, logged_out):
    """Stand in for probe_post. Each outcome is an info dict to return or an
    exception to raise. Returns the list of routes tried, in order."""
    tried = []

    def probe_post(url, download_dir, use_cookies=True):
        tried.append("session" if use_cookies else "logged out")
        outcome = with_session if use_cookies else logged_out
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(instagram, "probe_post", probe_post)
    return tried


# --- posts ------------------------------------------------------------------


def test_a_post_goes_logged_out_and_leaves_the_session_alone(monkeypatch, session, tmp_path):
    tried = fake_probe(monkeypatch, with_session=AssertionError("not reached"), logged_out=INFO)

    assert instagram.probe_post_with_fallback(POST, tmp_path) == (INFO, False)
    assert tried == ["logged out"]


def test_a_post_instagram_will_not_serve_logged_out_goes_with_the_session(monkeypatch, session, tmp_path, caplog):
    tried = fake_probe(monkeypatch, with_session=INFO, logged_out=DownloadError("login required"))

    with caplog.at_level(logging.INFO, logger="nonnus.instagram"):
        assert instagram.probe_post_with_fallback(POST, tmp_path) == (INFO, True)

    assert tried == ["logged out", "session"]
    # Counted in the log: how often logged out is not enough from the server.
    assert f"Instagram served {POST} only to the session" in caplog.text


def test_a_post_failing_both_ways_reports_the_logged_out_error(monkeypatch, session, tmp_path):
    logged_out_error = DownloadError("private")
    fake_probe(monkeypatch, with_session=DownloadError("also private"), logged_out=logged_out_error)

    with pytest.raises(DownloadError) as raised:
        instagram.probe_post_with_fallback(POST, tmp_path)

    assert raised.value is logged_out_error


def test_without_a_session_a_post_goes_logged_out_alone(monkeypatch, tmp_path):
    logged_out_error = DownloadError("login required")
    tried = fake_probe(monkeypatch, with_session=AssertionError("not reached"), logged_out=logged_out_error)

    with pytest.raises(DownloadError) as raised:
        instagram.probe_post_with_fallback(POST, tmp_path)

    assert raised.value is logged_out_error
    assert tried == ["logged out"]


# --- stories and highlights ---------------------------------------------------


@pytest.mark.parametrize("url", [STORY, STORIES])
def test_a_story_goes_with_the_session_alone(monkeypatch, session, tmp_path, url):
    # Instagram shows stories logged in only: a logged-out try is one more
    # request for nothing.
    tried = fake_probe(monkeypatch, with_session=INFO, logged_out=AssertionError("not reached"))

    assert instagram.probe_post_with_fallback(url, tmp_path) == (INFO, True)
    assert tried == ["session"]


@pytest.mark.parametrize("url", [STORY, STORIES])
def test_a_story_the_session_does_not_get_is_not_tried_logged_out(monkeypatch, session, tmp_path, url):
    session_error = DownloadError("HTTP Error 429: Too Many Requests")
    tried = fake_probe(monkeypatch, with_session=session_error, logged_out=AssertionError("not reached"))

    with pytest.raises(DownloadError) as raised:
        instagram.probe_post_with_fallback(url, tmp_path)

    assert raised.value is session_error
    assert tried == ["session"]


def test_without_a_session_a_story_is_tried_logged_out(monkeypatch, tmp_path):
    # Nothing else to try it with; the failure tells the user what they can
    # be told.
    tried = fake_probe(monkeypatch, with_session=AssertionError("not reached"), logged_out=DownloadError("login"))

    with pytest.raises(DownloadError):
        instagram.probe_post_with_fallback(STORY, tmp_path)

    assert tried == ["logged out"]


def test_a_highlight_goes_the_way_a_post_does(monkeypatch, session, tmp_path):
    # Some accounts' highlights open logged out.
    tried = fake_probe(monkeypatch, with_session=AssertionError("not reached"), logged_out=INFO)
    assert instagram.probe_post_with_fallback(HIGHLIGHT, tmp_path) == (INFO, False)
    assert tried == ["logged out"]

    tried = fake_probe(monkeypatch, with_session=INFO, logged_out=DownloadError("login required"))
    assert instagram.probe_post_with_fallback(HIGHLIGHT, tmp_path) == (INFO, True)
    assert tried == ["logged out", "session"]


# --- the video pass -------------------------------------------------------------


@pytest.mark.parametrize(
    "logged_out, with_session, used_session",
    [
        ("probed", AssertionError("not reached"), False),
        (DownloadError("login required"), "probed", True),
    ],
)
def test_the_video_pass_goes_the_way_the_probe_came_through(monkeypatch, session, tmp_path, logged_out, with_session,
                                                            used_session):
    # From the probe's own result, so there is no second API request for
    # Instagram to answer differently - that used to be where "an empty
    # media response" struck, a second after the probe had worked - and with
    # the session only if the probe needed it.
    probed = {"entries": [{"id": "v", "formats": [{"url": "https://cdn/v.mp4"}]}]}
    fake_probe(
        monkeypatch,
        logged_out=probed if logged_out == "probed" else logged_out,
        with_session=probed if with_session == "probed" else with_session,
    )
    calls = []

    def download_post_videos(info, download_dir, entries, video_indices, use_cookies=True):
        calls.append((info, use_cookies))
        video = download_dir / "v.mp4"
        video.write_bytes(b"video")
        return {0: video}

    monkeypatch.setattr(instagram, "download_post_videos", download_post_videos)
    monkeypatch.setattr(media, "ensure_h264_video", lambda path, work_dir: path)
    monkeypatch.setattr(media, "add_audio_warning_if_needed", lambda caption, path: caption)

    instagram.download_post(POST, tmp_path)

    assert calls == [(probed, used_session)]


# --- the session on its own -------------------------------------------------------


def test_the_session_is_checked_with_the_session_alone(monkeypatch, session, tmp_path):
    tried = fake_probe(monkeypatch, with_session=INFO, logged_out=AssertionError("not reached"))

    assert instagram.session_error(POST, tmp_path) is None
    assert tried == ["session"]


def test_a_session_instagram_turns_away_is_said_to_be(monkeypatch, session, tmp_path):
    turned_away = DownloadError("HTTP Error 429: Too Many Requests")
    tried = fake_probe(monkeypatch, with_session=turned_away, logged_out=AssertionError("not reached"))

    assert instagram.session_error(POST, tmp_path) is turned_away
    assert tried == ["session"]


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
