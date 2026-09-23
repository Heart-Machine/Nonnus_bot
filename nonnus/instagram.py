"""Fetching a post from Instagram with yt-dlp: probing it, downloading its
videos and photos, the cookies and the fallback without them, and the caption."""

import http.client
import logging
from html import escape
import re
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import (
    Any,
    Callable,
    Optional,
    Tuple,
    TypeVar,
)
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError, YoutubeDLError

from nonnus import config, links, media


logger = logging.getLogger(__name__)


def post_label(items: list[media.MediaItem]) -> str:
    """"Рилс" for a lone video, "Пост" for anything else - a photo or a carousel.

    Decided by what was downloaded rather than by the link: Instagram hands out
    /p/ links to reels as readily as /reel/ ones, while a single video is what
    it publishes as a reel either way."""
    return "Рилс" if len(items) == 1 and items[0].is_video else "Пост"


def build_post_caption(info: dict[str, Any], fallback_url: str, label: str = "Пост") -> str:
    post_url = info.get("webpage_url") or fallback_url
    author = next(
        (
            username
            for username in (
                links.username_from_instagram_profile_url(info.get("uploader_url")),
                links.username_from_instagram_profile_url(info.get("channel_url")),
                links.username_from_instagram_profile_url(info.get("creator_url")),
                links.username_from_instagram_profile_url(info.get("author_url")),
                links.username_from_instagram_profile_url(info.get("profile_url")),
                links.normalize_instagram_username(info.get("username")),
                links.normalize_instagram_username(info.get("owner_username")),
                links.normalize_instagram_username(info.get("channel")),
                links.normalize_instagram_username(info.get("author_id")),
                links.normalize_instagram_username(info.get("uploader_id")),
            )
            if username
        ),
        None,
    )

    if author:
        return f'{escape(label)} <a href="{escape(str(post_url), quote=True)}">{escape(author)}</a>'

    return f"{escape(label)} {escape(str(post_url))}"


class NoMediaInPostError(Exception):
    """Raised when the linked Instagram post yields no downloadable media at
    all - neither a video track nor a photo - distinct from a genuine
    download failure so the user gets an accurate message instead of being
    told to add cookies."""


class IncompletePostError(Exception):
    """Raised when some of a post's files could not be fetched.

    The post then goes nowhere rather than out with a slide missing - above
    all not into the file_id cache, which serves a post as it was saved to
    every later request: a photo lost to one network hiccup would stay lost
    for good, and without a word to anyone."""


# Instagram's CDN serves signed media URLs to anyone, but it still wants a
# plausible browser request - an unadorned urllib call gets a 403.
INSTAGRAM_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.instagram.com/",
}


# How long to go without the cookies once Instagram has turned them down,
# before trying them again. The file only changes on a deploy, and a deploy
# restarts the bot anyway, so this is for a session that recovers by itself -
# rare, but cheap to allow for.
COOKIE_RETRY_SECONDS = 60 * 60


# How often, at most, to remind the owner that the cookies stopped working.
COOKIE_ALERT_INTERVAL_SECONDS = 12 * 60 * 60


COOKIE_ALERT_TEXT = (
    "Instagram не принял cookies бота: публикацию удалось скачать только без входа.\n\n"
    "Пока бот скачивает без cookies. Публичные посты работают, а то, что требует входа, - нет.\n\n"
    "Выгрузи свежие cookies аккаунта бота, обнови INSTAGRAM_COOKIES_B64 в окружении production "
    "и запусти деплой вручную.\n\n"
    "Следующее напоминание - не раньше чем через 12 ч."
)


