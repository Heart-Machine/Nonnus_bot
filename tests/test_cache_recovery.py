"""Recovering when a cached post can no longer be sent, and not hammering
Instagram when a post cannot be prepared at all.

A file_id belongs to the bot that uploaded the file, so a cache kept under
another bot token is full of dead ones - and a dead file_id used to stay in
the cache for good, every request failing on it. The other half: a failed
preparation is kept for a minute, so that the inline query sent on every
keystroke answers with the failure instead of downloading again.

Downloads and uploads are stand-ins that count their calls; what runs for
real is the bot's own flow around them.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from nonnus import cache, config, delivery, handlers, inline, instagram, media, preparation

POST_URL = "https://www.instagram.com/p/ABC123/"
DEAD_FILE_ID = BadRequest("Bad Request: wrong file identifier/HTTP URL specified")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The bot with a storage chat, whose Instagram downloads, storage
    uploads and sends are stand-ins. `send_error` is raised by any send of a
    file_id listed in `failing_file_ids`; `download_error`, if set, by every
    download."""
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    state = SimpleNamespace(
        downloads=0,
        uploads=0,
        sends=[],
        failing_file_ids={"stale"},
        send_error=DEAD_FILE_ID,
        download_error=None,
    )

    def download_post(url, download_dir):
        state.downloads += 1
        if state.download_error:
            raise state.download_error
        path = download_dir / "photo.jpg"
        path.write_bytes(b"photo")
        return [media.MediaItem(path, "photo")], "caption"

    async def upload_items_to_storage(context, items, caption):
        state.uploads += 1
        return [{"type": "photo", "file_id": f"fresh{state.uploads}"}]

    async def send_prepared_result(message, cached_result, url):
        file_id = cached_result["items"][0]["file_id"]
        state.sends.append(file_id)
        if file_id in state.failing_file_ids:
            raise state.send_error

    monkeypatch.setattr(instagram, "download_post", download_post)
    monkeypatch.setattr(delivery, "upload_items_to_storage", upload_items_to_storage)
    monkeypatch.setattr(delivery, "send_prepared_result", send_prepared_result)
    return state


def cache_stale_post():
    cache.save_cached_inline_result(POST_URL, {"caption": "c", "title": "t", "items": [{"type": "photo", "file_id": "stale"}]})


def cached_file_id():
    cached = cache.get_cached_inline_result(POST_URL)
    return cached["items"][0]["file_id"] if cached else None


class Context:
    """What the handlers use of CallbackContext: the application's shared
    data and task factory, and a bot."""

    def __init__(self, loop, bot=None):
        self.application = SimpleNamespace(bot_data={"bot_username": "nonnus_bot"}, create_task=loop.create_task)
        self.bot = bot or SimpleNamespace(send_chat_action=_no_op)

    async def settle(self):
        """Wait for every preparation this context started."""
        for task in list(self.application.bot_data.get("inline_tasks", {}).values()):
            await asyncio.gather(task, return_exceptions=True)


async def _no_op(**kwargs):
    pass


class Status:
    def __init__(self):
        self.text = None
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.text = text

    async def delete(self):
        self.deleted = True


class Message:
    chat_id = 1
    from_user = SimpleNamespace(id=1, username="someone", first_name="Someone")

    def __init__(self):
        self.status = Status()

    async def reply_text(self, text, **kwargs):
        return self.status


def send_link(context=None):
    """The link sent to the bot directly; returns the status message."""

    async def run():
        nonlocal context
        context = context or Context(asyncio.get_running_loop())
        message = Message()
        await handlers.deliver_post(message, POST_URL, context)
        return message.status

    return asyncio.run(run())


# --- telling dead file_ids from other failures -------------------------


@pytest.mark.parametrize(
    "error",
    [
        BadRequest("Bad Request: wrong file identifier/HTTP URL specified"),
        BadRequest("Bad Request: wrong remote file identifier specified: can't unserialize it"),
        BadRequest("Bad Request: FILE_REFERENCE_EXPIRED"),
        BadRequest("Bad Request: type of file mismatch"),
        BadRequest("Bad Request: MEDIA_EMPTY"),
    ],
)
def test_dead_file_ids_are_recognised(error):
    assert delivery.is_dead_file_id_error(error)


@pytest.mark.parametrize(
    "error",
    [
        BadRequest("Bad Request: not enough rights to send photos to the chat"),
        BadRequest("Bad Request: chat not found"),
        TimedOut(),
        NetworkError("wrong file identifier"),  # only a BadRequest is Telegram refusing the request
    ],
)
def test_other_failures_are_not_taken_for_dead_file_ids(error):
    assert not delivery.is_dead_file_id_error(error)


# --- a link sent to the bot ---------------------------------------------


def test_dead_file_ids_are_dropped_and_the_post_downloaded_again(world):
    cache_stale_post()

    status = send_link()

    assert world.downloads == 1
    assert world.sends == ["stale", "fresh1"]
    assert status.deleted
    assert cached_file_id() == "fresh1"


def test_a_timeout_is_not_sent_again(world):
    # The post may well have arrived; sending again could deliver it twice.
    cache_stale_post()
    world.send_error = TimedOut()

    status = send_link()

    assert world.sends == ["stale"]
    assert world.downloads == 0
    assert status.text == handlers.SEND_FAILED_TEXT
    assert cached_file_id() == "stale"


