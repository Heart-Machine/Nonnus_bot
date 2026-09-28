"""Fetching a post from Instagram with yt-dlp: probing it, downloading its
videos and photos, the cookies and the fallback without them, and the caption."""

import copy
import http.client
import logging
from html import escape
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import (
    Any,
    Callable,
    Optional,
    Tuple,
    TypeVar,
)
from urllib.parse import parse_qs, urljoin, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.extractor.instagram import InstagramStoryIE
from yt_dlp.utils import ExtractorError, YoutubeDLError

from nonnus import cache, config, links, media, progress


logger = logging.getLogger(__name__)


def post_label(items: list[media.MediaItem]) -> str:
    """"Рилс" for a lone video, "Пост" for anything else - a photo or a carousel.

    Decided by what was downloaded rather than by the link: Instagram hands out
    /p/ links to reels as readily as /reel/ ones, while a single video is what
    it publishes as a reel either way."""
    return "Рилс" if len(items) == 1 and items[0].is_video else "Пост"


def post_kind(total: int, has_video: bool) -> str:
    """What the post is, for the progress: known from the probe, before any
    file is down. The same call post_label makes from the files afterwards."""
    if total > 1:
        return progress.CAROUSEL
    return progress.REEL if has_video else progress.PHOTO


def story_label(kind: str, info: dict[str, Any]) -> str:
    if kind == links.HIGHLIGHT:
        title = info.get("title")
        return f"Хайлайт «{title}»" if title else "Хайлайт"
    return "Сторис"


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


# A share link answers a desktop browser with the web app's JavaScript shell -
# the very same page whether the link exists or not - but a phone, or a link
# preview crawler, with a redirect to the post itself. Checked against
# Instagram: /share/<id>, /share/reel/<id> and /share/p/<id> all 302 to
# /reel/<shortcode>/ for an iPhone Safari user agent, and a made-up id gets
# the shell whatever the agent. So a share link is asked about as a phone.
SHARE_LINK_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
SHARE_LINK_TIMEOUT_SECONDS = 10
# instagram.com and m.instagram.com first send a share link on to www, then
# www sends it to the post: two hops, with one to spare.
SHARE_LINK_MAX_REDIRECTS = 3
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def redirect_target(url: str) -> Optional[str]:
    """Where `url` redirects to, without going there; None if it answers
    with a page instead."""
    request = urllib.request.Request(url, headers={"User-Agent": SHARE_LINK_USER_AGENT}, method="HEAD")
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=SHARE_LINK_TIMEOUT_SECONDS):
            return None
    except urllib.error.HTTPError as error:
        with error:
            location = error.headers.get("Location")
            if error.code in (301, 302, 303, 307, 308) and location:
                return urljoin(url, location)
        return None


def resolve_share_link(url: str) -> Optional[str]:
    """The post a share link stands for, as a link to it; None when Instagram
    would not say.

    Only redirects within Instagram are followed - a share link is not an
    open door to wherever its answer points - and only a few of them."""
    current = url
    for _ in range(SHARE_LINK_MAX_REDIRECTS):
        try:
            target = redirect_target(current)
        except (OSError, ValueError, http.client.HTTPException):
            logger.exception("Failed to resolve the share link %s", url)
            return None

        if target is None:
            logger.warning("Instagram answered the share link %s without saying which post it is", url)
            return None
        if urlparse(target).netloc.lower() not in INSTAGRAM_HOSTS:
            logger.warning("The share link %s redirects away from Instagram, to %s; not following", url, target)
            return None

        post = links.find_instagram_url(target)
        if post and not links.is_share_link(post):
            return post
        current = target

    logger.warning("The share link %s kept redirecting without reaching a post", url)
    return None




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


