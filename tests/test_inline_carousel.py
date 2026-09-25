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
from telegram import Bot

from nonnus import links, cache, delivery, inline, handlers

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
        # Without the username dropped this would be three path segments and
        # no payload at all - no "Посмотреть карусель" button.
        ("https://www.instagram.com/someone/p/ABC123/", "p_ABC123"),
    ],
)
def test_post_start_payload_names_the_post(url, payload):
    assert links.post_start_payload(url) == payload


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
    payload = links.post_start_payload(url)
    assert links.post_url_from_start_payload(payload) == links.normalize_post_url(url)


def test_post_start_payload_refuses_a_shortcode_that_would_not_fit():
    url = f"https://www.instagram.com/p/{'A' * 70}/"
    assert links.post_start_payload(url) is None


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
    assert links.post_url_from_start_payload(payload) is None


# --- the button -----------------------------------------------------------


def test_carousel_keyboard_links_to_the_bot_with_the_post():
    button = inline.carousel_keyboard(POST_URL, BOT_USERNAME).inline_keyboard[0][0]

    assert button.text == "Посмотреть карусель"
    assert button.url == "https://t.me/nonnus_bot?start=p_ABC123"


def test_carousel_keyboard_needs_a_bot_username():
    assert inline.carousel_keyboard(POST_URL, "") is None


def test_every_per_file_carousel_result_carries_the_button():
    results = inline.build_inline_results(POST_URL, cached_post(CAROUSEL), BOT_USERNAME)
    per_file = [result for result in results if result.type != "article"]
    urls = [result.reply_markup.inline_keyboard[0][0].url for result in per_file]

    assert urls == ["https://t.me/nonnus_bot?start=p_ABC123"] * 2


def test_a_single_file_result_has_no_button():
    # Without a button Telegram sends no inline_message_id when it is chosen,
    # which is what keeps a single cached file out of the chosen handler.
    results = inline.build_inline_results(POST_URL, cached_post(SINGLE), BOT_USERNAME)

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

    monkeypatch.setattr(cache, "get_cached_inline_result", lambda url: cached_post(items))
    monkeypatch.setattr(delivery, "get_bot_username", get_bot_username)

    telegram = RecordingBot()
    update = SimpleNamespace(
        chosen_inline_result=SimpleNamespace(
            result_id=result_id,
            from_user=SimpleNamespace(id=1, username="someone", first_name="Someone"),
            inline_message_id="inline-message",
            query=POST_URL,
        )
    )
    asyncio.run(inline.handle_chosen_inline_result(update, SimpleNamespace(bot=telegram)))
    return telegram.media_edits


def test_a_chosen_carousel_result_is_left_alone(monkeypatch):
    # It is final already. Swapping it would re-set the same file and strip
    # the button the moment the message was sent.
    assert choose(monkeypatch, inline.inline_item_result_id(POST_URL, 1, 2), CAROUSEL) == []


def test_the_first_carousel_result_is_not_mistaken_for_the_placeholder(monkeypatch):
    assert choose(monkeypatch, inline.inline_item_result_id(POST_URL, 0, 2), CAROUSEL) == []


def test_a_carousel_placeholder_gets_its_first_file_and_the_button(monkeypatch):
    edits = choose(monkeypatch, inline.inline_result_id(POST_URL), CAROUSEL)

    assert len(edits) == 1
    assert edits[0]["media"].media == "p1"
    assert edits[0]["reply_markup"].inline_keyboard[0][0].text == "Посмотреть карусель"


def test_a_single_file_placeholder_is_swapped_without_a_button(monkeypatch):
    edits = choose(monkeypatch, inline.inline_result_id(POST_URL), SINGLE)

    assert len(edits) == 1
    assert edits[0]["media"].media == "v1"
    assert edits[0]["reply_markup"] is None


# --- /start ---------------------------------------------------------------


