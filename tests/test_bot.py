"""Tests for the pure logic of the bot.

Nothing here touches the network or Telegram: the pieces that do are replaced
with stand-ins, so what gets exercised is the decision-making around them -
which entries of a post carry a video, what order the files come out in, and
what gets handed to Telegram.
"""

import pytest

from nonnus import config, links, media, instagram, cache, delivery, inline


# --- link recognition --------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "https://www.instagram.com/reel/ABC123/",
            "https://www.instagram.com/reel/ABC123/",
        ),
        (
            "смотри https://www.instagram.com/p/ABC123/ вот это",
            "https://www.instagram.com/p/ABC123/",
        ),
        (
            "https://instagram.com/tv/ABC123",
            "https://instagram.com/tv/ABC123",
        ),
        (
            "https://instagr.am/reel/ABC123/",
            "https://instagr.am/reel/ABC123/",
        ),
        (
            "https://www.instagram.com/reel/ABC123/?igsh=xyz",
            "https://www.instagram.com/reel/ABC123/?igsh=xyz",
        ),
        # A post opened from a profile in the web app links with the username
        # in front of the post type.
        (
            "смотри https://www.instagram.com/ilyuza_minnekhanova/p/DaLmfakDKfg/",
            "https://www.instagram.com/ilyuza_minnekhanova/p/DaLmfakDKfg/",
        ),
        (
            "https://www.instagram.com/some.one_2/reel/ABC123/",
            "https://www.instagram.com/some.one_2/reel/ABC123/",
        ),
    ],
)
def test_find_instagram_url_extracts_the_link(text, expected):
    assert links.find_instagram_url(text) == expected


def test_find_instagram_url_ignores_trailing_punctuation():
    found = links.find_instagram_url("глянь https://www.instagram.com/p/ABC123/, там карусель")
    assert found == "https://www.instagram.com/p/ABC123/"


@pytest.mark.parametrize("text", ["", "просто текст", "https://example.com/p/ABC123/"])
def test_find_instagram_url_returns_none_without_a_link(text):
    assert links.find_instagram_url(text) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/p/ABC123",
        "https://instagram.com/p/ABC123/",
        "https://instagr.am/p/ABC123/",
        "https://www.instagram.com/p/ABC123/?igsh=xyz",
        "https://www.instagram.com/someone/p/ABC123/",
        "https://www.instagram.com/some.one_2/p/ABC123/?img_index=3",
        "https://www.instagram.com/P/ABC123/",
    ],
)
def test_normalize_post_url_collapses_aliases_and_query(url):
    assert links.normalize_post_url(url) == "https://www.instagram.com/p/ABC123/"


def test_normalize_post_url_is_not_fooled_by_a_username_that_looks_like_a_type():
    # Read from the end of the path: the code comes last, the type right
    # before it, whatever the username says.
    assert links.normalize_post_url("https://www.instagram.com/reel/p/ABC123/") == "https://www.instagram.com/p/ABC123/"


def test_a_profile_link_and_a_plain_link_share_one_cache_entry():
    assert inline.inline_result_id("https://www.instagram.com/someone/p/ABC123/") == inline.inline_result_id(
        "https://www.instagram.com/p/ABC123/"
    )


# --- what a post is made of -------------------------------------------


def video_entry(entry_id="v1"):
    return {"id": entry_id, "formats": [{"url": f"https://cdn/{entry_id}.mp4"}], "thumbnails": []}


def photo_entry(entry_id="p1"):
    return {
        "id": entry_id,
        "formats": [],
        "thumbnails": [
            {"url": f"https://cdn/{entry_id}-small.webp", "width": 320, "height": 320},
            {"url": f"https://cdn/{entry_id}-big.webp", "width": 1440, "height": 1920},
        ],
    }


def test_entry_has_video_needs_a_usable_format():
    assert instagram.entry_has_video(video_entry()) is True
    assert instagram.entry_has_video(photo_entry()) is False
    assert instagram.entry_has_video({"formats": [{}]}) is False
    assert instagram.entry_has_video({}) is False


def test_best_photo_url_picks_the_largest_candidate():
    assert instagram.best_photo_url(photo_entry()) == "https://cdn/p1-big.webp"


def test_best_photo_url_falls_back_to_the_single_thumbnail():
    assert instagram.best_photo_url({"thumbnail": "https://cdn/only.jpg"}) == "https://cdn/only.jpg"
    assert instagram.best_photo_url({}) is None


def cdn(stp=None, path="/v/t51.2885-15/541_902_n.jpg"):
    """A photo URL the way Instagram's CDN hands them out, markers in stp."""
    query = f"stp={stp}&_nc_ht=scontent" if stp else "_nc_ht=scontent"
    return f"https://scontent.cdninstagram.com{path}?{query}"


