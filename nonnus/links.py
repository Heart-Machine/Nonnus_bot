"""Recognising Instagram links: finding one in a message, its canonical
form, and the /start deep-link payload that stands for a post."""

import re
from typing import Any, Optional
from urllib.parse import urlparse


# The web app links a post opened from a profile as /<username>/p/<code>/, so
# an optional username segment may come before the post type. m.instagram.com
# is the mobile site; it answers with a redirect to the same path on www, so
# its links carry the real shortcode.
#
# The app's share sheet also hands out /share/<id>, /share/reel/<id> and
# /share/p/<id>. The <id> there is not a shortcode, and "share" is not a
# username - so those are matched on their own, and have to be resolved into
# the post they stand for before anything else is done with them (see
# instagram.resolve_share_link). The share branch is what lets /share/<id>,
# with no post type, match at all; /share/reel/<id> would match the username
# branch too, and it makes no difference which does - is_share_link tells a
# share link by its path, not by how the pattern matched it.
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/"
    r"(?:share/(?:reels?/|p/)?[A-Za-z0-9_\-]+|(?:[A-Za-z0-9._]{1,30}/)?(?:reel|reels|p|tv)/[A-Za-z0-9_\-]+)/?"
    r"(?:\?[^\s.,!?;:()\[\]{}<>'\"]+)?",
    re.IGNORECASE,
)


def find_instagram_url(text: str) -> Optional[str]:
    match = INSTAGRAM_URL_RE.search(text or "")
    return match.group(0) if match else None


def is_share_link(url: str) -> bool:
    """A link from the app's share sheet, which names no post by itself."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    return bool(parts) and parts[0].lower() == "share"


INSTAGRAM_POST_TYPES = ("p", "reel", "reels", "tv")


def normalize_post_url(url: str) -> str:
    """The canonical address of a post: https://www.instagram.com/<type>/<code>/.

    A username segment before the type is dropped, so a post links the same
    whether it was shared from a profile or not - it is one cache entry, not
    two, and one deep link. The type and code are read from the end of the
    path rather than the start, which keeps a username that happens to be
    "p" or "reel" from being taken for the type."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[-2].lower() in INSTAGRAM_POST_TYPES:
        return f"https://www.instagram.com/{parts[-2].lower()}/{parts[-1]}/"

    path = urlparse(url).path.rstrip("/") + "/"
    return f"https://www.instagram.com{path}"


# The post types that are a single video, whatever the post. A /p/ link says
# nothing of the kind: it can be photos, a carousel or a video.
REEL_POST_TYPES = ("reel", "reels", "tv")


def is_reel_link(url: str) -> bool:
    """Whether the link alone says the post is a reel. It is all there is to
    go on before Instagram has been asked."""
    parts = [part for part in urlparse(normalize_post_url(url)).path.split("/") if part]
    return len(parts) == 2 and parts[0] in REEL_POST_TYPES


# Telegram caps the parameter of a /start deep link at 64 characters.
START_PAYLOAD_MAX_LENGTH = 64


START_PAYLOAD_POST_TYPES = {"p", "reel", "reels", "tv"}


INSTAGRAM_SHORTCODE_RE = re.compile(r"[A-Za-z0-9_-]+")


def post_start_payload(url: str) -> Optional[str]:
    """Name a post in a /start deep link as `<type>_<shortcode>`.

    Telegram allows only A-Z, a-z, 0-9, `_` and `-` there - which is exactly
    the shortcode alphabet, so the shortcode fits as is, and the post can be
    downloaded again even after it has dropped out of the file_id cache. That
    leaves no spare character for a separator, but none of the post types
    contains an underscore, so splitting on the first one is unambiguous."""
    parts = [part for part in urlparse(normalize_post_url(url)).path.split("/") if part]
    if len(parts) != 2:
        return None

    post_type, shortcode = parts
    if post_type not in START_PAYLOAD_POST_TYPES or not INSTAGRAM_SHORTCODE_RE.fullmatch(shortcode):
        return None

    payload = f"{post_type}_{shortcode}"
    return payload if len(payload) <= START_PAYLOAD_MAX_LENGTH else None


def post_url_from_start_payload(payload: str) -> Optional[str]:
    """The reverse of post_start_payload. Anyone can craft a /start link, so
    nothing that does not parse as a post is turned into a URL."""
    post_type, _, shortcode = payload.partition("_")
    if post_type not in START_PAYLOAD_POST_TYPES or not INSTAGRAM_SHORTCODE_RE.fullmatch(shortcode):
        return None

    return f"https://www.instagram.com/{post_type}/{shortcode}/"


def normalize_instagram_username(value: Any) -> Optional[str]:
    if value is None:
        return None

    username = str(value).strip().lstrip("@")
    if not username or username.lower() in {"none", "unknown", "na", "n/a"}:
        return None

    if username.isdigit() or username.startswith(("http://", "https://")):
        return None

    match = re.search(r"[A-Za-z0-9._]{1,30}", username)
    if not match:
        return None

    return f"@{match.group(0)}"


def username_from_instagram_profile_url(value: Any) -> Optional[str]:
    if value is None:
        return None

    parsed_url = urlparse(str(value).strip())
    host = parsed_url.netloc.lower()
    if host not in {"instagram.com", "www.instagram.com"}:
        return None

    path_parts = [part for part in parsed_url.path.split("/") if part]
    if not path_parts:
        return None

    username = path_parts[0]
    if username.lower() in {"reel", "reels", "p", "tv", "explore", "accounts"}:
        return None

    return normalize_instagram_username(username)
