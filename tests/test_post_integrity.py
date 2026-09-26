"""A post goes out whole or not at all.

A post that is sent is also cached, and the cache serves it as it was saved
to every later request - so a file lost on the way would stay lost. These
tests cover the three ways that used to happen: a slide that failed to
download was dropped, a photo cut short in transit was kept, and an album
Telegram refuses - a document among photos, or an album of one - failed the
send partway through.

The photo downloads run against a real HTTP server on localhost, since what
is being checked is how urllib behaves when a response is cut short.
"""
import asyncio
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nonnus import cache, config, delivery, handlers, instagram, media, status_message

POST_URL = "https://www.instagram.com/p/ABC123/"


def video_entry(entry_id):
    return {"id": entry_id, "formats": [{"url": f"https://cdn/{entry_id}.mp4"}], "thumbnails": []}


def photo_entry(entry_id):
    return {"id": entry_id, "formats": [], "thumbnails": [{"url": f"https://cdn/{entry_id}.jpg"}]}


# --- a slide that would not download -----------------------------------


@pytest.fixture
def carousel(monkeypatch, tmp_path):
    """A three-file carousel - photo, video, photo - whose downloads are
    stand-ins. Set `failing_photos` to the positions whose photo fails."""
    state = SimpleNamespace(failing_photos=set(), video_file=True)
    monkeypatch.setattr(
        instagram,
        "probe_post",
        lambda url, download_dir, use_cookies=True: {
            "entries": [photo_entry("a"), video_entry("b"), photo_entry("c")]
        },
    )

    def download_post_videos(url, download_dir, entries, video_indices, use_cookies=True):
        if not state.video_file:
            return {}
        path = download_dir / "b.mp4"
        path.write_bytes(b"video")
        return {1: path}

    def download_photo(entry, index, download_dir):
        if index in state.failing_photos:
            return None
        path = download_dir / f"photo-{index}.jpg"
        path.write_bytes(b"photo")
        return path

    monkeypatch.setattr(instagram, "download_post_videos", download_post_videos)
    monkeypatch.setattr(instagram, "download_photo", download_photo)
    monkeypatch.setattr(media, "prepare_photo_for_upload", lambda path, work_dir: path)
    monkeypatch.setattr(media, "ensure_h264_video", lambda path, work_dir: path)
    state.work_dir = tmp_path
    return state


def test_a_whole_carousel_comes_through(carousel):
    items, _ = instagram.download_post(POST_URL, carousel.work_dir)

    assert [item.kind for item in items] == ["photo", "video", "photo"]


def test_a_photo_that_would_not_download_fails_the_post(carousel):
    carousel.failing_photos = {2}

    with pytest.raises(instagram.IncompletePostError, match="3 of 3"):
        instagram.download_post(POST_URL, carousel.work_dir)


def test_a_video_yt_dlp_left_no_file_for_fails_the_post(carousel):
    carousel.video_file = False

    with pytest.raises(instagram.IncompletePostError, match="2 of 3"):
        instagram.download_post(POST_URL, carousel.work_dir)


def test_an_incomplete_post_is_not_cached_and_the_user_is_told(monkeypatch):
    def incomplete(url, download_dir):
        raise instagram.IncompletePostError("file 3 of 3")

    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    monkeypatch.setattr(instagram, "download_post", incomplete)

    status = SimpleNamespace(text=None)

    class StatusMessage:
        async def edit_text(self, text, **kwargs):
            status.text = text

        async def delete(self):
            status.text = "deleted"

    class Message:
        chat_id = 1
        from_user = SimpleNamespace(id=1, username="someone", first_name="Someone")

        async def reply_text(self, text, **kwargs):
            return StatusMessage()

    async def run():
        async def no_action(**kwargs):
            pass

        loop = asyncio.get_running_loop()
        application = SimpleNamespace(bot_data={}, create_task=lambda coroutine: loop.create_task(coroutine))
        context = SimpleNamespace(application=application, bot=SimpleNamespace(send_chat_action=no_action))
        await handlers.deliver_post(Message(), POST_URL, context)

    asyncio.run(run())

    assert status.text == status_message.INCOMPLETE_POST_TEXT
    assert cache.get_cached_inline_result(POST_URL) is None


# --- a photo cut short in transit ---------------------------------------