class StoryIE(InstagramStoryIE):
    """Stories and highlights from Instagram's API alone.

    yt-dlp's own story extractor falls short twice. It reads the author from
    the story's web page before anything else, and logged in, Instagram sends
    that page to its home page instead - seen from the server on a live story
    of an open account the bot follows, with a session the API accepted - so
    it gives up with "This content is unreachable". And it keeps only the
    items with video formats: a highlight tried held 95 items, 59 videos and
    36 photos, and it returned the 59.

    So here, with no page at all:
    - one story is media/<id>/info, the request yt-dlp itself makes for a
      post when logged in;
    - a highlight is reels_media by its id;
    - someone's current stories are reels_media by their account id, which
      is kept once known (cache.AccountIds) and else found by username.

    Each item is still built by yt-dlp (_extract_product) and requested
    through its machinery - cookies, headers, impersonation - and a photo
    comes out the way a post's photo does: its pictures under thumbnails, no
    formats. One request for a story or a highlight, and for someone's
    stories once their id is known - which counts, with an account Instagram
    holds back quickly."""

    IE_NAME = "nonnus:instagram:story"

    def _api(self, path: str, item_id: str) -> Any:
        return self._download_json(
            f"{self._API_BASE_URL}/{path}", item_id, headers=self._api_headers,
            impersonate=self._can_impersonate and self._is_web_app,
        )

    def _items(self, reel: dict[str, Any]) -> list[dict[str, Any]]:
        """The reel's items as yt-dlp builds them - each with the reel's
        author. reels_media names the author on the reel rather than on every
        item, and _extract_product takes the author (channel) from the item's
        own `user`: without this someone's stories came out captioned with the
        bare link instead of "Сторис @автор". yt-dlp's own story extractor
        fills the items in the same way; what an item says of itself wins."""
        owner = reel.get("user") or {}
        return [
            self._extract_product({**item, "user": {**owner, **(item.get("user") or {})}}, get_comments=False)
            for item in reel.get("items") or []
        ]

    @staticmethod
    def _keep_owner_id(user: Any) -> None:
        """Keep the id of the account a story or a highlight came from: asked
        for all their stories later, the bot has no lookup to make."""
        if isinstance(user, dict) and user.get("username") and (user.get("pk") or user.get("id")):
            cache.remember_account_id(user["username"], str(user.get("pk") or user.get("id")))

    def _account_reel(self, user_id: str, username: str) -> dict[str, Any]:
        answer = self._api(f"feed/reels_media/?reel_ids={user_id}", username) or {}
        return (answer.get("reels") or {}).get(str(user_id)) or {}

    @staticmethod
    def _someone_else_s(reel: dict[str, Any], username: str) -> bool:
        owner = str((reel.get("user") or {}).get("username") or "")
        return bool(owner) and owner.lower() != username.lower()

    def _find_account_id(self, username: str) -> str:
        """Someone's account id, found the way gallery-dl finds it: Instagram's
        search, then the profile page, which carries it as profile_id and
        also has the accounts the search leaves out.

        Not web_profile_info, which this was asked of before. From the server
        it answered 429 with the session and without it alike, while a post
        went through on the same session within a minute; gallery-dl users
        have seen the same since February 2026, and gallery-dl moved to the
        search and the page. The search and the page were tried from the
        server on that same account and both gave its id."""
        # The warnings carry yt-dlp's own words without its plea to report a bug:
        # a refusal from Instagram is not one.
        try:
            found = self._download_json(
                f"{self._BASE_URL}web/search/topsearch/?query={username}", username, "Looking the account up",
                headers=self._api_headers, impersonate=self._can_impersonate and self._is_web_app,
            )
        except ExtractorError as error:
            logger.warning("Instagram's search failed for %s: %s", username, error.orig_msg)
            found = None
        for entry in (found or {}).get("users") or []:
            user = entry.get("user") or {}
            if str(user.get("username") or "").lower() == username.lower() and user.get("pk"):
                return str(user["pk"])

        try:
            page = self._download_webpage(f"{self._BASE_URL}{username}/", username, "Reading the profile page")
        except ExtractorError as error:
            logger.warning("Instagram's profile page failed for %s: %s", username, error.orig_msg)
            page = ""
        profile_id = re.search(r'"profile_id":"(\d+)"', page or "")
        if profile_id:
            return profile_id.group(1)

        raise ExtractorError(f"Instagram gave no id for {username}", expected=True)

    def _real_extract(self, url):
        username, story_id = self._match_valid_url(url).group("user", "id")
        if username == "highlights":
            reel = ((self._api(f"feed/reels_media/?reel_ids=highlight:{story_id}", story_id) or {}).get("reels") or {}).get(
                f"highlight:{story_id}"
            ) or {}
            self._keep_owner_id(reel.get("user"))
            return self.playlist_result(self._items(reel), story_id, reel.get("title"))

        if story_id:
            items = (self._api(f"media/{story_id}/info/", story_id) or {}).get("items") or []
            if not items:
                raise ExtractorError("Instagram has no such story", expected=True)
            self._keep_owner_id(items[0].get("user"))
            return self._extract_product(items[0], get_comments=False)

        user_id = cache.known_account_id(username)
        reel = self._account_reel(user_id, username) if user_id else None
        # Stories under another name: the username has passed to someone
        # else since its id was kept. With no stories there is no name to
        # tell by, and the kept id stands.
        if reel is None or self._someone_else_s(reel, username):
            found = self._find_account_id(username)
            if found != user_id:
                reel = self._account_reel(found, username)
            cache.remember_account_id(username, found)
        return self.playlist_result(self._items(reel), username, f"Story by {username}")


