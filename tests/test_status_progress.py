"""The status message under a link and the caption of an inline placeholder:
showing what the preparation is doing, settling exactly once, and never left
hanging when something unforeseen breaks - plus the application's
last-resort error handler.

The edit interval is shortened for the tests; downloads are stand-ins that
report stages the way the real ones do.
"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from telegram import Bot, Update
from telegram.error import BadRequest
from telegram.request import BaseRequest

from nonnus import app, cache, config, delivery, handlers, inline, instagram, media, preparation, progress, status_message

POST_URL = "https://www.instagram.com/p/ABC123/"


@pytest.fixture(autouse=True)
def quick_edits(monkeypatch):
    monkeypatch.setattr(status_message, "STATUS_EDIT_INTERVAL_SECONDS", 0.05)


class RecordedMessage:
    """A sent status message: records its edits and whether it was deleted."""

    def __init__(self, fail_edits=False):
        self.edits = []
        self.deleted = False
        self.fail_edits = fail_edits

    async def edit_text(self, text, **kwargs):
        if self.fail_edits:
            raise BadRequest("message can't be edited")
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


# --- the texts ------------------------------------------------------------


@pytest.mark.parametrize(
    "stage, done, total, expected",
    [
        (progress.DOWNLOADING, 0, 1, "Скачиваю публикацию..."),
        (progress.DOWNLOADING, 3, 10, "Скачиваю публикацию: 3 из 10..."),
        (progress.QUEUED, 0, 0, "Жду своей очереди: сейчас скачиваются другие публикации..."),
        (progress.COMPRESSING, 0, 0, "Видео большое, сжимаю перед отправкой..."),
        (progress.UPLOADING, 0, 0, "Загружаю в Telegram..."),
    ],
)
def test_each_stage_has_its_text(stage, done, total, expected):
    assert status_message.progress_text(stage, done, total) == expected


@pytest.mark.parametrize(
    "done, total, kind, expected",
    [
        (0, 1, progress.REEL, "Скачиваю рилс..."),
        (0, 1, progress.PHOTO, "Скачиваю фото..."),
        (3, 12, progress.CAROUSEL, "Скачиваю карусель: 3 из 12..."),
    ],
)
def test_once_the_post_is_known_the_text_names_it(done, total, kind, expected):
    assert status_message.progress_text(progress.DOWNLOADING, done, total, kind) == expected


# --- the status message ---------------------------------------------------


def test_quick_updates_are_thinned_out_to_the_latest(monkeypatch):
    monkeypatch.setattr(status_message, "STATUS_EDIT_INTERVAL_SECONDS", 0.3)
    message = RecordedMessage()

    async def run():
        status = status_message.ReplyStatus(message, "start")
        # Spaced out, each one arriving well inside the interval.
        for text in ["1 из 3", "2 из 3", "3 из 3"]:
            status.show(text)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)

    asyncio.run(run())

    assert message.edits == ["3 из 3"]


def test_settling_cancels_a_pending_update(monkeypatch):
    # A late "3 из 10" must not overwrite the error that followed it - and
    # the error must not wait for the pending edit's turn to come.
    monkeypatch.setattr(status_message, "STATUS_EDIT_INTERVAL_SECONDS", 1.0)
    message = RecordedMessage()

    async def run():
        loop = asyncio.get_running_loop()
        status = status_message.ReplyStatus(message, "start")
        status.show("3 из 10")
        await asyncio.sleep(0)
        started = loop.time()
        await status.fail("error")
        took = loop.time() - started
        status.show("4 из 10")
        await asyncio.sleep(0.1)
        return took

    took = asyncio.run(run())

    assert message.edits == ["error"]
    assert took < 0.5


def test_nothing_is_shown_after_settling(monkeypatch):
    # A stage reported once the message is settled - its last word said -
    # is not shown, however soon an edit would be due.
    monkeypatch.setattr(status_message, "STATUS_EDIT_INTERVAL_SECONDS", 0)
    message = RecordedMessage()

    async def run():
        status = status_message.ReplyStatus(message, "start")
        await status.fail("error")
        status.show("Загружаю в Telegram...")
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert message.edits == ["error"]


def test_settling_does_not_swallow_a_cancellation_of_the_handler(monkeypatch):
    # If the handler itself is cancelled while settling - at shutdown, say -
    # that must go through, not be taken for the pending edit's own
    # cancellation and quietly dropped.
    monkeypatch.setattr(status_message, "STATUS_EDIT_INTERVAL_SECONDS", 0)

    class SlowToCancel(RecordedMessage):
        """A progress edit that takes a moment to wind down once cancelled;
        the final edit goes through at once."""

        async def edit_text(self, text, **kwargs):
            if text == "error":
                self.edits.append(text)
                return
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                raise

    async def run():
        status = status_message.ReplyStatus(SlowToCancel(), "start")
        status.show("1 из 3")
        await asyncio.sleep(0.01)
        settling = asyncio.get_running_loop().create_task(status.fail("error"))
        await asyncio.sleep(0.05)
        settling.cancel()
        await asyncio.wait({settling}, timeout=2)
        return settling

    settling = asyncio.run(run())

    # Swallowed, it would have gone on to the final edit as if nothing
    # happened, and ended normally.
    assert settling.cancelled()


def test_a_progress_edit_that_fails_is_not_fatal():
    message = RecordedMessage(fail_edits=True)

    async def run():
        status = status_message.ReplyStatus(message, "start")
        status.show("1 из 3")
        await asyncio.sleep(0.15)
        await status.done()

    asyncio.run(run())

    assert message.deleted


def test_the_same_text_is_not_edited_again():
    # Telegram refuses an edit that changes nothing.
    message = RecordedMessage()

    async def run():
        status = status_message.ReplyStatus(message, "start")
        status.show("start")
        await asyncio.sleep(0.15)

    asyncio.run(run())

    assert message.edits == []


# --- progress -------------------------------------------------------------


def test_a_report_from_a_worker_thread_reaches_the_listeners_on_the_loop():
    heard = []

    async def run():
        loop_thread = threading.get_ident()
        tracker = progress.Progress(asyncio.get_running_loop())
        tracker.subscribe(lambda *stage: heard.append((stage, threading.get_ident() == loop_thread)))
        token = progress.CURRENT.set(tracker)
        try:
            await asyncio.to_thread(progress.report, progress.DOWNLOADING, 1, 3)
        finally:
            progress.CURRENT.reset(token)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert heard == [((progress.DOWNLOADING, 1, 3, None), True)]


def test_someone_joining_late_hears_the_current_stage():
    heard = []

    async def run():
        tracker = progress.Progress(asyncio.get_running_loop())
        tracker.report(progress.UPLOADING)
        await asyncio.sleep(0)
        tracker.subscribe(lambda *stage: heard.append(stage))

    asyncio.run(run())

    assert heard == [(progress.UPLOADING, 0, 0, None)]


def test_outside_a_preparation_a_report_goes_nowhere():
    progress.report(progress.DOWNLOADING, 1, 2)


def stages_of_download(monkeypatch, tmp_path, info):
    """Run download_post on a probed post `info`, with the files coming down
    as stand-ins, and return the stages it reported."""
    monkeypatch.setattr(instagram, "probe_post", lambda url, download_dir, use_cookies=True: info)

    def download_post_videos(url, download_dir, entries, video_indices, use_cookies=True):
        paths = {}
        for index in video_indices:
            paths[index] = download_dir / f"{index}.mp4"
            paths[index].write_bytes(b"video")
        return paths

    def download_photo(entry, index, download_dir):
        path = download_dir / f"{index}.jpg"
        path.write_bytes(b"photo")
        return path

    monkeypatch.setattr(instagram, "download_post_videos", download_post_videos)
    monkeypatch.setattr(instagram, "download_photo", download_photo)
    monkeypatch.setattr(media, "prepare_photo_for_upload", lambda path, work_dir: path)
    monkeypatch.setattr(media, "ensure_h264_video", lambda path, work_dir: path)
    monkeypatch.setattr(media, "add_audio_warning_if_needed", lambda caption, path: caption)
    heard = []

    async def run():
        tracker = progress.Progress(asyncio.get_running_loop())
        tracker.subscribe(lambda *stage: heard.append(stage))
        progress.CURRENT.set(tracker)
        await asyncio.to_thread(instagram.download_post, POST_URL, tmp_path)
        await asyncio.sleep(0)

    asyncio.run(run())
    return heard


VIDEO = {"id": "v", "formats": [{"url": "https://cdn/v.mp4"}], "thumbnails": []}
PHOTO = {"id": "f", "formats": [], "thumbnails": [{"url": "https://cdn/f.jpg"}]}


def test_download_post_counts_the_files_of_a_carousel_as_they_come(monkeypatch, tmp_path):
    heard = stages_of_download(monkeypatch, tmp_path, {"entries": [PHOTO, VIDEO, PHOTO]})

    # The video pass first, then each photo.
    assert heard == [
        (progress.DOWNLOADING, 0, 3, progress.CAROUSEL),
        (progress.DOWNLOADING, 1, 3, progress.CAROUSEL),
        (progress.DOWNLOADING, 2, 3, progress.CAROUSEL),
        (progress.DOWNLOADING, 3, 3, progress.CAROUSEL),
    ]


@pytest.mark.parametrize("info, kind", [(VIDEO, progress.REEL), (PHOTO, progress.PHOTO)])
def test_download_post_says_what_a_lone_file_is(monkeypatch, tmp_path, info, kind):
    heard = stages_of_download(monkeypatch, tmp_path, dict(info))

    assert {stage[3] for stage in heard} == {kind}


def test_waiting_for_a_slot_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(preparation, "DOWNLOAD_SLOTS", asyncio.Semaphore(1))
    monkeypatch.setattr(instagram, "download_post", lambda url, download_dir: ([], "caption"))
    heard = []

    async def run():
        tracker = progress.Progress(asyncio.get_running_loop())
        tracker.subscribe(lambda *stage: heard.append(stage[0]))
        progress.CURRENT.set(tracker)
        await preparation.DOWNLOAD_SLOTS.acquire()
        waiting = asyncio.create_task(preparation.download_post_in_thread(POST_URL, tmp_path, None))
        await asyncio.sleep(0.05)
        preparation.DOWNLOAD_SLOTS.release()
        await waiting
        await asyncio.sleep(0)

    monkeypatch.setattr(preparation, "alert_if_cookies_rejected", _no_alert)
    asyncio.run(run())

    assert heard == [progress.QUEUED, progress.DOWNLOADING]


async def _no_alert(context):
    pass


# --- deliver_post ---------------------------------------------------------


class Chat:
    """A chat the link came from: hands out the status message."""

    chat_id = 1

    def __init__(self):
        self.status = RecordedMessage()

    async def reply_text(self, text, **kwargs):
        return self.status


def run_deliver_post(*chats):
    async def run():
        async def no_action(**kwargs):
            pass

        loop = asyncio.get_running_loop()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}, create_task=loop.create_task),
            bot=SimpleNamespace(send_chat_action=no_action),
        )
        await asyncio.gather(*(handlers.deliver_post(chat, POST_URL, context) for chat in chats))

    asyncio.run(run())


@pytest.fixture
def slow_carousel(monkeypatch):
    """A storage-chat setup where downloading a three-file post takes a
    moment, reporting each file, and the upload is a stand-in."""
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    monkeypatch.setattr(preparation, "alert_if_cookies_rejected", _no_alert)

    def download_post(url, download_dir):
        import time

        items = []
        for done in range(1, 4):
            time.sleep(0.08)
            path = download_dir / f"{done}.jpg"
            path.write_bytes(b"photo")
            items.append(media.MediaItem(path, "photo"))
            progress.report(progress.DOWNLOADING, done, 3, progress.CAROUSEL)
        return items, "caption"

    async def upload_items_to_storage(context, items, caption):
        await asyncio.sleep(0.15)
        return [{"type": "photo", "file_id": f"f{n}"} for n in range(len(items))]

    async def send_prepared_result(message, cached_result, url):
        pass

    monkeypatch.setattr(instagram, "download_post", download_post)
    monkeypatch.setattr(delivery, "upload_items_to_storage", upload_items_to_storage)
    monkeypatch.setattr(delivery, "send_prepared_result", send_prepared_result)


def test_the_status_follows_the_preparation_and_goes_away_at_the_end(slow_carousel):
    chat = Chat()

    run_deliver_post(chat)

    assert any(text.startswith("Скачиваю карусель:") and "из 3" in text for text in chat.status.edits)
    assert "Загружаю в Telegram..." in chat.status.edits
    assert chat.status.deleted


def test_everyone_waiting_on_one_preparation_sees_its_progress(slow_carousel):
    first, second = Chat(), Chat()

    run_deliver_post(first, second)

    assert "Загружаю в Telegram..." in first.status.edits
    assert "Загружаю в Telegram..." in second.status.edits
    assert first.status.deleted and second.status.deleted


def test_an_unforeseen_failure_does_not_leave_the_status_hanging(monkeypatch):
    def broken(url):
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(cache, "get_cached_inline_result", broken)
    chat = Chat()

    run_deliver_post(chat)

    assert chat.status.edits == [handlers.UNEXPECTED_ERROR_TEXT]


# --- the inline placeholder -------------------------------------------------


class PlaceholderBot:
    """The bot as the chosen placeholder sees it: records the caption edits
    and the swap for the post, in the order they were made."""

    def __init__(self):
        self.calls = []

    async def edit_message_caption(self, **kwargs):
        self.calls.append(("caption", kwargs))

    async def edit_message_media(self, **kwargs):
        self.calls.append(("media", kwargs))

    def captions(self):
        return [kwargs["caption"] for call, kwargs in self.calls if call == "caption"]


def choose_the_placeholder(monkeypatch, settle_for=0.0):
    """Someone sends the placeholder for POST_URL, not prepared yet; returns
    the bot's calls, `settle_for` seconds after the handler is done."""
    bot = PlaceholderBot()

    async def get_bot_username(context):
        return "nonnus_bot"

    async def run():
        loop = asyncio.get_running_loop()
        context = SimpleNamespace(application=SimpleNamespace(bot_data={}, create_task=loop.create_task), bot=bot)
        update = SimpleNamespace(
            chosen_inline_result=SimpleNamespace(
                result_id=inline.inline_result_id(POST_URL), inline_message_id="inline-message", query=POST_URL
            )
        )
        await inline.handle_chosen_inline_result(update, context)
        await asyncio.sleep(settle_for)

    monkeypatch.setattr(delivery, "get_bot_username", get_bot_username)
    asyncio.run(run())
    return bot


