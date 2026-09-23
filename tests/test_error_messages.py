"""What the user is told when a post cannot be delivered - that it names the
real cause, not a neighbouring one.

Downloads and the storage upload are stand-ins; the rest of deliver_post
runs for real.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden

from nonnus import config, delivery, handlers, instagram, media

POST_URL = "https://www.instagram.com/p/ABC123/"
MB = 1024 * 1024


class Status:
    text = None

    async def edit_text(self, text, **kwargs):
        self.text = text

    async def delete(self):
        self.text = "deleted"


class Message:
    chat_id = 1

    def __init__(self):
        self.status = Status()

    async def reply_text(self, text, **kwargs):
        return self.status


def send_link():
    async def run():
        async def no_action(**kwargs):
            pass

        loop = asyncio.get_running_loop()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}, create_task=loop.create_task),
            bot=SimpleNamespace(send_chat_action=no_action),
        )
        message = Message()
        await handlers.deliver_post(message, POST_URL, context)
        return message.status.text

    return asyncio.run(run())


@pytest.fixture
def big_photo(monkeypatch):
    """A post that is one photo over the photo limit, which is set to 1 MB
    here so the file can stay small."""
    monkeypatch.setattr(config, "PHOTO_MAX_FILE_SIZE_MB", 1)
    monkeypatch.setattr(config, "PHOTO_MAX_FILE_SIZE_BYTES", MB)

    def download_post(url, download_dir):
        path = download_dir / "photo.jpg"
        path.write_bytes(b"x" * (MB + 1))
        return [media.MediaItem(path, "photo")], "caption"

    monkeypatch.setattr(instagram, "download_post", download_post)


# --- a file over Telegram's limit ----------------------------------------


def test_the_error_carries_the_kind_and_the_limit_it_crossed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHOTO_MAX_FILE_SIZE_MB", 1)
    monkeypatch.setattr(config, "PHOTO_MAX_FILE_SIZE_BYTES", MB)
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"x" * (MB + 1))

    with pytest.raises(media.MediaTooLargeError) as raised:
        media.ensure_items_fit_telegram([media.MediaItem(path, "photo")])

    assert (raised.value.kind, raised.value.limit_mb) == ("photo", 1)


@pytest.mark.parametrize(
    "kind, limit_mb, words",
    [("photo", 10, ["фото", "10 МБ"]), ("video", 50, ["видео", "50 МБ"])],
)
def test_the_message_names_the_limit_that_was_crossed(kind, limit_mb, words):
    text = handlers.too_large_text(media.MediaTooLargeError(kind, limit_mb))

    assert all(word in text for word in words)


def test_a_photo_too_large_is_not_reported_against_the_video_limit(monkeypatch, big_photo):
    # It used to say "larger than 50 MB" whatever the file was.
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")

    text = send_link()

    assert "фото" in text and "1 МБ" in text
    assert f"{config.MAX_FILE_SIZE_MB} МБ" not in text


def test_the_same_holds_without_a_storage_chat(monkeypatch, big_photo):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "")

    text = send_link()

    assert "фото" in text and "1 МБ" in text


# --- the storage chat refusing the upload -------------------------------


def test_a_failed_storage_upload_is_not_blamed_on_the_post(monkeypatch, tmp_path):
    # The post came through; the bot was, say, removed from the storage chat.
    # It used to say the post might be private or deleted.
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")

    def download_post(url, download_dir):
        path = download_dir / "photo.jpg"
        path.write_bytes(b"photo")
        return [media.MediaItem(path, "photo")], "caption"

    async def upload_items_to_storage(context, items, caption):
        raise Forbidden("Forbidden: bot was kicked from the supergroup chat")

    monkeypatch.setattr(instagram, "download_post", download_post)
    monkeypatch.setattr(delivery, "upload_items_to_storage", upload_items_to_storage)

    assert send_link() == handlers.UPLOAD_FAILED_TEXT


def test_a_download_that_failed_still_says_so(monkeypatch):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")

    def download_post(url, download_dir):
        raise RuntimeError("yt-dlp gave up")

    monkeypatch.setattr(instagram, "download_post", download_post)

    assert "закрытая или удалена" in send_link()