def logged_out(*urls):
    # Logged out, Instagram gives variants with no sizes at all.
    return {"thumbnails": [{"url": url} for url in urls]}


def test_best_photo_url_skips_square_crops_that_come_first():
    # The order DO34j-fjCwE came in: seven square crops of a 1440x1920
    # photo, then the full frame. Taking the first meant a 1080x1080 square.
    crops = [cdn(f"c0.240.1440.1440a_dst-jpg_e35_s{n}x{n}_tt6") for n in (1080, 150, 240, 320, 480, 640, 750)]
    original = cdn("dst-jpg_e35_tt6")
    frames = [cdn("dst-jpg_e35_p1080x1080_tt6"), cdn("dst-jpg_e35_p320x320_tt6"), cdn("dst-jpg_e35_s640x640_tt6")]

    assert instagram.best_photo_url(logged_out(*crops, *frames, original)) == original


def test_best_photo_url_takes_the_original_when_it_comes_first():
    # The order DHhOwXQN2ER came in: the original, then scaled-down copies.
    original = cdn()
    copies = [cdn(f"dst-webp_s{n}x{n}") for n in (1080, 150, 240, 750)]

    assert instagram.best_photo_url(logged_out(original, *copies)) == original


def test_best_photo_url_takes_the_largest_bound_without_an_original():
    small, large = cdn("dst-jpg_e35_p640x640"), cdn("dst-jpg_e35_p1080x1080")
    crop = cdn("c0.240.1440.1440a_dst-jpg_e35_s1440x1440")

    assert instagram.best_photo_url(logged_out(small, crop, large)) == large


def test_best_photo_url_still_returns_something_when_there_are_only_crops():
    small, large = cdn("c0.240.1440.1440a_s320x320"), cdn("c0.240.1440.1440a_s1080x1080")

    assert instagram.best_photo_url(logged_out(small, large)) == large


def test_best_photo_url_reads_markers_from_the_path_of_older_links():
    crop = cdn(path="/v/t51.2885-15/c0.240.1440.1440a/s1080x1080/541_n.jpg")
    copy = cdn(path="/v/t51.2885-15/s640x640/541_n.jpg")
    original = cdn(path="/v/t51.2885-15/541_n.jpg")

    assert instagram.best_photo_url(logged_out(crop, copy, original)) == original


def test_best_photo_url_prefers_the_full_frame_even_when_sizes_are_known():
    # Logged in, sizes come along; a crop of the same photo is smaller anyway,
    # but it must not win on a tie.
    entry = {
        "thumbnails": [
            {"url": cdn("c0.0.1440.1440a_s1440x1440"), "width": 1440, "height": 1440},
            {"url": cdn("dst-jpg_e35"), "width": 1440, "height": 1440},
        ]
    }

    assert instagram.best_photo_url(entry) == cdn("dst-jpg_e35")


def test_post_entries_wraps_a_single_medium_post():
    entry = video_entry()
    assert instagram.post_entries(entry) == [entry]


def test_post_entries_keeps_carousel_order_and_drops_blanks():
    first, second = photo_entry("a"), video_entry("b")
    assert instagram.post_entries({"entries": [first, None, second]}) == [first, second]


def test_post_entries_handles_a_lazy_entries_iterable():
    entries = iter([photo_entry("a"), video_entry("b")])
    assert len(instagram.post_entries({"entries": entries})) == 2


# --- mapping downloaded videos back onto carousel positions ------------


