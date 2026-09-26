"""The inline placeholder says what is coming as far as the link tells: a
/reel/ link is a reel, anything else - a /p/ link can be photos, a carousel
or a video - is a post.
"""
import asyncio
from types import SimpleNamespace

import pytest

from nonnus import config, inline, links, preparation


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/reel/ABC123/",
        "https://www.instagram.com/reels/ABC123/",
        "https://www.instagram.com/tv/ABC123/",
        "https://www.instagram.com/someone/reel/ABC123/",
        "https://m.instagram.com/reel/ABC123/?igsh=xyz",
        "https://instagr.am/reel/ABC123",
    ],
)
def test_reel_links_are_reels(url):
    assert links.is_reel_link(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/someone/p/ABC123/",
        # Someone called "reel", sharing a post from their profile.
        "https://www.instagram.com/reel/p/ABC123/",
    ],
)
def test_other_links_are_posts(url):
    assert not links.is_reel_link(url)


def test_the_two_kinds_have_pictures_of_their_own():
    reel, post = inline.PLACEHOLDERS["reel"].image, inline.PLACEHOLDERS["post"].image

    assert reel.startswith(b"\xff\xd8") and post.startswith(b"\xff\xd8")
    assert reel != post


@pytest.mark.parametrize(
    "url, kind, title, caption",
    [
        ("https://www.instagram.com/reel/ABC123/", "reel", "Готовлю рилс...",
         "Готовлю рилс, подожди немного — сообщение обновится само..."),
        ("https://www.instagram.com/p/ABC123/", "post", "Готовлю пост...",
         "Готовлю пост, подожди немного — сообщение обновится само..."),
    ],
)
def test_a_new_link_gets_the_placeholder_of_its_kind(monkeypatch, url, kind, title, caption):
    result = answer_inline_query(monkeypatch, url, lambda kind: f"picture-{kind}")

    assert result.photo_file_id == f"picture-{kind}"
    assert result.title == title
    assert result.caption == caption


def test_without_a_picture_the_text_placeholder_names_the_kind_too(monkeypatch):
    result = answer_inline_query(monkeypatch, "https://www.instagram.com/reel/ABC123/", lambda kind: None)

    assert result.title == "Готовлю рилс..."


def answer_inline_query(monkeypatch, url, picture):
    """Answer an inline query for a post that is not ready yet; return the
    one result offered. `picture` gives the placeholder's file_id by kind."""
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")

    async def get_placeholder_photo_file_id(context, kind):
        return picture(kind)

    monkeypatch.setattr(inline, "get_placeholder_photo_file_id", get_placeholder_photo_file_id)
    answers = []

    async def answer(results, **kwargs):
        answers.append(results)

    async def run():
        # A preparation still under way.
        pending = asyncio.get_running_loop().create_future()
        monkeypatch.setattr(preparation, "get_or_create_prepare_task", lambda url, context, reuse_failure=False, user=None: pending)
        query = SimpleNamespace(query=url, answer=answer, from_user=SimpleNamespace(id=1, username="someone", first_name="Someone"))
        await inline.handle_inline_query(SimpleNamespace(inline_query=query), SimpleNamespace())

    asyncio.run(run())

    [results] = answers
    [result] = results
    return result