def probe_post(url: str, download_dir: Path, use_cookies: bool = True) -> dict[str, Any]:
    """The one request to Instagram's API for a post: what the extractor
    returns, unprocessed.

    Unprocessed, because this is also what the videos are downloaded from
    (download_post_videos), and processing picks a format - yt-dlp's default
    one here, which a later pass would keep rather than apply ours. The raw
    result has everything used from it: the entries, their formats and
    thumbnails, the author.

    yt-dlp builds no `formats` for Instagram photos - their URLs only ever
    surface as thumbnails. Processing a photo entry would die with "No video
    formats found"; unprocessed, and with ignore_no_formats_error should
    anything process it, photo posts and mixed carousels come through."""
    ydl_opts = build_ydl_opts(download_dir, use_cookies)
    ydl_opts["ignore_no_formats_error"] = True

    with YoutubeDL(ydl_opts) as ydl:
        if links.story_kind(url) is None:
            info = ydl.extract_info(url, download=False, process=False)
        else:
            ydl.add_info_extractor(StoryIE())
            info = ydl.extract_info(url, download=False, process=False, ie_key=StoryIE.ie_key())

    if info and info.get("entries") is not None:
        # A generator would be used up by the first look at the entries,
        # leaving nothing for the video pass.
        info["entries"] = list(info["entries"])

    if not info:
        raise NoMediaInPostError("Instagram returned nothing for this post")

    return info


T = TypeVar("T")


# Instagram shows a story, and someone's current stories, only to an account
# that is logged in: they go with the session alone. A highlight goes the way
# a post does - some accounts' highlights open logged out.
LOGIN_ONLY_STORY_KINDS = (links.STORY, links.STORIES)


def with_session_fallback(url: str, attempt: Callable[[bool], T]) -> Tuple[T, bool]:
    """Run attempt(use_cookies) logged out first, and with the session when
    that fails and there is one. Returns the result and whether the session
    was used.

    Logged out first, so that the session makes as few requests as it can.
    Instagram holds back an account that downloads for everyone: two in a
    row were answered 429 on the search and given empty story lists within
    hours of going on the bot, while a public post comes through logged out.
    The session is kept for what needs it - stories, which go with it alone,
    and whatever Instagram will not serve logged out.

    Failing both ways reports the logged-out error: the post is the problem -
    private, deleted - or both routes are down. Whether the session itself
    still works is the daily check's to find out (canary), not a guess from
    one post.

    Only the probe goes through this - the one request to Instagram's API a
    post takes. The videos are downloaded from what it returned, straight
    from the CDN."""
    has_session = bool(config.COOKIES_FILE)
    if links.story_kind(url) in LOGIN_ONLY_STORY_KINDS:
        return attempt(has_session), has_session

    try:
        return attempt(False), False
    except YoutubeDLError as logged_out_error:
        if not has_session:
            raise
        try:
            result = attempt(True)
        except YoutubeDLError:
            raise logged_out_error from None

    # How often the session is still needed: the measure of whether logged
    # out is enough from the server's address.
    logger.info("Instagram served %s only to the session", url)
    return result, True


