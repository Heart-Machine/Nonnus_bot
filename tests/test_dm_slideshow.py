"""A carousel sent to a chat as one slideshow message instead of albums.

python-telegram-bot has no method for sendRichMessage, so it goes through
Bot.do_api_request. Most tests use a stand-in bot; the last one runs the
library's real Bot with only the network layer replaced, so what it checks is
the request that would actually reach Telegram.
"""
import asyncio
import json
from types import SimpleNamespace

from telegram import Bot
from telegram.error import BadRequest
from telegram.request import BaseRequest

from nonnus import delivery

POST_URL = "https://www.instagram.com/p/ABC123/"
CAROUSEL = [{"type": "photo", "file_id": "p1"}, {"type": "video", "file_id": "v1"}]


def cached_post(items):
    return {"caption": "caption", "title": "@someone", "items": items}


class RecordingBot:
    def __init__(self, refuse=False):
        self.api_calls = []
        self.refuse = refuse

    async def do_api_request(self, endpoint, api_kwargs=None, **kwargs):
        self.api_calls.append((endpoint, api_kwargs))
        if self.refuse:
            raise BadRequest("rich messages are not available here")
        return {"message_id": 100}


class FakeMessage:
    def __init__(self, chat_type="private", bot_instance=None, is_topic_message=False, message_thread_id=None):
        self.chat_id = 42
        self.message_id = 7
        self.chat = SimpleNamespace(type=chat_type)
        self.is_topic_message = is_topic_message
        self.message_thread_id = message_thread_id
        self.bot_instance = bot_instance or RecordingBot()
        self.replies = []

    def get_bot(self):
        return self.bot_instance

    async def reply_media_group(self, media, **kwargs):
        self.replies.append(("album", [item.media for item in media]))

    async def reply_photo(self, photo, **kwargs):
        self.replies.append(("photo", photo))

    async def reply_video(self, video, **kwargs):
        self.replies.append(("video", video))

    async def reply_document(self, document, **kwargs):
        self.replies.append(("document", document))


def send(message, items):
    asyncio.run(delivery.send_prepared_result(message, cached_post(items), POST_URL))


def test_a_carousel_goes_out_as_one_slideshow():
    message = FakeMessage()
    send(message, CAROUSEL)

    assert message.bot_instance.api_calls == [
        (
            "sendRichMessage",
            {"chat_id": 42, "rich_message": delivery.carousel_slideshow_message(POST_URL, cached_post(CAROUSEL))},
        )
    ]
    assert message.replies == []


def test_in_a_group_the_slideshow_quotes_the_link_it_answers():
    # The same as reply_* does by default: quote outside a private chat.
    message = FakeMessage(chat_type="supergroup")
    send(message, CAROUSEL)

    _, api_kwargs = message.bot_instance.api_calls[0]
    assert api_kwargs["reply_parameters"] == {"message_id": 7}


def test_in_a_forum_topic_the_slideshow_stays_in_the_topic():
    message = FakeMessage(chat_type="supergroup", is_topic_message=True, message_thread_id=55)
    send(message, CAROUSEL)

    _, api_kwargs = message.bot_instance.api_calls[0]
    assert api_kwargs["message_thread_id"] == 55


def test_a_refused_slideshow_falls_back_to_albums():
    message = FakeMessage(bot_instance=RecordingBot(refuse=True))
    send(message, CAROUSEL)

    assert message.replies == [("album", ["p1", "v1"])]


def test_a_single_file_is_sent_as_itself():
    message = FakeMessage()
    send(message, [{"type": "video", "file_id": "v1"}])

    assert message.bot_instance.api_calls == []
    assert message.replies == [("video", "v1")]


def test_a_carousel_with_a_document_goes_out_as_albums():
    # A document cannot be a slide, so the post is not offered as a
    # slideshow at all rather than shown with a file missing. Nor can it
    # share an album with photos and videos - Telegram refuses the album -
    # so it goes out on its own after them.
    message = FakeMessage()
    send(message, CAROUSEL + [{"type": "document", "file_id": "d1"}])

    assert message.bot_instance.api_calls == []
    assert message.replies == [("album", ["p1", "v1"]), ("document", "d1")]


class RecordingRequest(BaseRequest):
    """The network layer of python-telegram-bot, replaced so the real Bot can
    run: records what it would have sent and answers like Telegram would."""

    def __init__(self):
        self.sent = []

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **timeouts):
        self.sent.append((url, request_data.json_parameters if request_data else {}))
        return 200, json.dumps({"ok": True, "result": {"message_id": 100}}).encode()


def test_the_request_that_reaches_telegram():
    request = RecordingRequest()
    real_bot = Bot("1:test", request=request, get_updates_request=RecordingRequest())
    message = FakeMessage(chat_type="group", bot_instance=real_bot)

    send(message, CAROUSEL)

    url, parameters = request.sent[0]
    assert url.endswith("/sendRichMessage")
    assert parameters["chat_id"] == "42"
    assert json.loads(parameters["reply_parameters"]) == {"message_id": 7}
    # The library JSON-encodes the nested dict itself; it arrives intact.
    assert json.loads(parameters["rich_message"]) == delivery.carousel_slideshow_message(
        POST_URL, cached_post(CAROUSEL)
    )
