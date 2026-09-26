"""Stories and highlights: their links, the photos yt-dlp would drop, the
caption, a highlight too big for one message, who may have all of someone's
stories, how long they stay cached, and what a user is told when one is gone.

Instagram is never asked: the extractor's own requests are replaced, so what
runs is yt-dlp's story extractor with the bot's changes to it. The accounts
and ids are made up.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram.error import BadRequest

from nonnus import cache, config, delivery, handlers, inline, instagram, links, media, preparation, progress, status_message, users

STORY_PK = "3994224585897789830"
PHOTO_PK = "3994224585897789831"
HIGHLIGHT_ID = "18000000000000042"
USER = {"pk": "42", "id": "42", "username": "some.one", "full_name": "Some One"}


# --- links -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, canonical, kind",
    [
        # As the app shares a story.
        (f"глянь https://www.instagram.com/stories/Some.One/{STORY_PK}?utm_source=ig_story_item_share&stkn=abc",
         f"https://www.instagram.com/stories/some.one/{STORY_PK}/", links.STORY),
        # As the app shares a highlight: base64 of "highlight:<id>", pointing
        # at one of its items.
        ("https://www.instagram.com/s/aGlnaGxpZ2h0OjE4MDAwMDAwMDAwMDAwMDQy?story_media_id=1_42&stkn=MXA5==",
         f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/", links.HIGHLIGHT),
        (f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/",
         f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/", links.HIGHLIGHT),
        ("https://m.instagram.com/stories/some.one/", "https://www.instagram.com/stories/some.one/", links.STORIES),
    ],
)
def test_story_links_are_found_and_known_for_what_they_are(text, canonical, kind):
    url = links.find_instagram_url(text)

    assert links.normalize_post_url(url) == canonical
    assert links.story_kind(url) == kind


@pytest.mark.parametrize(
    "url, canonical",
    [
        # Posts from the profiles of someone called "s" and someone called
        # "stories" are posts.
        ("https://www.instagram.com/s/p/ABC123/", "https://www.instagram.com/p/ABC123/"),
        ("https://www.instagram.com/stories/p/ABC123/", "https://www.instagram.com/p/ABC123/"),
    ],
)
def test_posts_from_profiles_named_like_stories_stay_posts(url, canonical):
    assert links.find_instagram_url(url) == url
    assert links.normalize_post_url(url) == canonical
    assert links.story_kind(url) is None


def test_an_s_link_that_is_not_a_highlight_is_not_taken_for_one():
    # base64 of "nothing:123"
    assert links.story_kind("https://www.instagram.com/s/bm90aGluZzoxMjM=") is None


def test_a_highlight_has_a_deep_link_and_a_story_does_not():
    highlight = f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/"

    assert links.post_url_from_start_payload(links.post_start_payload(highlight)) == highlight
    assert links.post_start_payload(f"https://www.instagram.com/stories/some.one/{STORY_PK}/") is None


def test_the_carousel_button_of_a_highlight_opens_it_in_the_bot():
    keyboard = inline.carousel_keyboard(f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/", "nonnus_bot")

    assert keyboard.inline_keyboard[0][0].url == f"https://t.me/nonnus_bot?start=highlight_{HIGHLIGHT_ID}"


# --- the extractor ---------------------------------------------------------------


def story_item(pk, video):
    item = {
        "pk": pk,
        "taken_at": 1,
        "user": dict(USER),
        "image_versions2": {"candidates": [{"url": f"https://cdn.example/{pk}.jpg", "width": 1080, "height": 1920}]},
    }
    if video:
        item["video_versions"] = [{"url": f"https://cdn.example/{pk}.mp4", "width": 720, "height": 1280, "type": 101}]
    return item


@pytest.fixture
def instagram_answers(monkeypatch):
    """The two requests yt-dlp's story extractor makes - the page, for the
    user, and the reels_media API - answered with a video and a photo.

    Replaced on yt-dlp's own extractor, which the bot's inherits from: a story
    that went to yt-dlp's extractor by mistake would still stay off the
    network - and lose its photo, which is what the tests would catch."""
    asked = []

    def download_webpage(self, url, video_id, *args, **kwargs):
        asked.append(url)
        return 'window.data = {"user":{"pk":"42","id":"42","username":"some.one"}};'

    def download_json(self, url, video_id, *args, **kwargs):
        asked.append(url)
        items = [story_item(STORY_PK, video=True), story_item(PHOTO_PK, video=False)]
        return {"reels": {
            f"highlight:{HIGHLIGHT_ID}": {"title": "Trip", "user": dict(USER), "items": items},
            "42": {"user": dict(USER), "items": [dict(item) for item in items]},
        }}

    monkeypatch.setattr(instagram.InstagramStoryIE, "_download_webpage", download_webpage)
    monkeypatch.setattr(instagram.InstagramStoryIE, "_download_json", download_json)
    return asked


def test_a_highlight_keeps_its_photos(instagram_answers, tmp_path):
    info = instagram.probe_post(f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/", tmp_path, use_cookies=False)

    video, photo = instagram.post_entries(info)
    assert instagram.entry_has_video(video)
    assert not instagram.entry_has_video(photo)
    assert photo["formats"] == []
    assert instagram.best_photo_url(photo) == f"https://cdn.example/{PHOTO_PK}.jpg"
    assert info["title"] == "Trip"


def test_a_story_link_brings_that_one_story(instagram_answers, tmp_path):
    info = instagram.probe_post(f"https://www.instagram.com/stories/some.one/{PHOTO_PK}/", tmp_path, use_cookies=False)

    # The photo alone - not the playlist of all the user's current stories.
    assert "entries" not in info
    assert info["formats"] == []
    assert instagram.best_photo_url(info) == f"https://cdn.example/{PHOTO_PK}.jpg"


def test_a_user_s_stories_are_all_of_them(instagram_answers, tmp_path):
    info = instagram.probe_post("https://www.instagram.com/stories/some.one/", tmp_path, use_cookies=False)

    assert len(instagram.post_entries(info)) == 2


def test_posts_still_go_to_yt_dlp_s_own_extractor(monkeypatch, tmp_path):
    used = []

    class YoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add_info_extractor(self, ie):
            used.append(ie)

        def extract_info(self, url, download, process, ie_key=None):
            used.append(ie_key)
            return {"id": "x", "formats": [{"url": "https://cdn.example/x.mp4"}]}

    monkeypatch.setattr(instagram, "YoutubeDL", YoutubeDL)

    instagram.probe_post("https://www.instagram.com/p/ABC123/", tmp_path, use_cookies=False)

    assert used == [None]


# --- downloading -------------------------------------------------------------------


def download(monkeypatch, tmp_path, url, info):
    """download_post with the probe answering `info` and the files coming down
    as stand-ins; returns the items, the caption and the stages reported."""
    monkeypatch.setattr(instagram, "probe_post", lambda url, download_dir, use_cookies=True: info)

    def download_post_videos(info, download_dir, entries, video_indices, use_cookies=True):
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
        result = await asyncio.to_thread(instagram.download_post, url, Path(tmp_path))
        await asyncio.sleep(0)
        return result

    items, caption = asyncio.run(run())
    return items, caption, heard


def entry(index, video):
    base = {"id": f"s{index}", "channel": "some.one", "thumbnails": [{"url": f"https://cdn.example/{index}.jpg"}]}
    base["formats"] = [{"url": f"https://cdn.example/{index}.mp4"}] if video else []
    return base


def test_a_big_highlight_comes_whole(monkeypatch, tmp_path):
    url = f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/"
    info = {"_type": "playlist", "title": "Trip", "webpage_url": url,
            "entries": [entry(index, video=index % 3 == 0) for index in range(95)]}

    items, caption, heard = download(monkeypatch, tmp_path, url, info)

    assert len(items) == 95
    assert caption == f'Хайлайт «Trip» <a href="{url}">@some.one</a>'
    assert {stage[3] for stage in heard} == {progress.HIGHLIGHT}
    assert heard[-1][1:3] == (95, 95)


def test_a_story_is_captioned_as_one(monkeypatch, tmp_path):
    url = f"https://www.instagram.com/stories/some.one/{STORY_PK}/"
    info = {**entry(0, video=True), "webpage_url": url}

    items, caption, heard = download(monkeypatch, tmp_path, url, info)

    assert caption == f'Сторис <a href="{url}">@some.one</a>'
    assert {stage[3] for stage in heard} == {progress.STORY}


def test_a_user_s_stories_name_their_author_from_the_stories(monkeypatch, tmp_path):
    url = "https://www.instagram.com/stories/some.one/"
    info = {"_type": "playlist", "title": "Story by some.one", "webpage_url": url,
            "entries": [entry(0, video=True), entry(1, video=False)]}

    items, caption, heard = download(monkeypatch, tmp_path, url, info)

    assert caption == f'Сторис <a href="{url}">@some.one</a>'
    assert [item.kind for item in items] == ["video", "photo"]


@pytest.mark.parametrize(
    "kind, done, total, expected",
    [(progress.STORY, 0, 1, "Скачиваю сторис..."), (progress.STORY, 2, 7, "Скачиваю сторис: 2 из 7..."),
     (progress.HIGHLIGHT, 12, 50, "Скачиваю хайлайт: 12 из 50...")],
)
def test_the_progress_names_stories_and_highlights(kind, done, total, expected):
    assert status_message.progress_text(progress.DOWNLOADING, done, total, kind) == expected


# --- the cache -----------------------------------------------------------------------


CACHED = {"caption": "caption", "title": "title", "items": [{"type": "photo", "file_id": "f1"}]}


def age(url, hours):
    cache.POST_CACHE._connect().execute(
        "UPDATE posts SET saved_at = datetime('now', ?) WHERE url = ?", (f"-{hours} hours", links.normalize_post_url(url))
    )


@pytest.mark.parametrize(
    "url, served_after_two_hours",
    [
        (f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/", False),
        ("https://www.instagram.com/stories/some.one/", False),
        # A single story and a post stay what they were.
        (f"https://www.instagram.com/stories/some.one/{STORY_PK}/", True),
        ("https://www.instagram.com/p/ABC123/", True),
    ],
)
def test_what_changes_is_served_from_the_cache_for_an_hour(url, served_after_two_hours):
    cache.save_cached_inline_result(url, dict(CACHED))
    assert cache.get_cached_inline_result(url) is not None

    age(url, 2)

    assert (cache.get_cached_inline_result(url) is not None) == served_after_two_hours


# --- what the user is told ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (f"https://www.instagram.com/stories/some.one/{STORY_PK}/", "Этой сторис больше нет: сторис живут сутки."),
        ("https://www.instagram.com/stories/some.one/", "Сейчас у этого пользователя нет сторис."),
        (f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/", "В этом хайлайте нет ни видео, ни фото, которые я могу скачать."),
        ("https://www.instagram.com/p/ABC123/", "В этой публикации нет ни видео, ни фото, которые я могу скачать."),
    ],
)
def test_nothing_to_download_is_explained_for_what_the_link_was(monkeypatch, url, expected):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    # All of someone's stories are for premium users.
    users.USER_STORE.set_role(1, users.PREMIUM)

    async def prepare_inline_post(url, context, tracker=None):
        raise instagram.NoMediaInPostError("Instagram returned nothing")

    monkeypatch.setattr(preparation, "prepare_inline_post", prepare_inline_post)
    status = SimpleNamespace(edits=[])

    async def edit_text(text, **kwargs):
        status.edits.append(text)

    async def reply_text(text, **kwargs):
        return SimpleNamespace(edit_text=edit_text)

    async def run():
        async def no_action(**kwargs):
            pass

        loop = asyncio.get_running_loop()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}, create_task=loop.create_task),
            bot=SimpleNamespace(send_chat_action=no_action),
        )
        message = SimpleNamespace(chat_id=1, from_user=SimpleNamespace(id=1, username=None, first_name="U"),
                                  reply_text=reply_text)
        await handlers.deliver_post(message, url, context)

    asyncio.run(run())

    assert status.edits[-1] == expected


def test_a_failed_story_is_not_blamed_on_a_closed_post():
    assert "сторис живут сутки" in handlers.download_failed_text(f"https://www.instagram.com/stories/some.one/{STORY_PK}/")
    assert "хайлайт" in handlers.download_failed_text(f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/")
    assert "публикацию" in handlers.download_failed_text("https://www.instagram.com/p/ABC123/")


# --- sending a highlight too big for one message --------------------------------------


HIGHLIGHT_URL = f"https://www.instagram.com/stories/highlights/{HIGHLIGHT_ID}/"


def highlight_result(count):
    return {
        "caption": f'Хайлайт «Trip» <a href="{HIGHLIGHT_URL}">@some.one</a>\n\nВидео сжато.',
        "title": "@some.one",
        "items": [{"type": "photo" if index % 2 else "video", "file_id": f"f{index}"} for index in range(count)],
    }


def slideshow_caption(slideshow):
    return slideshow["blocks"][0]["caption"]["text"]


def test_a_carousel_is_one_slideshow_and_says_post():
    result = {"caption": 'Пост <a href="https://www.instagram.com/p/ABC123/">@some.one</a>', "title": "@some.one",
              "items": [{"type": "photo", "file_id": f"f{index}"} for index in range(20)]}

    [(part, slideshow)] = delivery.slideshow_messages("https://www.instagram.com/p/ABC123/", result)

    assert len(part) == 20
    assert slideshow_caption(slideshow)[0] == "Пост "


def test_a_highlight_of_95_is_two_slideshows_of_48_and_47_each_named():
    messages = delivery.slideshow_messages(HIGHLIGHT_URL, highlight_result(95))

    assert [len(part) for part, slideshow in messages] == [48, 47]
    assert [file["file_id"] for part, slideshow in messages for file in part] == [f"f{index}" for index in range(95)]
    for part, slideshow in messages:
        assert slideshow_caption(slideshow) == ["Хайлайт «Trip» ", {"type": "url", "text": "@some.one", "url": HIGHLIGHT_URL}]
        assert len(slideshow["blocks"][0]["blocks"]) == len(part)
    # The note after the author goes with the first only.
    assert [len(slideshow["blocks"]) for part, slideshow in messages] == [2, 1]


@pytest.mark.parametrize(
    "url, caption, label",
    [
        ("https://www.instagram.com/p/ABC123/", 'Пост <a href="x">@a</a>', "Пост"),
        (HIGHLIGHT_URL, 'Хайлайт «Trip &amp; more» <a href="x">@a</a>', "Хайлайт «Trip & more»"),
        # No author, so no link to find the words by: the link tells.
        (f"https://www.instagram.com/stories/some.one/{STORY_PK}/", "Сторис https://example", "Сторис"),
        (HIGHLIGHT_URL, "Хайлайт https://example", "Хайлайт"),
        ("https://www.instagram.com/p/ABC123/", "Пост https://example", "Пост"),
    ],
)
def test_the_slideshow_names_what_it_is_from_the_caption(url, caption, label):
    assert delivery.caption_label(url, caption) == label


class Chat:
    """A private chat the highlight is sent to; refuses the rich messages
    listed in `refuse`, by their place in order."""

    chat_id = 42
    message_id = 7
    chat = SimpleNamespace(type="private")
    is_topic_message = False
    message_thread_id = None

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.slideshows = []
        self.albums = []

    def get_bot(self):
        return self

    async def do_api_request(self, endpoint, api_kwargs=None, **kwargs):
        number = len(self.slideshows)
        self.slideshows.append(api_kwargs["rich_message"])
        if number in self.refuse:
            raise BadRequest("rich messages are not available here")

    async def reply_media_group(self, media, **kwargs):
        self.albums.append(([item.media for item in media], media[0].caption))


def test_a_big_highlight_goes_out_as_two_messages():
    chat = Chat()

    asyncio.run(delivery.send_prepared_result(chat, highlight_result(95), HIGHLIGHT_URL))

    assert len(chat.slideshows) == 2
    assert chat.albums == []


def test_a_slideshow_refused_goes_out_as_albums_of_its_own_files_only():
    chat = Chat(refuse={1})

    asyncio.run(delivery.send_prepared_result(chat, highlight_result(95), HIGHLIGHT_URL))

    sent = [file_id for files, caption in chat.albums for file_id in files]
    assert sent == [f"f{index}" for index in range(48, 95)]
    # The first slideshow carried the caption; the albums after it do not.
    assert {caption for files, caption in chat.albums} == {None}


# --- who may have all of someone's stories ---------------------------------------


STORIES_URL = "https://www.instagram.com/stories/some.one/"
SENDER = SimpleNamespace(id=1, username=None, first_name="U")


@pytest.mark.parametrize(
    "url, role, refused",
    [
        (STORIES_URL, users.REGULAR, True),
        (STORIES_URL, users.PREMIUM, False),
        (STORIES_URL, users.ADMIN, False),
        (f"https://www.instagram.com/stories/some.one/{STORY_PK}/", users.REGULAR, False),
        (HIGHLIGHT_URL, users.REGULAR, False),
        ("https://www.instagram.com/p/ABC123/", users.REGULAR, False),
    ],
)
def test_all_of_someone_s_stories_are_for_premium_users_and_admins(url, role, refused):
    users.USER_STORE.set_role(SENDER.id, role)

    assert (users.refusal_for(SENDER, url) is not None) == refused


def test_with_nobody_to_ask_about_all_stories_are_refused():
    assert users.refusal_for(None, STORIES_URL) == users.STORIES_FOR_PREMIUM_TEXT


def deliver(monkeypatch, url):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    started = []

    async def prepare_inline_post(url, context, tracker=None):
        started.append(url)
        return {"caption": "c", "title": "t", "items": [{"type": "photo", "file_id": "f"}]}

    async def send_prepared_result(message, cached_result, url):
        pass

    monkeypatch.setattr(preparation, "prepare_inline_post", prepare_inline_post)
    monkeypatch.setattr(delivery, "send_prepared_result", send_prepared_result)
    status = SimpleNamespace(edits=[], deleted=False)

    async def edit_text(text, **kwargs):
        status.edits.append(text)

    async def delete():
        status.deleted = True

    async def reply_text(text, **kwargs):
        return SimpleNamespace(edit_text=edit_text, delete=delete)

    async def run():
        async def no_action(**kwargs):
            pass

        loop = asyncio.get_running_loop()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}, create_task=loop.create_task),
            bot=SimpleNamespace(send_chat_action=no_action),
        )
        await handlers.deliver_post(SimpleNamespace(chat_id=1, from_user=SENDER, reply_text=reply_text), url, context)

    asyncio.run(run())
    return status, started


def test_a_regular_user_asking_for_all_stories_is_told_and_nothing_is_downloaded(monkeypatch):
    status, started = deliver(monkeypatch, STORIES_URL)

    assert status.edits == [users.STORIES_FOR_PREMIUM_TEXT]
    assert started == []


def test_not_even_from_the_cache(monkeypatch):
    cache.save_cached_inline_result(STORIES_URL, dict(CACHED))

    status, started = deliver(monkeypatch, STORIES_URL)

    assert status.edits == [users.STORIES_FOR_PREMIUM_TEXT]


def test_a_premium_user_gets_them(monkeypatch):
    users.USER_STORE.set_role(SENDER.id, users.PREMIUM)

    status, started = deliver(monkeypatch, STORIES_URL)

    assert started == [STORIES_URL]
    assert status.deleted


def test_in_inline_mode_a_regular_user_is_told_too(monkeypatch):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    answers = []

    async def answer(results, **kwargs):
        answers.append(results)

    async def run():
        loop = asyncio.get_running_loop()
        context = SimpleNamespace(application=SimpleNamespace(bot_data={}, create_task=loop.create_task))
        query = SimpleNamespace(query=STORIES_URL, answer=answer, from_user=SENDER)
        await inline.handle_inline_query(SimpleNamespace(inline_query=query), context)
        return context

    context = asyncio.run(run())

    [[result]] = answers
    assert result.input_message_content.message_text == users.STORIES_FOR_PREMIUM_TEXT
    assert context.application.bot_data.get("inline_tasks", {}) == {}