class InstagramSession:
    """Whether the Instagram cookies the bot was deployed with still work.

    The cookie file is written once per deploy and the bot never updates it,
    so a session Instagram has closed stays closed until the next deploy. And
    a closed session is worse than none: yt-dlp sees sessionid, takes the
    logged-in route and fails, where the logged-out route would have served a
    public post without trouble.

    Detection is indirect on purpose. A post that fails with the cookies and
    then comes through without them says the cookies were the problem; one
    that fails both ways says the post was - private or deleted - and raises no
    alarm. That also keeps this off yt-dlp's error wording, which is nothing to
    build on: a dead session currently surfaces as a JSON parse error rather
    than as anything about logging in."""

    def __init__(
        self,
        cookies_file: str,
        retry_after: float = COOKIE_RETRY_SECONDS,
        alert_every: float = COOKIE_ALERT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cookies_file = cookies_file
        self.retry_after = retry_after
        self.alert_every = alert_every
        self._clock = clock
        # Downloads run in worker threads, several at a time.
        self._lock = threading.Lock()
        self._suspended_until = float("-inf")
        self._last_alert = float("-inf")
        self._alert_pending = False

    def use_cookies(self) -> bool:
        with self._lock:
            return bool(self.cookies_file) and self._clock() >= self._suspended_until

    def mark_rejected(self) -> None:
        now = self._clock()
        with self._lock:
            self._suspended_until = now + self.retry_after
            if now - self._last_alert >= self.alert_every:
                self._last_alert = now
                self._alert_pending = True

    def take_alert(self) -> bool:
        with self._lock:
            pending, self._alert_pending = self._alert_pending, False
            return pending


INSTAGRAM_SESSION = InstagramSession(config.COOKIES_FILE)


def build_ydl_opts(download_dir: Path, use_cookies: bool = True) -> dict[str, Any]:
    ydl_opts: dict[str, Any] = {
        "outtmpl": str(download_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        # quiet does not cover the progress bar, which otherwise lands in the
        # container log as a run of carriage returns for every file.
        "noprogress": True,
        # Instagram extracts a carousel as a playlist of children, and we
        # want every child, not just the first one.
        "noplaylist": False,
    }

    if use_cookies and config.COOKIES_FILE:
        source_cookiefile = Path(config.COOKIES_FILE)
        cookiefile = download_dir / source_cookiefile.name
        shutil.copyfile(source_cookiefile, cookiefile)
        ydl_opts["cookiefile"] = str(cookiefile)

    return ydl_opts


def probe_post(url: str, download_dir: Path, use_cookies: bool = True) -> dict[str, Any]:
    """Metadata-only pass over the post.

    yt-dlp builds no `formats` for Instagram photos - their URLs only ever
    surface as thumbnails - so a plain download run dies with "No video
    formats found" on any post that isn't pure video. ignore_no_formats_error
    lets those entries through, which is what makes photo posts and mixed
    carousels visible to us at all."""
    ydl_opts = build_ydl_opts(download_dir, use_cookies)
    ydl_opts["ignore_no_formats_error"] = True

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise NoMediaInPostError("Instagram returned nothing for this post")

    return info


T = TypeVar("T")


def with_cookie_fallback(url: str, attempt: Callable[[bool], T]) -> Tuple[T, bool]:
    """Run attempt(use_cookies) with the session cookies while they are
    trusted, and again without them when that fails. Returns the result and
    whether the cookies were used.

    Both yt-dlp passes over a post go through this, not only the first,
    because Instagram does not answer a dead session the same way twice.
    Sometimes yt-dlp spots the redirect to the login page and quietly drops
    the cookies itself; sometimes it gets an empty page and fails on the JSON.
    So the probe can come through on the cookies and the video pass right
    after it still fail on them."""
    if not INSTAGRAM_SESSION.use_cookies():
        return attempt(False), False

    try:
        return attempt(True), True
    except YoutubeDLError as cookie_error:
        try:
            result = attempt(False)
        except YoutubeDLError:
            # Failed both ways: the post is the problem - private, deleted -
            # not the session. Report what the primary route said.
            raise cookie_error from None

    logger.warning(
        "Instagram turned down the session cookies for %s but served it without them; "
        "going without cookies for %d min. Refresh INSTAGRAM_COOKIES_B64.",
        url,
        COOKIE_RETRY_SECONDS // 60,
    )
    INSTAGRAM_SESSION.mark_rejected()
    return result, False


def probe_post_with_fallback(url: str, download_dir: Path) -> Tuple[dict[str, Any], bool]:
    return with_cookie_fallback(url, lambda use_cookies: probe_post(url, download_dir, use_cookies))


def post_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Carousel children in display order, or the post itself when it holds
    a single medium."""
    entries = info.get("entries")
    if entries is None:
        return [info]

    return [entry for entry in list(entries) if entry]


def entry_has_video(entry: dict[str, Any]) -> bool:
    return any(fmt.get("url") for fmt in entry.get("formats") or [])


def downloaded_entry_path(entry: dict[str, Any]) -> Optional[Path]:
    for requested_download in entry.get("requested_downloads") or []:
        filepath = requested_download.get("filepath") or requested_download.get("_filename")
        if filepath and Path(filepath).exists():
            return Path(filepath)

    return None


def download_post_videos(
    url: str,
    download_dir: Path,
    entries: list[dict[str, Any]],
    video_indices: list[int],
    use_cookies: bool = True,
) -> dict[int, Path]:
    """Download only the carousel positions that actually carry a video.

    playlist_items is 1-based and keeps the photo entries out of the run
    entirely, so the format selector never has to cope with an entry that has
    no formats at all. Returns a map from carousel position to file."""
    ydl_opts = build_ydl_opts(download_dir, use_cookies)
    ydl_opts["format"] = (
        "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
        "best[vcodec^=avc1][ext=mp4]/"
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    )
    ydl_opts["merge_output_format"] = "mp4"
    if len(entries) > 1:
        ydl_opts["playlist_items"] = ",".join(str(index + 1) for index in video_indices)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # The download pass returns the selected entries in the order we asked
    # for them, so position N of the result is carousel item video_indices[N].
    video_paths: dict[int, Path] = {}
    for position, entry in enumerate(post_entries(info or {})):
        if position >= len(video_indices):
            break

        video_path = downloaded_entry_path(entry)
        if video_path:
            video_paths[video_indices[position]] = video_path

    if not video_paths and len(video_indices) == 1:
        # yt-dlp didn't report a filepath: fall back to the largest video
        # file it left behind (the cookie copy shares this directory, hence
        # the extension filter).
        candidates = sorted(
            (
                item
                for item in download_dir.iterdir()
                if item.is_file() and item.suffix.lower() in media.VIDEO_FILE_EXTENSIONS
            ),
            key=lambda item: item.stat().st_size,
            reverse=True,
        )
        if candidates:
            video_paths[video_indices[0]] = candidates[0]

    return video_paths


# How Instagram marks a photo variant in its CDN URL: a crop directive,
# c<x>.<y>.<width>.<height>a - c0.240.1440.1440a is the square cut from row 240
# of a 1440x1920 frame - and a size bound, s<w>x<h> or p<w>x<h>, for a
# scaled-down copy. Both live in the stp query parameter, or as path segments
# on older links.
PHOTO_CROP_RE = re.compile(r"(?:^|_)c\d+\.\d+\.\d+\.\d+a(?:_|$)")


PHOTO_SIZE_BOUND_RE = re.compile(r"(?:^|_)[sp](\d+)x(\d+)(?:_|$)")


def photo_variant_markers(url: str) -> str:
    parsed = urlparse(url)
    stp = parse_qs(parsed.query).get("stp", [""])[0]
    return f"{stp}_{parsed.path.replace('/', '_')}"


def best_photo_url(entry: dict[str, Any]) -> Optional[str]:
    """The full frame of a photo, at the largest size Instagram offers.

    Next to the full frame Instagram lists square crops made for the profile
    grid. Logged in, every variant comes with its size; logged out, none do -
    only URLs, in an order that cannot be relied on: one post leads with the
    original, the next with a 1080x1080 crop of a 1440x1920 photo. So a crop
    is recognised by its marker in the URL and ruled out, and among the rest
    the real size decides when it is known, otherwise the size bound in the
    URL, with a variant that has no bound at all - the original upload -
    ranked above every scaled-down copy."""
    thumbnails = [thumbnail for thumbnail in entry.get("thumbnails") or [] if thumbnail.get("url")]
    if not thumbnails:
        thumbnail_url = entry.get("thumbnail")
        return str(thumbnail_url) if thumbnail_url else None

    def rank(thumbnail: dict[str, Any]) -> Tuple[bool, float]:
        markers = photo_variant_markers(str(thumbnail["url"]))
        is_full_frame = PHOTO_CROP_RE.search(markers) is None
        width, height = thumbnail.get("width"), thumbnail.get("height")
        if width and height:
            return is_full_frame, width * height

        bound = PHOTO_SIZE_BOUND_RE.search(markers)
        return is_full_frame, int(bound.group(1)) * int(bound.group(2)) if bound else float("inf")

    return str(max(thumbnails, key=rank)["url"])


def download_photo(entry: dict[str, Any], index: int, download_dir: Path) -> Optional[Path]:
    """Fetch a carousel photo straight from its CDN URL, since yt-dlp has no
    downloader for an entry it never built formats for."""
    photo_url = best_photo_url(entry)
    if not photo_url:
        return None

    parsed_url = urlparse(photo_url)
    if parsed_url.scheme not in {"http", "https"}:
        logger.warning("Skipping photo %s with unsupported URL scheme %r", index, parsed_url.scheme)
        return None

    suffix = Path(parsed_url.path).suffix.lower()
    if suffix not in media.DOWNLOADABLE_PHOTO_EXTENSIONS:
        suffix = ".jpg"
    photo_path = download_dir / f"photo-{index:02d}{suffix}"

    headers = dict(INSTAGRAM_IMAGE_HEADERS)
    headers.update(entry.get("http_headers") or {})

    for attempt in range(1, PHOTO_DOWNLOAD_ATTEMPTS + 1):
        if fetch_photo(photo_url, headers, photo_path):
            return photo_path
        logger.warning("Photo %s of the post: attempt %d of %d failed", index, attempt, PHOTO_DOWNLOAD_ATTEMPTS)

    return None


# A failed photo now fails the whole post, so a single dropped connection is
# worth one more try before that.
PHOTO_DOWNLOAD_ATTEMPTS = 2


def fetch_photo(url: str, headers: dict[str, str], photo_path: Path) -> bool:
    """One try at fetching a photo into photo_path; False if it did not arrive
    whole.

    That includes a body that ends early. Python raises nothing when a
    response stops short of its Content-Length and the server closes the
    connection cleanly - the read just ends - so without the check here the
    first part of a photo would go out, and into the cache, as the photo."""
    try:
        request = urllib.request.Request(url, headers=headers)
        with (
            urllib.request.urlopen(request, timeout=config.PHOTO_DOWNLOAD_TIMEOUT_SECONDS) as response,
            photo_path.open("wb") as photo_file,
        ):
            shutil.copyfileobj(response, photo_file)
            expected_size = response.headers.get("Content-Length")
    # HTTPException is not an OSError: a chunked body cut short raises
    # IncompleteRead, which would otherwise take the whole post down.
    except (OSError, ValueError, http.client.HTTPException):
        logger.exception("Failed to download %s", photo_path.name)
        return False

    size = photo_path.stat().st_size if photo_path.exists() else 0
    if size == 0:
        return False

    if expected_size and expected_size.strip().isdigit() and int(expected_size) != size:
        logger.warning("%s came up short: %d of %s bytes", photo_path.name, size, expected_size.strip())
        return False

    return True


def download_post(url: str, download_dir: Path) -> Tuple[list[media.MediaItem], str]:
    """Download every piece of media in an Instagram post - a Reel, a single
    video or photo, or a carousel mixing both - in the order the post shows
    them."""
    # Canonicalize to instagram.com: yt-dlp's Instagram extractor only
    # recognizes that domain, not aliases like instagr.am.
    url = links.normalize_post_url(url)

    try:
        info, _ = probe_post_with_fallback(url, download_dir)
    except ExtractorError as error:
        if "no video formats" in str(error).lower():
            raise NoMediaInPostError("This Instagram post has no downloadable media.") from error
        raise

    entries = post_entries(info)
    if not entries:
        raise NoMediaInPostError("This Instagram post has no downloadable media.")

    video_indices = [index for index, entry in enumerate(entries) if entry_has_video(entry)]
    video_paths: dict[int, Path] = {}
    if video_indices:
        # If the probe had to fall back, the cookies are suspended by now and
        # this goes without them straight away; otherwise it gets its own
        # fallback, for the reason given in with_cookie_fallback.
        video_paths, _ = with_cookie_fallback(
            url,
            lambda use_cookies: download_post_videos(url, download_dir, entries, video_indices, use_cookies),
        )

    items: list[media.MediaItem] = []
    missing: list[int] = []
    for index, entry in enumerate(entries):
        if index in video_paths:
            items.append(media.MediaItem(media.ensure_h264_video(video_paths[index], download_dir), "video"))
            continue

        if index in video_indices:
            logger.warning("yt-dlp downloaded no file for video %s of %s", index + 1, url)
            missing.append(index)
            continue

        photo_path = download_photo(entry, index, download_dir)
        if photo_path is None:
            missing.append(index)
            continue

        items.append(media.MediaItem(media.prepare_photo_for_upload(photo_path, download_dir), "photo"))

    if missing:
        positions = ", ".join(str(index + 1) for index in missing)
        raise IncompletePostError(f"Could not fetch file(s) {positions} of {len(entries)} in {url}")

    caption = build_post_caption(info, url, post_label(items))
    if len(items) == 1 and items[0].is_video:
        # Only meaningful for a lone video: in a carousel a silent clip next
        # to photos is normal, not a symptom of a stripped audio track.
        caption = media.add_audio_warning_if_needed(caption, items[0].path)

    return items, caption