class RecordingMessage:
    from_user = SimpleNamespace(id=1, username="someone", first_name="Someone")

    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def run_start(monkeypatch, args):
    delivered = []

    async def deliver_post(message, url, context):
        delivered.append(url)

    monkeypatch.setattr(handlers, "deliver_post", deliver_post)
    message = RecordingMessage()
    asyncio.run(handlers.start(SimpleNamespace(message=message), SimpleNamespace(args=args)))
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


# --- the whole carousel as one message ----------------------------------


def test_slideshow_keeps_every_file_in_carousel_order():
    slideshow = delivery.carousel_slideshow_message(POST_URL, cached_post(CAROUSEL))["blocks"][0]

    assert slideshow["type"] == "slideshow"
    assert slideshow["blocks"] == [
        {"type": "photo", "photo": {"type": "photo", "media": "p1"}},
        {"type": "video", "video": {"type": "video", "media": "v1"}},
    ]


def test_slideshow_caption_links_the_author_to_the_post():
    caption = delivery.carousel_slideshow_message(POST_URL, cached_post(CAROUSEL))["blocks"][0]["caption"]

    assert caption == {"text": ["Пост ", {"type": "url", "text": "@someone", "url": POST_URL}]}


def test_slideshow_caption_falls_back_to_the_url_without_an_author():
    cached = {**cached_post(CAROUSEL), "title": "Instagram"}
    caption = delivery.carousel_slideshow_message(POST_URL, cached)["blocks"][0]["caption"]

    assert caption["text"][1]["text"] == POST_URL


def test_slideshow_keeps_caption_notes_as_plain_paragraphs():
    # Notes were escaped for Telegram's HTML parse mode; a rich block wants
    # the plain text back.
    cached = {**cached_post(CAROUSEL), "caption": '<a href="x">@someone</a>\n\nВидео &quot;сжато&quot;.'}
    blocks = delivery.carousel_slideshow_message(POST_URL, cached)["blocks"]

    assert blocks[1:] == [{"type": "paragraph", "text": 'Видео "сжато".'}]


def test_no_slideshow_for_a_single_file():
    assert delivery.carousel_slideshow_message(POST_URL, cached_post(SINGLE)) is None


def test_no_slideshow_when_a_file_went_out_as_a_document():
    items = CAROUSEL + [{"type": "document", "file_id": "d1"}]

    assert delivery.carousel_slideshow_message(POST_URL, cached_post(items)) is None


def test_no_slideshow_past_the_media_limit():
    items = [{"type": "photo", "file_id": f"p{n}"} for n in range(delivery.RICH_MESSAGE_MEDIA_LIMIT + 1)]

    assert delivery.carousel_slideshow_message(POST_URL, cached_post(items)) is None


def test_carousel_results_open_with_the_slideshow():
    results = inline.build_inline_results(POST_URL, cached_post(CAROUSEL), BOT_USERNAME)
    slideshow = results[0]

    assert [result.type for result in results] == ["article", "photo", "video"]
    assert slideshow.title == "Вся карусель одним сообщением"
    assert slideshow.id == f"{inline.inline_result_id(POST_URL)}-all"
    assert slideshow.reply_markup is None


def test_slideshow_result_serialises_the_way_the_bot_api_expects():
    # Put through the same preparation answer_inline_query applies, so this is
    # the JSON that actually goes out. It leans on that private method on
    # purpose: the custom content class only works because this step leaves
    # it alone, so a change there should fail loudly here.
    result = inline.build_inline_results(POST_URL, cached_post(CAROUSEL), BOT_USERNAME)[0]
    prepared = Bot("1:test")._insert_defaults_for_ilq_results(result).to_dict()

    assert prepared["type"] == "article"
    assert prepared["input_message_content"] == {
        "rich_message": delivery.carousel_slideshow_message(POST_URL, cached_post(CAROUSEL))
    }


def test_single_file_post_gets_no_slideshow_result():
    results = inline.build_inline_results(POST_URL, cached_post(SINGLE), BOT_USERNAME)

    assert [result.type for result in results] == ["video"]


def test_choosing_the_slideshow_leaves_the_message_alone(monkeypatch):
    assert choose(monkeypatch, f"{inline.inline_result_id(POST_URL)}-all", CAROUSEL) == []