def test_the_placeholder_follows_the_preparation_then_becomes_the_post(monkeypatch, slow_carousel):
    bot = choose_the_placeholder(monkeypatch)

    assert any(text.startswith("Скачиваю карусель:") and "из 3" in text for text in bot.captions())
    assert "Загружаю в Telegram..." in bot.captions()
    assert [call for call, kwargs in bot.calls][-1] == "media"
    assert {kwargs["inline_message_id"] for call, kwargs in bot.calls} == {"inline-message"}


def test_every_caption_edit_keeps_the_placeholder_button(monkeypatch, slow_carousel):
    # An edit that leaves reply_markup out takes the keyboard off the message.
    bot = choose_the_placeholder(monkeypatch)

    keyboards = [kwargs["reply_markup"] for call, kwargs in bot.calls if call == "caption"]
    assert keyboards
    for keyboard in keyboards:
        button = keyboard.inline_keyboard[0][0]
        assert (button.text, button.url) == ("Открыть в Instagram", POST_URL)


def test_no_progress_edit_lands_on_the_post_once_it_is_swapped_in(monkeypatch, slow_carousel):
    # An edit still waiting for its turn when the post is ready would, if
    # let through, replace the post's caption with "Загружаю в Telegram...".
    # The interval is long enough for the whole preparation to fit inside
    # it, so an edit is still waiting when the post is swapped in.
    monkeypatch.setattr(status_message, "STATUS_EDIT_INTERVAL_SECONDS", 0.5)

    async def upload_items_to_storage(context, items, caption):
        return [{"type": "photo", "file_id": f"f{n}"} for n in range(len(items))]

    def download_post(url, download_dir):
        path = download_dir / "1.jpg"
        path.write_bytes(b"photo")
        progress.report(progress.DOWNLOADING, 1, 1)
        return [media.MediaItem(path, "photo")], "caption"

    monkeypatch.setattr(instagram, "download_post", download_post)
    monkeypatch.setattr(delivery, "upload_items_to_storage", upload_items_to_storage)

    bot = choose_the_placeholder(monkeypatch, settle_for=0.8)

    assert [call for call, kwargs in bot.calls][-1] == "media"