def probe_post_with_fallback(url: str, download_dir: Path) -> Tuple[dict[str, Any], bool]:
    return with_session_fallback(url, lambda use_cookies: probe_post(url, download_dir, use_cookies))


def session_error(url: str, download_dir: Path) -> Optional[YoutubeDLError]:
    """What stops Instagram from serving url to the session - None when it
    serves it. For the daily check: posts no longer tell, since they only go
    with the session once the logged-out route has failed. Meant for when
    there is a session: without one this is just a logged-out probe."""
    try:
        probe_post(url, download_dir, use_cookies=True)
    except YoutubeDLError as error:
        return error
    return None


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
    info: dict[str, Any],
    download_dir: Path,
    entries: list[dict[str, Any]],
    video_indices: list[int],
    use_cookies: bool = True,
) -> dict[int, Path]:
    """Download only the carousel positions that actually carry a video, from
    the probe's result rather than by asking Instagram's API again.

    That second request used to be how this worked, and on a server it was a
    second chance for Instagram to answer with nothing: the probe came
    through, and a second later the same post came back as "an empty media
    response". From the probe's result it is one request per post, and the
    files come from the CDN URLs already in it.

    playlist_items is 1-based and keeps the photo entries out of the run
    entirely, so the format selector never has to cope with an entry that has
    no formats at all. The result is processed from a copy, since processing
    writes into it and the caller still reads the photos from the original.
    Returns a map from carousel position to file."""
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
        result = ydl.process_ie_result(copy.deepcopy(info), download=True)

    # The download pass returns the selected entries in the order we asked
    # for them, so position N of the result is carousel item video_indices[N].
    video_paths: dict[int, Path] = {}
    for position, entry in enumerate(post_entries(result or {})):
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
        info, probe_used_cookies = probe_post_with_fallback(url, download_dir)
    except ExtractorError as error:
        if "no video formats" in str(error).lower():
            raise NoMediaInPostError("This Instagram post has no downloadable media.") from error
        raise

    entries = post_entries(info)
    if not entries:
        raise NoMediaInPostError("This Instagram post has no downloadable media.")

    story = links.story_kind(url)
    total = len(entries)
    video_indices = [index for index, entry in enumerate(entries) if entry_has_video(entry)]
    if story is None:
        kind = post_kind(total, bool(video_indices))
    else:
        kind = progress.HIGHLIGHT if story == links.HIGHLIGHT else progress.STORY
    progress.report(progress.DOWNLOADING, 0, total, kind)
    video_paths: dict[int, Path] = {}
    if video_indices:
        # From the probe's result, the way the probe got it: with the
        # cookies if it came through on them, without if it was served
        # logged-out. No request to the API, so nothing to fall back from -
        # and a post counts against the cookies once, not twice.
        video_paths = download_post_videos(info, download_dir, entries, video_indices, use_cookies=probe_used_cookies)

    # The videos come down in one yt-dlp run, the photos one by one after it,
    # so that is how the count moves.
    done = len(video_paths)
    progress.report(progress.DOWNLOADING, done, total, kind)
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
        done += 1
        progress.report(progress.DOWNLOADING, done, total, kind)

    if missing:
        positions = ", ".join(str(index + 1) for index in missing)
        raise IncompletePostError(f"Could not fetch file(s) {positions} of {len(entries)} in {url}")

    if story is None:
        caption = build_post_caption(info, url, post_label(items))
    else:
        # A playlist of stories names its author only on the items.
        author = {"channel": info.get("channel") or entries[0].get("channel")}
        caption = build_post_caption({**info, **author}, url, story_label(story, info))
    if len(items) == 1 and items[0].is_video:
        # Only meaningful for a lone video: in a carousel a silent clip next
        # to photos is normal, not a symptom of a stripped audio track.
        caption = media.add_audio_warning_if_needed(caption, items[0].path)

    return items, caption
