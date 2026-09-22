"""The "Посмотреть карусель" button under inline carousel messages, and the
/start deep link it opens.

The handler tests drive the real coroutines with stand-ins for Telegram, so
what gets checked is the decision each one makes: whether a chosen inline
result has its media swapped, with which button, and what /start does with
its parameter.
"""
import asyncio
from types import SimpleNamespace

import pytest

import bot

POST_URL = "https://www.instagram.com/p/ABC123/"
BOT_USERNAME = "nonnus_bot"
CAROUSEL = [{"type": "photo", "file_id": "p1"}, {"type": "video", "file_id": "v1"}]
SINGLE = [{"type": "video", "file_id": "v1"}]


def cached_post(items):
    return {"caption": "caption", "title": "@someone", "items": items}


# --- the deep-link payload ------------------------------------------------


@pytest.mark.parametrize(
    "url, payload",
    [
        ("https://www.instagram.com/p/ABC123/", "p_ABC123"),
        ("https://www.instagram.com/reel/ABC123/", "reel_ABC123"),
        ("https://instagr.am/p/ABC123", "p_ABC123"),
        ("https://www.instagram.com/p/ABC123/?igsh=xyz", "p_ABC123"),
        ("https://www.instagram.com/p/A_b-9/", "p_A_b-9"),
    ],
)
def test_post_start_payload_names_the_post(url, payload):
    assert bot.post_start_payload(url) == payload


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/reel/ABC123/",
        "https://www.instagram.com/reels/ABC123/",
        "https://www.instagram.com/tv/ABC123/",
        # An underscore inside the shortcode must not be taken for the
        # separator between type and shortcode.
        "https://www.instagram.com/p/A_b-9/",
    ],
)
def test_payload_round_trips_to_the_normalized_url(url):
    payload = bot.post_start_payload(url)
    assert bot.post_url_from_start_payload(payload) == bot.normalize_post_url(url)


def test_post_start_payload_refuses_a_shortcode_that_would_not_fit():
    url = f"https://www.instagram.com/p/{'A' * 70}/"
    assert bot.post_start_payload(url) is None


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "p",
        "p_",
        "story_ABC123",  # not a post type
        "p_ABC 123",  # outside the shortcode alphabet
        "p_ABC/../x",
        "../etc_passwd",
    ],
)
def test_post_url_from_start_payload_rejects_anything_that_is_not_a_post(payload):
    assert bot.post_url_from_start_payload(payload) is None


# --- the button -----------------------------------------------------------


def test_carousel_keyboard_links_to_the_bot_with_the_post():
    button = bot.carousel_keyboard(POST_URL, BOT_USERNAME).inline_keyboard[0][0]

    assert button.text == "Посмотреть карусель"
    assert button.url == "https://t.me/nonnus_bot?start=p_ABC123"


def test_carousel_keyboard_needs_a_bot_username():
    assert bot.carousel_keyboard(POST_URL, "") is None


def test_every_carousel_result_carries_the_button():
    results = bot.build_inline_results(POST_URL, cached_post(CAROUSEL), BOT_USERNAME)
    urls = [result.reply_markup.inline_keyboard[0][0].url for result in results]

    assert urls == ["https://t.me/nonnus_bot?start=p_ABC123"] * 2


def test_a_single_file_result_has_no_button():
    # Without a button Telegram sends no inline_message_id when it is chosen,
    # which is what keeps a single cached file out of the chosen handler.
    results = bot.build_inline_results(POST_URL, cached_post(SINGLE), BOT_USERNAME)

    assert results[0].reply_markup is None


# --- choosing an inline result ------------------------------------------


class RecordingBot:
    def __init__(self):
        self.media_edits = []

    async def edit_message_media(self, **kwargs):
        self.media_edits.append(kwargs)

    async def edit_message_caption(self, **kwargs):
        raise AssertionError("preparation was not expected to fail here")


def choose(monkeypatch, result_id, items):
    """Run handle_chosen_inline_result for POST_URL with `items` in the cache
    and return the media edits it made."""

    async def get_bot_username(context):
        return BOT_USERNAME

    monkeypatch.setattr(bot, "get_cached_inline_result", lambda url: cached_post(items))
    monkeypatch.setattr(bot, "get_bot_username", get_bot_username)

    telegram = RecordingBot()
    update = SimpleNamespace(
        chosen_inline_result=SimpleNamespace(
            result_id=result_id,
            inline_message_id="inline-message",
            query=POST_URL,
        )
    )
    asyncio.run(bot.handle_chosen_inline_result(update, SimpleNamespace(bot=telegram)))
    return telegram.media_edits


def test_a_chosen_carousel_result_is_left_alone(monkeypatch):
    # It is final already. Swapping it would re-set the same file and strip
    # the button the moment the message was sent.
    assert choose(monkeypatch, bot.inline_item_result_id(POST_URL, 1, 2), CAROUSEL) == []


def test_the_first_carousel_result_is_not_mistaken_for_the_placeholder(monkeypatch):
    assert choose(monkeypatch, bot.inline_item_result_id(POST_URL, 0, 2), CAROUSEL) == []


def test_a_carousel_placeholder_gets_its_first_file_and_the_button(monkeypatch):
    edits = choose(monkeypatch, bot.inline_result_id(POST_URL), CAROUSEL)

    assert len(edits) == 1
    assert edits[0]["media"].media == "p1"
    assert edits[0]["reply_markup"].inline_keyboard[0][0].text == "Посмотреть карусель"


def test_a_single_file_placeholder_is_swapped_without_a_button(monkeypatch):
    edits = choose(monkeypatch, bot.inline_result_id(POST_URL), SINGLE)

    assert len(edits) == 1
    assert edits[0]["media"].media == "v1"
    assert edits[0]["reply_markup"] is None


# --- /start ---------------------------------------------------------------


class RecordingMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def run_start(monkeypatch, args):
    delivered = []

    async def deliver_post(message, url, context):
        delivered.append(url)

    monkeypatch.setattr(bot, "deliver_post", deliver_post)
    message = RecordingMessage()
    asyncio.run(bot.start(SimpleNamespace(message=message), SimpleNamespace(args=args)))
    return delivered, message.replies


def test_start_with_a_post_payload_delivers_the_post(monkeypatch):
    delivered, replies = run_start(monkeypatch, ["p_ABC123"])

    assert delivered == [POST_URL]
    assert replies == []


def test_start_without_a_payload_explains_itself(monkeypatch):
    delivered, replies = run_start(monkeypatch, [])

    assert delivered == []
    assert len(replies) == 1


def test_start_with_a_forged_payload_falls_back_to_the_help_text(monkeypatch):
    delivered, replies = run_start(monkeypatch, ["../etc_passwd"])

    assert delivered == []
    assert len(replies) == 1