def test_a_failed_preparation_shows_on_the_placeholder(monkeypatch, slow_carousel):
    def broken(url, download_dir):
        raise RuntimeError("private post")

    monkeypatch.setattr(instagram, "download_post", broken)

    bot = choose_the_placeholder(monkeypatch)

    assert bot.captions()[-1] == "Не получилось подготовить публикацию. Попробуй еще раз."
    assert bot.calls[-1][1]["reply_markup"].inline_keyboard[0][0].text == "Открыть в Instagram"
    assert [call for call, kwargs in bot.calls if call == "media"] == []


# --- the last-resort error handler ------------------------------------------


class RecordingRequest(BaseRequest):
    """The network layer of python-telegram-bot, replaced: records the calls
    and answers each as Telegram would, well enough for the bot to go on."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **timeouts):
        endpoint = url.rsplit("/", 1)[-1]
        self.sent.append((endpoint, request_data.json_parameters if request_data else {}))
        if self.fail:
            return 400, json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}).encode()
        result = {"message_id": 2, "date": 0, "chat": {"id": 42, "type": "private"}}
        return 200, json.dumps({"ok": True, "result": result}).encode()


def update_in(chat_type, bot):
    return Update.de_json(
        {
            "update_id": 7,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 42, "type": chat_type},
                "text": "https://www.instagram.com/p/ABC123/",
            },
        },
        bot,
    )


def handle_error(update, request):
    bot = Bot("1:test", request=request, get_updates_request=RecordingRequest())
    if update is not None:
        update = update(bot)
    asyncio.run(app.on_error(update, SimpleNamespace(error=RuntimeError("boom"))))
    return [endpoint for endpoint, _ in request.sent]


def test_in_a_private_chat_the_user_is_told_something_went_wrong():
    request = RecordingRequest()

    assert handle_error(lambda bot: update_in("private", bot), request) == ["sendMessage"]
    assert request.sent[0][1]["text"] == handlers.UNEXPECTED_ERROR_TEXT


def test_in_a_group_nothing_is_sent():
    # The message may not even have been meant for the bot.
    assert handle_error(lambda bot: update_in("supergroup", bot), RecordingRequest()) == []


def test_a_background_task_failure_is_only_logged():
    assert handle_error(None, RecordingRequest()) == []


def test_failing_to_tell_the_user_is_not_an_error_of_its_own():
    request = RecordingRequest(fail=True)

    assert handle_error(lambda bot: update_in("private", bot), request) == ["sendMessage"]


def test_the_application_has_the_error_handler():
    assert app.on_error in app.build_application("1:test").error_handlers