class PhotoServer:
    """A one-connection-at-a-time HTTP server on localhost that answers each
    request with the next canned response, raw bytes and all."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = 0
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(len(self.responses))
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._socket.getsockname()[1]}/photo.jpg"

    def _serve(self):
        for response in self.responses:
            connection, _ = self._socket.accept()
            with connection:
                connection.recv(65536)
                self.requests += 1
                connection.sendall(response)

    def close(self):
        self._socket.close()


def whole(body):
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)


def cut_short(body, promised):
    # Promises more than it sends, then closes the connection cleanly.
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (promised, body)


def chunked_cut_short(body):
    # Announces a chunk longer than what follows, then closes.
    return b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n%x\r\n%s" % (
        len(body) * 3,
        body,
    )


@pytest.fixture
def serve(monkeypatch):
    servers = []
    monkeypatch.setattr(config, "PHOTO_DOWNLOAD_TIMEOUT_SECONDS", 5)

    def start(*responses):
        server = PhotoServer(*responses)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def download(server, tmp_path):
    return instagram.download_photo({"thumbnails": [{"url": server.url}]}, 0, tmp_path)


def test_a_whole_photo_is_kept(serve, tmp_path):
    server = serve(whole(b"x" * 1000))

    path = download(server, tmp_path)

    assert path.read_bytes() == b"x" * 1000
    assert server.requests == 1


def test_a_photo_cut_short_is_fetched_again(serve, tmp_path):
    server = serve(cut_short(b"x" * 300, promised=1000), whole(b"x" * 1000))

    path = download(server, tmp_path)

    assert path.read_bytes() == b"x" * 1000
    assert server.requests == 2


def test_a_photo_cut_short_every_time_is_given_up_on(serve, tmp_path):
    server = serve(*[cut_short(b"x" * 300, promised=1000)] * instagram.PHOTO_DOWNLOAD_ATTEMPTS)

    assert download(server, tmp_path) is None
    assert server.requests == instagram.PHOTO_DOWNLOAD_ATTEMPTS


def test_a_chunked_photo_cut_short_is_given_up_on_rather_than_raising(serve, tmp_path):
    # IncompleteRead is not an OSError, and used to take the whole post down.
    server = serve(*[chunked_cut_short(b"x" * 300)] * instagram.PHOTO_DOWNLOAD_ATTEMPTS)

    assert download(server, tmp_path) is None


# --- albums Telegram would refuse ---------------------------------------


class RecordingMessage:
    """Records each message a post goes out as: ("album", [files]) or
    (kind, file), plus the caption it carried."""

    def __init__(self):
        self.sent = []
        self.captions = []

    def _record(self, entry, caption):
        self.sent.append(entry)
        self.captions.append(caption)

    async def reply_media_group(self, media, **kwargs):
        # A file_id stays a string; a file from disk becomes an InputFile.
        files = [getattr(item.media, "filename", item.media) for item in media]
        self._record(("album", files), media[0].caption)

    async def reply_photo(self, photo, caption=None, **kwargs):
        self._record(("photo", photo if isinstance(photo, str) else Path(photo.name).name), caption)

    async def reply_video(self, video, caption=None, **kwargs):
        self._record(("video", video if isinstance(video, str) else Path(video.name).name), caption)

    async def reply_document(self, document, caption=None, **kwargs):
        self._record(("document", document), caption)


def cached(items):
    return {"caption": "caption", "title": "@someone", "items": items}


def photos(count, prefix="p"):
    return [{"type": "photo", "file_id": f"{prefix}{n}"} for n in range(count)]


def test_album_groups_split_around_documents_and_keep_the_order():
    items = photos(2) + [{"type": "document", "file_id": "d0"}] + photos(2, "q")

    groups = delivery.album_groups(items, lambda item: item["type"])

    assert [[item["file_id"] for item in group] for group in groups] == [["p0", "p1"], ["d0"], ["q0", "q1"]]


def test_documents_next_to_each_other_share_an_album():
    items = photos(1) + [{"type": "document", "file_id": f"d{n}"} for n in range(2)]

    groups = delivery.album_groups(items, lambda item: item["type"])

    assert [[item["file_id"] for item in group] for group in groups] == [["p0"], ["d0", "d1"]]


def test_an_eleven_file_carousel_sent_as_albums_has_no_album_of_one(monkeypatch):
    # The slideshow is what normally goes out; albums are the fallback when
    # Telegram refuses it, and were cut 10 + 1.
    monkeypatch.setattr(delivery, "carousel_slideshow_message", lambda url, cached_result: None)
    message = RecordingMessage()

    asyncio.run(delivery.send_prepared_result(message, cached(photos(11)), POST_URL))

    assert [len(files) for kind, files in message.sent] == [6, 5]
    assert message.captions == ["caption", None]


def test_a_carousel_with_a_document_in_the_middle_keeps_its_order_and_one_caption(monkeypatch):
    items = photos(2) + [{"type": "document", "file_id": "d0"}] + photos(3, "q")
    message = RecordingMessage()

    asyncio.run(delivery.send_prepared_result(message, cached(items), POST_URL))

    assert message.sent == [("album", ["p0", "p1"]), ("document", "d0"), ("album", ["q0", "q1", "q2"])]
    assert message.captions == ["caption", None, None]


def local_items(tmp_path, count):
    items = []
    for n in range(count):
        path = tmp_path / f"photo-{n:02d}.jpg"
        path.write_bytes(b"photo")
        items.append(media.MediaItem(path, "photo"))
    return items


def test_an_eleven_file_carousel_uploads_to_storage_without_an_album_of_one(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    albums = []

    async def send_media_group(chat_id, media, **kwargs):
        albums.append(len(media))
        return [SimpleNamespace(video=None, photo=[SimpleNamespace(file_id=f"f{n}")]) for n in range(len(media))]

    context = SimpleNamespace(bot=SimpleNamespace(send_media_group=send_media_group))

    uploaded = asyncio.run(delivery.upload_items_to_storage(context, local_items(tmp_path, 11), "caption"))

    assert albums == [6, 5]
    assert len(uploaded) == 11


def test_an_eleven_file_carousel_sent_from_disk_has_no_album_of_one(tmp_path):
    message = RecordingMessage()

    asyncio.run(delivery.send_local_media_items(message, local_items(tmp_path, 11), "caption"))

    assert [len(files) for kind, files in message.sent] == [6, 5]
    assert message.captions == ["caption", None]