class FakeYoutubeDL:
    """Stands in for yt-dlp so download_post_videos can be checked without a
    network round trip. Records the options it was handed."""

    captured_opts: list[dict] = []
    result: dict = {}

    def __init__(self, ydl_opts):
        type(self).captured_opts.append(ydl_opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download):
        assert download is True
        return type(self).result


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYoutubeDL.captured_opts = []
    FakeYoutubeDL.result = {}
    monkeypatch.setattr(instagram, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_download_post_videos_maps_results_onto_carousel_positions(fake_ydl, tmp_path):
    second = tmp_path / "b.mp4"
    fourth = tmp_path / "d.mp4"
    for path in (second, fourth):
        path.write_bytes(b"video")

    fake_ydl.result = {
        "entries": [
            {"id": "b", "requested_downloads": [{"filepath": str(second)}]},
            {"id": "d", "requested_downloads": [{"filepath": str(fourth)}]},
        ]
    }
    entries = [photo_entry("a"), video_entry("b"), photo_entry("c"), video_entry("d"), photo_entry("e")]

    video_paths = instagram.download_post_videos("https://x/", tmp_path, entries, [1, 3])

    assert video_paths == {1: second, 3: fourth}
    # playlist_items is 1-based, so positions 1 and 3 are items 2 and 4.
    assert fake_ydl.captured_opts[0]["playlist_items"] == "2,4"


def test_download_post_videos_skips_playlist_items_for_a_single_medium_post(fake_ydl, tmp_path):
    only = tmp_path / "a.mp4"
    only.write_bytes(b"video")
    fake_ydl.result = {"id": "a", "requested_downloads": [{"filepath": str(only)}]}

    video_paths = instagram.download_post_videos("https://x/", tmp_path, [video_entry("a")], [0])

    assert video_paths == {0: only}
    assert "playlist_items" not in fake_ydl.captured_opts[0]


def test_download_post_videos_falls_back_to_the_largest_video_file(fake_ydl, tmp_path):
    (tmp_path / "cookies.txt").write_bytes(b"x" * 5000)
    small = tmp_path / "small.mp4"
    small.write_bytes(b"x" * 10)
    biggest = tmp_path / "biggest.mp4"
    biggest.write_bytes(b"x" * 100)
    # yt-dlp reported no filepath at all.
    fake_ydl.result = {"id": "a"}

    video_paths = instagram.download_post_videos("https://x/", tmp_path, [video_entry("a")], [0])

    # The cookie file shares the directory and must not be mistaken for media.
    assert video_paths == {0: biggest}


# --- assembling a post -------------------------------------------------


@pytest.fixture
def stub_downloads(monkeypatch, tmp_path):
    """Replace the two things download_post does over the network."""

    def fake_download_photo(entry, index, download_dir):
        path = download_dir / f"photo-{index:02d}.jpg"
        path.write_bytes(b"photo")
        return path

    monkeypatch.setattr(instagram, "download_photo", fake_download_photo)
    monkeypatch.setattr(media, "prepare_photo_for_upload", lambda path, work_dir: path)
    monkeypatch.setattr(media, "ensure_h264_video", lambda path, work_dir: path)
    return tmp_path


def test_download_post_keeps_mixed_carousel_order(monkeypatch, stub_downloads):
    work_dir = stub_downloads
    video_path = work_dir / "clip.mp4"
    video_path.write_bytes(b"video")

    monkeypatch.setattr(
        instagram,
        "probe_post",
        lambda url, download_dir, use_cookies=True: {
            "entries": [photo_entry("a"), video_entry("b"), photo_entry("c")],
            "channel": "someone",
            "webpage_url": "https://www.instagram.com/p/ABC123/",
        },
    )
    monkeypatch.setattr(instagram, "download_post_videos", lambda *args: {1: video_path})

    items, caption = instagram.download_post("https://www.instagram.com/p/ABC123/", work_dir)

    assert [item.kind for item in items] == ["photo", "video", "photo"]
    assert items[1].path == video_path
    assert caption == 'Пост <a href="https://www.instagram.com/p/ABC123/">@someone</a>'


def test_download_post_warns_about_missing_audio_only_for_a_lone_video(monkeypatch, stub_downloads):
    work_dir = stub_downloads
    video_path = work_dir / "clip.mp4"
    video_path.write_bytes(b"video")
    calls = []

    monkeypatch.setattr(
        instagram,
        "probe_post",
        lambda url, download_dir, use_cookies=True: {**video_entry("b"), "channel": "someone"},
    )
    monkeypatch.setattr(instagram, "download_post_videos", lambda *args: {0: video_path})
    monkeypatch.setattr(
        media,
        "add_audio_warning_if_needed",
        lambda caption, path: calls.append(path) or f"{caption}\n\nno audio",
    )

    items, caption = instagram.download_post("https://www.instagram.com/p/ABC123/", work_dir)

    assert [item.kind for item in items] == ["video"]
    assert caption.startswith("Рилс ")
    assert calls == [video_path]
    assert caption.endswith("no audio")


def test_download_post_skips_the_audio_warning_for_a_carousel(monkeypatch, stub_downloads):
    work_dir = stub_downloads
    video_path = work_dir / "clip.mp4"
    video_path.write_bytes(b"video")

    monkeypatch.setattr(
        instagram,
        "probe_post",
        lambda url, download_dir, use_cookies=True: {"entries": [video_entry("b"), photo_entry("c")]},
    )
    monkeypatch.setattr(instagram, "download_post_videos", lambda *args: {0: video_path})
    monkeypatch.setattr(
        media,
        "add_audio_warning_if_needed",
        lambda caption, path: pytest.fail("a carousel must not get the audio warning"),
    )

    items, _ = instagram.download_post("https://www.instagram.com/p/ABC123/", work_dir)

    assert len(items) == 2


def test_a_photo_that_would_not_download_is_not_taken_for_a_post_without_media(monkeypatch, tmp_path):
    # It used to be: the user was told the post had no photo in it at all.
    monkeypatch.setattr(
        instagram, "probe_post", lambda url, download_dir, use_cookies=True: {"entries": [photo_entry("a")]}
    )
    monkeypatch.setattr(instagram, "download_photo", lambda entry, index, download_dir: None)

    with pytest.raises(instagram.IncompletePostError):
        instagram.download_post("https://www.instagram.com/p/ABC123/", tmp_path)


def test_download_post_reports_an_empty_post(monkeypatch, tmp_path):
    monkeypatch.setattr(instagram, "probe_post", lambda url, download_dir, use_cookies=True: {"entries": []})

    with pytest.raises(instagram.NoMediaInPostError):
        instagram.download_post("https://www.instagram.com/p/ABC123/", tmp_path)


def test_download_photo_refuses_a_non_http_url(tmp_path):
    entry = {"thumbnail": "file:///etc/passwd"}
    assert instagram.download_photo(entry, 0, tmp_path) is None


# --- preparing files for Telegram --------------------------------------


def test_prepare_items_for_upload_leaves_photos_alone(monkeypatch, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"photo")
    video = tmp_path / "b.mp4"
    video.write_bytes(b"video")
    compressed_video = tmp_path / "b.compressed.mp4"
    compressed_video.write_bytes(b"small")

    monkeypatch.setattr(
        media, "prepare_video_for_upload", lambda path, work_dir: (compressed_video, True)
    )

    items, compressed = media.prepare_items_for_upload(
        [media.MediaItem(photo, "photo"), media.MediaItem(video, "video")], tmp_path
    )

    assert compressed is True
    assert items == [media.MediaItem(photo, "photo"), media.MediaItem(compressed_video, "video")]


def test_ensure_items_fit_telegram_accepts_files_within_the_limits(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x" * 1024)
    video = tmp_path / "b.mp4"
    video.write_bytes(b"x" * 1024)

    media.ensure_items_fit_telegram([media.MediaItem(photo, "photo"), media.MediaItem(video, "video")])


def test_ensure_items_fit_telegram_rejects_an_oversized_video(tmp_path):
    video = tmp_path / "b.mp4"
    video.write_bytes(b"x" * (config.MAX_FILE_SIZE_BYTES + 1))

    with pytest.raises(media.MediaTooLargeError):
        media.ensure_items_fit_telegram([media.MediaItem(video, "video")])


def test_ensure_items_fit_telegram_holds_photos_to_their_own_lower_limit(tmp_path):
    # Comfortably under the video ceiling, over the photo one.
    assert config.PHOTO_MAX_FILE_SIZE_BYTES < config.MAX_FILE_SIZE_BYTES
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x" * (config.PHOTO_MAX_FILE_SIZE_BYTES + 1))

    with pytest.raises(media.MediaTooLargeError):
        media.ensure_items_fit_telegram([media.MediaItem(photo, "photo")])


def test_media_too_large_is_a_runtime_error():
    assert issubclass(media.MediaTooLargeError, RuntimeError)


# --- albums ------------------------------------------------------------


@pytest.mark.parametrize(
    "count, sizes",
    [
        (1, [1]),
        (2, [2]),
        (10, [10]),
        # Cut by ten, these would leave an album of one, which Telegram refuses.
        (11, [6, 5]),
        (21, [7, 7, 7]),
        (12, [6, 6]),
        (20, [10, 10]),
        (23, [8, 8, 7]),
    ],
)
def test_album_chunks_never_leave_an_album_of_one(count, sizes):
    chunks = delivery.album_chunks(list(range(count)))

    assert [len(chunk) for chunk in chunks] == sizes
    assert [item for chunk in chunks for item in chunk] == list(range(count))


def test_album_chunks_of_nothing_is_nothing():
    assert delivery.album_chunks([]) == []


def test_media_group_limit_matches_telegram():
    assert delivery.MEDIA_GROUP_LIMIT == 10


@pytest.mark.parametrize(
    "kind, expected",
    [("photo", "photo"), ("video", "video"), ("document", "document")],
)
def test_build_input_media_maps_each_kind(kind, expected):
    assert delivery.build_input_media(kind, "file-id", "caption").type == expected


def test_build_input_media_asks_telegram_to_stream_video():
    assert delivery.build_input_media("video", "file-id", None).supports_streaming is True


# --- inline results ----------------------------------------------------


POST_URL = "https://www.instagram.com/p/ABC123/"


def cached_post(items):
    return {
        "version": cache.INLINE_CACHE_VERSION,
        "caption": f'<a href="{POST_URL}">@someone</a>',
        "title": "@someone",
        "items": items,
    }


def test_inline_item_result_id_keeps_the_bare_id_only_for_a_single_file():
    base_id = inline.inline_result_id(POST_URL)
    assert inline.inline_item_result_id(POST_URL, 0, 1) == base_id
    # The first file of a carousel must not share the placeholder's bare id:
    # that difference is how the chosen-result handler tells them apart.
    assert inline.inline_item_result_id(POST_URL, 0, 5) == f"{base_id}-0"
    assert inline.inline_item_result_id(POST_URL, 2, 5) == f"{base_id}-2"


def test_inline_result_id_fits_telegram_limit():
    # Telegram rejects inline result ids longer than 64 bytes, and a carousel
    # position gets appended to this.
    assert len(inline.inline_result_id(POST_URL)) <= 32


def test_build_inline_results_offers_every_file_of_a_carousel():
    results = inline.build_inline_results(
        POST_URL,
        cached_post(
            [
                {"type": "photo", "file_id": "photo-1"},
                {"type": "video", "file_id": "video-1"},
                {"type": "document", "file_id": "doc-1"},
            ]
        ),
    )

    assert [result.type for result in results] == ["photo", "video", "document"]
    assert [result.title for result in results] == ["@someone - 1/3", "@someone - 2/3", "@someone - 3/3"]
    assert len({result.id for result in results}) == 3
    assert "Файл 1 из 3" in results[0].caption


def test_build_inline_results_keeps_a_single_file_plain():
    results = inline.build_inline_results(POST_URL, cached_post([{"type": "video", "file_id": "v"}]))

    assert len(results) == 1
    assert results[0].id == inline.inline_result_id(POST_URL)
    assert results[0].title == "@someone"
    assert "Файл" not in results[0].caption


def test_build_inline_results_falls_back_to_the_url_without_a_caption():
    results = inline.build_inline_results(
        POST_URL, {"items": [{"type": "photo", "file_id": "p"}]}
    )

    assert results[0].caption == POST_URL
    assert results[0].title == "Instagram"


def test_add_carousel_note_only_applies_to_a_carousel():
    assert inline.add_carousel_note_if_needed("caption", 0, 1) == "caption"
    assert "Файл 3 из 7" in inline.add_carousel_note_if_needed("caption", 2, 7)


# --- captions ----------------------------------------------------------


@pytest.mark.parametrize(
    "info",
    [
        {"channel": "someone"},
        {"username": "someone"},
        {"owner_username": "@someone"},
        {"uploader_url": "https://www.instagram.com/someone/"},
        {"channel_url": "https://instagram.com/someone"},
    ],
)
def test_build_post_caption_finds_the_author(info):
    caption = instagram.build_post_caption({**info, "webpage_url": POST_URL}, POST_URL)
    assert caption == f'Пост <a href="{POST_URL}">@someone</a>'


def test_build_post_caption_falls_back_to_the_url():
    assert instagram.build_post_caption({}, POST_URL) == f"Пост {POST_URL}"


def test_build_post_caption_takes_the_label():
    caption = instagram.build_post_caption({"channel": "someone"}, POST_URL, "Рилс")
    assert caption == f'Рилс <a href="{POST_URL}">@someone</a>'


def test_post_label_calls_a_lone_video_a_reel(tmp_path):
    video = media.MediaItem(tmp_path / "a.mp4", "video")
    photo = media.MediaItem(tmp_path / "b.jpg", "photo")

    assert instagram.post_label([video]) == "Рилс"
    assert instagram.post_label([photo]) == "Пост"
    assert instagram.post_label([video, photo]) == "Пост"
    assert instagram.post_label([video, video]) == "Пост"


def test_build_post_caption_ignores_numeric_ids_and_section_paths():
    caption = instagram.build_post_caption(
        {"uploader_id": "1234567", "uploader_url": "https://www.instagram.com/reel/ABC123/"},
        POST_URL,
    )
    assert caption == f"Пост {POST_URL}"


def test_title_from_caption_reads_the_link_text():
    assert delivery.title_from_caption(f'<a href="{POST_URL}">@someone</a>') == "@someone"
    assert delivery.title_from_caption(POST_URL) == "Instagram"


def test_add_compression_note_only_when_compressed():
    assert media.add_compression_note_if_needed("caption", False) == "caption"
    assert "сжато" in media.add_compression_note_if_needed("caption", True)