def test_a_refusal_over_the_chat_leaves_the_cache_alone(world):
    # Missing rights in a group: a new upload would be refused the same way.
    cache_stale_post()
    world.send_error = BadRequest("Bad Request: not enough rights to send photos to the chat")

    status = send_link()

    assert world.sends == ["stale"]
    assert world.downloads == 0
    assert status.text == handlers.SEND_FAILED_TEXT
    assert cached_file_id() == "stale"


# --- inline mode --------------------------------------------------------


class InlineQuery:
    """Records every answer; the first `refusals` answers are refused with
    `error`."""

    def __init__(self, refusals=0, error=DEAD_FILE_ID):
        self.query = POST_URL
        self.from_user = SimpleNamespace(id=1, username="someone", first_name="Someone")
        self.answers = []
        self.refusals = refusals
        self.error = error

    async def answer(self, results, **kwargs):
        self.answers.append(results)
        if len(self.answers) <= self.refusals:
            raise self.error


@pytest.fixture
def placeholder(monkeypatch):
    async def placeholder_file_id(context, kind):
        return "placeholder"

    monkeypatch.setattr(inline, "get_placeholder_photo_file_id", placeholder_file_id)


def run_inline(*queries):
    """Send the inline queries one after another through one context, each
    preparation it starts allowed to finish before the next query."""

    async def run():
        context = Context(asyncio.get_running_loop())
        for query in queries:
            await inline.handle_inline_query(SimpleNamespace(inline_query=query), context)
            await context.settle()
        return context

    return asyncio.run(run())


def answered_with(query, index=-1):
    return [(result.type, result.id) for result in query.answers[index]]


def test_an_inline_answer_refused_over_dead_files_falls_back_to_the_placeholder(world, placeholder):
    cache_stale_post()
    query = InlineQuery(refusals=1)

    run_inline(query)

    assert len(query.answers) == 2
    assert answered_with(query) == [("photo", inline.inline_result_id(POST_URL))]
    assert world.downloads == 1
    assert cached_file_id() == "fresh1"


def test_an_inline_answer_refused_for_another_reason_is_not_swallowed(world, placeholder):
    cache_stale_post()
    query = InlineQuery(refusals=1, error=BadRequest("Bad Request: query is too old and response timeout expired"))

    with pytest.raises(BadRequest):
        run_inline(query)

    assert cached_file_id() == "stale"


def test_a_failed_preparation_answers_the_next_inline_queries_without_downloading_again(world, placeholder):
    world.download_error = instagram.NoMediaInPostError("nothing here")
    first, second, third = InlineQuery(), InlineQuery(), InlineQuery()

    run_inline(first, second, third)

    assert world.downloads == 1
    # The first query had nothing to go on yet and got the placeholder; the
    # next ones get the outcome - a branch that could never run before.
    assert answered_with(first) == [("photo", inline.inline_result_id(POST_URL))]
    assert answered_with(second)[0][0] == "article"
    assert second.answers[-1][0].title == "В посте нет медиа"
    assert answered_with(third) == answered_with(second)


def test_a_failure_is_forgotten_after_a_while(world, placeholder, monkeypatch):
    monkeypatch.setattr(preparation, "FAILED_PREPARATION_MEMORY_SECONDS", 0.05)
    world.download_error = instagram.NoMediaInPostError("nothing here")

    async def run():
        context = Context(asyncio.get_running_loop())
        for _ in range(2):
            await inline.handle_inline_query(SimpleNamespace(inline_query=InlineQuery()), context)
            await context.settle()
            await asyncio.sleep(0.1)
        return context

    context = asyncio.run(run())

    assert world.downloads == 2
    assert context.application.bot_data["inline_tasks"] == {}


def test_a_link_sent_to_the_bot_is_a_new_try_even_right_after_a_failure(world, placeholder):
    world.download_error = instagram.NoMediaInPostError("nothing here")

    async def run():
        context = Context(asyncio.get_running_loop())
        await inline.handle_inline_query(SimpleNamespace(inline_query=InlineQuery()), context)
        await context.settle()
        message = Message()
        await handlers.deliver_post(message, POST_URL, context)
        return message.status

    status = asyncio.run(run())

    assert world.downloads == 2
    assert status.text == "В этой публикации нет ни видео, ни фото, которые я могу скачать."


def test_a_post_that_came_through_leaves_no_task_behind(world, placeholder):
    context = run_inline(InlineQuery())

    assert world.downloads == 1
    assert context.application.bot_data["inline_tasks"] == {}


def test_a_placeholder_swap_refused_over_dead_files_drops_them(world, monkeypatch):
    cache_stale_post()

    async def edit_message_media(**kwargs):
        raise DEAD_FILE_ID

    async def run():
        bot = SimpleNamespace(edit_message_media=edit_message_media)
        context = Context(asyncio.get_running_loop(), bot=bot)
        chosen = SimpleNamespace(
            result_id=inline.inline_result_id(POST_URL), inline_message_id="inline-1", query=POST_URL,
            from_user=SimpleNamespace(id=1, username="someone", first_name="Someone"),
        )
        await inline.handle_chosen_inline_result(SimpleNamespace(chosen_inline_result=chosen), context)

    asyncio.run(run())

    assert cached_file_id() is None
