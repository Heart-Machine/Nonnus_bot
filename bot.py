import asyncio
import hashlib
from html import escape, unescape
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Tuple, TypeVar
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResult,
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputMessageContent,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError, YoutubeDLError


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int_list(name: str, default: list[int]) -> list[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return default

    result = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))

    return result or default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_TIMEOUT_SECONDS", "180"))
ENABLE_VIDEO_COMPRESSION = env_bool("ENABLE_VIDEO_COMPRESSION", True)
VIDEO_COMPRESSION_TARGET_MB = int(os.getenv("VIDEO_COMPRESSION_TARGET_MB", str(max(MAX_FILE_SIZE_MB - 1, 1))))
VIDEO_COMPRESSION_TARGET_BYTES = VIDEO_COMPRESSION_TARGET_MB * 1024 * 1024
VIDEO_COMPRESSION_HEIGHTS = env_int_list("VIDEO_COMPRESSION_HEIGHTS", [1280, 854, 640])
VIDEO_COMPRESSION_AUDIO_KBPS = int(os.getenv("VIDEO_COMPRESSION_AUDIO_KBPS", "96"))
VIDEO_COMPRESSION_PRESET = os.getenv("VIDEO_COMPRESSION_PRESET", "veryfast").strip() or "veryfast"
VIDEO_COMPRESSION_MIN_VIDEO_KBPS = int(os.getenv("VIDEO_COMPRESSION_MIN_VIDEO_KBPS", "250"))
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID", "").strip()
# Telegram's sendPhoto limit sits far below the video one, and Instagram
# serves photos as full-size originals, so photos need their own ceiling.
PHOTO_MAX_FILE_SIZE_MB = int(os.getenv("PHOTO_MAX_FILE_SIZE_MB", "10"))
PHOTO_MAX_FILE_SIZE_BYTES = PHOTO_MAX_FILE_SIZE_MB * 1024 * 1024
PHOTO_MAX_DIMENSION = int(os.getenv("PHOTO_MAX_DIMENSION", "2560"))
PHOTO_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("PHOTO_DOWNLOAD_TIMEOUT_SECONDS", "60"))
# Telegram accepts at most 10 items in a single album.
MEDIA_GROUP_LIMIT = 10
UPLOAD_TIMEOUTS: dict[str, Any] = {
    "read_timeout": UPLOAD_TIMEOUT_SECONDS,
    "write_timeout": UPLOAD_TIMEOUT_SECONDS,
    "connect_timeout": 30,
    "pool_timeout": 30,
}
# Bumped to 5: captions now start with "Пост" or "Рилс", and entries cached
# before that would keep the old caption for as long as they stay cached.
# Bumped to 4: a cache entry now holds a list of media items instead of one
# file_id, so entries written by older versions can't be reused.
INLINE_CACHE_VERSION = "5"
INLINE_CACHE_DB = Path(os.getenv("INLINE_CACHE_DB", str(BASE_DIR / ".inline_cache.sqlite3"))).expanduser()
if not INLINE_CACHE_DB.is_absolute():
    INLINE_CACHE_DB = BASE_DIR / INLINE_CACHE_DB
# The JSON file the cache was kept in before INLINE_CACHE_DB. It is read once,
# when the database is created, so the posts cached in it carry over.
INLINE_CACHE_FILE = Path(os.getenv("INLINE_CACHE_FILE", str(BASE_DIR / ".inline_cache.json"))).expanduser()
if not INLINE_CACHE_FILE.is_absolute():
    INLINE_CACHE_FILE = BASE_DIR / INLINE_CACHE_FILE

# The web app links a post opened from a profile as /<username>/p/<code>/, so
# an optional username segment may come before the post type.
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:[A-Za-z0-9._]{1,30}/)?(?:reel|reels|p|tv)/[A-Za-z0-9_\-]+/?"
    r"(?:\?[^\s.,!?;:()\[\]{}<>'\"]+)?",
    re.IGNORECASE,
)

# A small placeholder photo (assets/inline_placeholder.jpg: a download
# icon plus "Готовлю видео..." caption text) shown as the inline result
# while a Reel is being downloaded, so the query doesn't have to be
# retyped once it's ready. Note: InlineQueryResultCachedPhoto's
# title/description fields are NOT rendered by any major Telegram client
# for photo-type inline results (confirmed via python-telegram-bot#2115
# and telegramdesktop/tdesktop#7310) - the only text/graphics that
# actually show up are whatever is baked into the image itself, which is
# why this is a drawn icon rather than relying on API metadata fields.
# See get_placeholder_photo_file_id() and handle_chosen_inline_result().
INLINE_PLACEHOLDER_IMAGE_PATH = BASE_DIR / "assets" / "inline_placeholder.jpg"
INLINE_PLACEHOLDER_IMAGE_BYTES = INLINE_PLACEHOLDER_IMAGE_PATH.read_bytes()


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def find_instagram_url(text: str) -> Optional[str]:
    match = INSTAGRAM_URL_RE.search(text or "")
    return match.group(0) if match else None


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


def inline_result_id(url: str) -> str:
    return hashlib.sha256(f"{INLINE_CACHE_VERSION}:{normalize_post_url(url)}".encode("utf-8")).hexdigest()[:32]


class PostCache:
    """The file_id cache: the files a post went to the storage chat as, keyed
    by its normalized URL, so it can be sent again without a download.

    It lives in SQLite. A lookup reads one row by its key, however many posts
    are cached, and a write is a transaction - a crash or a container restart
    in the middle loses that one write, not the cache. WAL mode keeps a
    reader and a writer out of each other's way, so the database can be
    looked at and edited by hand while the bot runs.

    The connection is opened on first use, not at import, and in autocommit
    mode: every read sees the latest state, a row deleted by hand included,
    and holds no transaction open behind it. Every call is one statement on
    the primary key, quick enough to run on the event loop."""

    def __init__(self, path: Path, legacy_json: Optional[Path] = None) -> None:
        self.path = path
        self.legacy_json = legacy_json
        self._connection: Optional[sqlite3.Connection] = None

    def get(self, url: str, version: str) -> Optional[dict[str, Any]]:
        row = self._connect().execute(
            "SELECT result FROM posts WHERE url = ? AND version = ?", (url, version)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, url: str, version: str, result: dict[str, Any]) -> None:
        self._connect().execute(
            "INSERT INTO posts (url, version, result) VALUES (?, ?, ?)"
            " ON CONFLICT (url) DO UPDATE SET"
            " version = excluded.version, result = excluded.result, saved_at = CURRENT_TIMESTAMP",
            (url, version, json.dumps(result, ensure_ascii=False)),
        )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The timeout is how long to wait on a lock someone editing the file
        # by hand holds, before giving up on the statement.
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            # The table and the posts carried over from the JSON file appear
            # together or not at all: a start that dies halfway through
            # leaves nothing behind, and the next one imports again.
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'posts'").fetchone():
                    connection.execute(
                        "CREATE TABLE posts ("
                        " url TEXT PRIMARY KEY,"
                        " version TEXT NOT NULL,"
                        " result TEXT NOT NULL,"
                        " saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                    )
                    self._import_legacy_json(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except BaseException:
            connection.close()
            raise

        self._connection = connection
        return connection

    def _import_legacy_json(self, connection: sqlite3.Connection) -> None:
        if self.legacy_json is None or not self.legacy_json.exists():
            return

        try:
            entries = json.loads(self.legacy_json.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read the old cache file %s, starting with an empty cache", self.legacy_json)
            return
        if not isinstance(entries, dict):
            logger.error("The old cache file %s holds no posts, starting with an empty cache", self.legacy_json)
            return

        rows = [
            (
                url,
                str(entry["version"]),
                json.dumps({key: value for key, value in entry.items() if key != "version"}, ensure_ascii=False),
            )
            for url, entry in entries.items()
            if isinstance(entry, dict) and entry.get("version")
        ]
        connection.executemany("INSERT OR IGNORE INTO posts (url, version, result) VALUES (?, ?, ?)", rows)
        logger.info(
            "Carried %d cached posts over from %s; the bot no longer uses that file and it can be deleted",
            len(rows),
            self.legacy_json,
        )


POST_CACHE = PostCache(INLINE_CACHE_DB, legacy_json=INLINE_CACHE_FILE)


def get_cached_inline_result(url: str) -> Optional[dict[str, Any]]:
    """The cached post, or None - also when the cache cannot be read, since a
    post that is not in the cache is simply downloaded again."""
    try:
        cached_result = POST_CACHE.get(normalize_post_url(url), INLINE_CACHE_VERSION)
    except (sqlite3.Error, OSError, ValueError):
        logger.exception("Failed to read the post cache")
        return None

    if cached_result and cached_result.get("items"):
        return {**cached_result, "version": INLINE_CACHE_VERSION}

    return None


def save_cached_inline_result(url: str, cached_result: dict[str, Any]) -> None:
    """Remember a prepared post. Failing to is logged and nothing more: the
    post is still delivered, it will just be downloaded again next time."""
    cached_result["version"] = INLINE_CACHE_VERSION
    result = {key: value for key, value in cached_result.items() if key != "version"}
    try:
        POST_CACHE.put(normalize_post_url(url), INLINE_CACHE_VERSION, result)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to save %s to the post cache", url)


def parse_storage_chat_id() -> int | str:
    if not STORAGE_CHAT_ID:
        raise RuntimeError("Set STORAGE_CHAT_ID to use inline mode")

    if re.fullmatch(r"-?\d+", STORAGE_CHAT_ID):
        return int(STORAGE_CHAT_ID)

    return STORAGE_CHAT_ID


def title_from_caption(caption: str) -> str:
    match = re.search(r">([^<>]+)</a>", caption)
    if match:
        return match.group(1)

    return "Instagram"


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def add_carousel_note_if_needed(caption: str, index: int, total: int) -> str:
    """An inline message carries exactly one medium, so when a single file of
    a carousel is sent that way, the caption spells out what else is in the
    post and how to get it."""
    if total <= 1:
        return caption

    note = (
        f"Файл {index + 1} из {total} в этой публикации. "
        "Повтори inline-запрос, чтобы отправить остальные."
    )
    return f"{caption}\n\n{escape(note)}"


def inline_item_result_id(url: str, index: int, total: int) -> str:
    """A single-file post keeps the bare per-URL id - the same one the
    placeholder uses - while a carousel appends the item's position, so every
    result in one answer stays distinct. The bare id is also how
    handle_chosen_inline_result tells the placeholder, which still needs its
    media swapped in, from a carousel result that is already final."""
    base_id = inline_result_id(url)
    return base_id if total == 1 else f"{base_id}-{index}"


# Telegram caps the parameter of a /start deep link at 64 characters.
START_PAYLOAD_MAX_LENGTH = 64
START_PAYLOAD_POST_TYPES = {"p", "reel", "reels", "tv"}
INSTAGRAM_SHORTCODE_RE = re.compile(r"[A-Za-z0-9_-]+")
CAROUSEL_BUTTON_TEXT = "Посмотреть карусель"


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


def carousel_keyboard(url: str, bot_username: str) -> Optional[InlineKeyboardMarkup]:
    """The button under an inline carousel message.

    An inline message carries a single medium, and Telegram never tells the
    bot which chat it was sent to - so the rest of the album cannot follow it
    there. The button opens a private chat with the bot instead, where the
    /start deep link delivers the whole post as an album."""
    payload = post_start_payload(url)
    if not payload or not bot_username:
        return None

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(CAROUSEL_BUTTON_TEXT, url=f"https://t.me/{bot_username}?start={payload}")]]
    )


def build_input_media(kind: str, media: Any, caption: Optional[str]) -> InputMediaPhoto | InputMediaDocument | InputMediaVideo:
    if kind == "photo":
        return InputMediaPhoto(media=media, caption=caption, parse_mode=ParseMode.HTML)

    if kind == "document":
        return InputMediaDocument(media=media, caption=caption, parse_mode=ParseMode.HTML)

    return InputMediaVideo(
        media=media,
        caption=caption,
        parse_mode=ParseMode.HTML,
        supports_streaming=True,
    )


# Telegram's limit on media in a single rich message. An Instagram carousel
# tops out at 20, so this only matters if that ever changes.
RICH_MESSAGE_MEDIA_LIMIT = 50
CAROUSEL_SLIDESHOW_TITLE = "Вся карусель одним сообщением"


class InputRichMessageContent(InputMessageContent):
    """Bot API 10.1 InputRichMessageContent, which python-telegram-bot does
    not know about yet: it stops at Bot API 10.0, and the pull requests adding
    rich messages were closed unmerged.

    It rides on the library's own InlineQueryResultArticle and serialises to
    exactly {"rich_message": {...}}. answer_inline_query only reaches into the
    content of a result for parse_mode, which this has none of, so it passes
    through untouched. Replace it with the library's class once it has one."""

    __slots__ = ("rich_message",)

    def __init__(self, rich_message: dict[str, Any], *, api_kwargs: Optional[dict[str, Any]] = None) -> None:
        super().__init__(api_kwargs=api_kwargs)
        with self._unfrozen():
            self.rich_message = rich_message


def carousel_slideshow_message(url: str, cached_result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The whole carousel as one rich message: a slideshow of every file in
    carousel order, with the author link as its caption.

    This is what gets a carousel into a chat through inline mode in one piece.
    An inline message cannot be an album, but it can be a rich message, and
    Telegram only allows previously uploaded files in one - which every item
    here already is, by its file_id in the storage chat.

    Only photos and videos can be slides. A file Telegram refused as media and
    took as a document cannot, so a carousel holding one is not offered as a
    slideshow at all rather than shown with a file missing."""
    items = cached_result.get("items") or []
    if len(items) < 2 or len(items) > RICH_MESSAGE_MEDIA_LIMIT:
        return None

    slides = []
    for item in items:
        kind = item.get("type")
        if kind not in ("photo", "video"):
            return None
        slides.append({"type": kind, kind: {"type": kind, "media": item["file_id"]}})

    post_url = normalize_post_url(url)
    author = cached_result.get("title")
    link_text = author if author and author != "Instagram" else post_url
    blocks: list[dict[str, Any]] = [
        {
            "type": "slideshow",
            "blocks": slides,
            # A slideshow is always a carousel, so always a post.
            "caption": {"text": ["Пост ", {"type": "url", "text": link_text, "url": post_url}]},
        }
    ]

    # Notes the caption picked up after the author link - that a video was
    # compressed, say - were escaped for Telegram's HTML parse mode. A rich
    # message block takes plain text, so they go back to it, one paragraph each.
    for note in (cached_result.get("caption") or "").split("\n\n")[1:]:
        blocks.append({"type": "paragraph", "text": unescape(note)})

    return {"blocks": blocks}


def build_inline_results(url: str, cached_result: dict[str, Any], bot_username: str = "") -> list[InlineQueryResult]:
    """One inline result per file in the post. For a carousel the list opens
    with the whole post as a single slideshow message; the per-file results
    after it stay as the fallback for a client that renders rich messages
    poorly, each carrying a button to the whole album in a private chat with
    the bot."""
    items = cached_result.get("items") or []
    base_caption = cached_result.get("caption") or escape(normalize_post_url(url))
    title = cached_result.get("title") or "Instagram"
    total = len(items)
    reply_markup = carousel_keyboard(url, bot_username) if total > 1 else None

    results: list[InlineQueryResult] = []
    slideshow = carousel_slideshow_message(url, cached_result)
    if slideshow is not None:
        # No button: this one already is the whole carousel. Without a button
        # Telegram also sends no inline_message_id when it is chosen, so it
        # never reaches handle_chosen_inline_result.
        results.append(
            InlineQueryResultArticle(
                id=f"{inline_result_id(url)}-all",
                title=CAROUSEL_SLIDESHOW_TITLE,
                description=f"{title}, слайдов: {total}",
                input_message_content=InputRichMessageContent(slideshow),
            )
        )

    for index, item in enumerate(items):
        result_id = inline_item_result_id(url, index, total)
        item_title = title if total == 1 else f"{title} - {index + 1}/{total}"
        caption = add_carousel_note_if_needed(base_caption, index, total)
        file_id = item["file_id"]
        kind = item.get("type")

        if kind == "photo":
            results.append(
                InlineQueryResultCachedPhoto(
                    id=result_id,
                    photo_file_id=file_id,
                    title=item_title,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )
        elif kind == "document":
            results.append(
                InlineQueryResultCachedDocument(
                    id=result_id,
                    document_file_id=file_id,
                    title=item_title,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )
        else:
            results.append(
                InlineQueryResultCachedVideo(
                    id=result_id,
                    video_file_id=file_id,
                    title=item_title,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )

    return results


def build_inline_article(result_id: str, title: str, description: str, message_text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text),
    )


async def get_placeholder_photo_file_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Upload the "Готовлю видео..." placeholder image to the storage chat
    once per bot run and cache its file_id, so every not-yet-ready inline
    query can reuse it as InlineQueryResultCachedPhoto instead of re-sending
    the bytes each time."""
    file_id = context.application.bot_data.get("placeholder_photo_file_id")
    if file_id:
        return file_id

    if not STORAGE_CHAT_ID:
        return None

    try:
        sent_message = await context.bot.send_photo(
            chat_id=parse_storage_chat_id(),
            photo=INLINE_PLACEHOLDER_IMAGE_BYTES,
        )
    except TelegramError:
        logger.exception("Failed to upload inline placeholder photo")
        return None

    if not sent_message.photo:
        return None

    file_id = sent_message.photo[-1].file_id
    context.application.bot_data["placeholder_photo_file_id"] = file_id
    return file_id


def build_inline_placeholder_result(url: str, photo_file_id: str) -> InlineQueryResultCachedPhoto:
    """A placeholder inline result shown while a Reel is being prepared. It
    carries a reply_markup so Telegram is guaranteed to report an
    inline_message_id in chosen_inline_result, which handle_chosen_inline_result
    then uses to swap this placeholder for the real video once it's ready -
    no need for the user to retype the query."""
    return InlineQueryResultCachedPhoto(
        id=inline_result_id(url),
        photo_file_id=photo_file_id,
        title="Готовлю видео...",
        description="Нажми, чтобы отправить — видео появится тут само через несколько секунд",
        caption="Готовлю видео, подожди немного — сообщение обновится само...",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Открыть в Instagram", url=normalize_post_url(url))]]
        ),
    )


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    cached_username = context.application.bot_data.get("bot_username")
    if cached_username:
        return str(cached_username)

    bot_user = await context.bot.get_me()
    username = bot_user.username or ""
    context.application.bot_data["bot_username"] = username
    return username


async def is_message_addressed_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if message is None:
        return False

    if message.chat.type == "private":
        return True

    username = await get_bot_username(context)
    if not username:
        return False

    return re.search(rf"@{re.escape(username)}(?![A-Za-z0-9_])", message.text or "", re.IGNORECASE) is not None


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


def post_label(items: list["MediaItem"]) -> str:
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
                username_from_instagram_profile_url(info.get("uploader_url")),
                username_from_instagram_profile_url(info.get("channel_url")),
                username_from_instagram_profile_url(info.get("creator_url")),
                username_from_instagram_profile_url(info.get("author_url")),
                username_from_instagram_profile_url(info.get("profile_url")),
                normalize_instagram_username(info.get("username")),
                normalize_instagram_username(info.get("owner_username")),
                normalize_instagram_username(info.get("channel")),
                normalize_instagram_username(info.get("author_id")),
                normalize_instagram_username(info.get("uploader_id")),
            )
            if username
        ),
        None,
    )

    if author:
        return f'{escape(label)} <a href="{escape(str(post_url), quote=True)}">{escape(author)}</a>'

    return f"{escape(label)} {escape(str(post_url))}"


def video_file_has_audio(video_path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        logger.exception("Failed to probe audio streams with ffprobe")
        # Не удалось проверить — не пугаем пользователя ложным предупреждением.
        return True

    return bool(result.stdout.strip())


def add_audio_warning_if_needed(caption: str, video_path: Path) -> str:
    if video_file_has_audio(video_path):
        return caption

    warning = "Звук недоступен: Instagram не отдал аудиодорожку для этого видео."
    return f"{caption}\n\n{escape(warning)}"


def get_video_duration_seconds(video_path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        logger.exception("Failed to read video duration with ffprobe")
        return None

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None

    return duration if duration > 0 else None


def get_video_dimensions(video_path: Path) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        logger.exception("Failed to read video dimensions with ffprobe")
        return None

    try:
        width_str, height_str = result.stdout.strip().split("x")
        width, height = int(width_str), int(height_str)
    except ValueError:
        return None

    return (width, height) if width > 0 and height > 0 else None


def video_send_hints(video_path: Path) -> dict[str, Any]:
    """Best-effort width/height/duration for Telegram's sendVideo call, so
    the client doesn't have to guess the aspect ratio itself before (or
    instead of) fully decoding the stream."""
    hints: dict[str, Any] = {}

    dimensions = get_video_dimensions(video_path)
    if dimensions:
        hints["width"], hints["height"] = dimensions

    duration = get_video_duration_seconds(video_path)
    if duration:
        hints["duration"] = round(duration)

    return hints


H264_COMPATIBLE_CODECS = {"h264", "avc1"}


def get_video_codec(video_path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        logger.exception("Failed to read video codec with ffprobe")
        return None

    codec = result.stdout.strip()
    return codec or None


def ensure_h264_video(video_path: Path, work_dir: Path) -> Path:
    """Re-encode the video to H.264 if it uses a codec (e.g. VP9, which
    Instagram sometimes serves) that Telegram on iOS can't decode. Without
    this, iPhones show a frozen frame with audio still playing instead of
    the actual video."""
    codec = get_video_codec(video_path)
    if codec is None or codec in H264_COMPATIBLE_CODECS:
        return video_path

    output_path = work_dir / f"{video_path.stem}.h264.mp4"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_COMPRESSION_PRESET,
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{VIDEO_COMPRESSION_AUDIO_KBPS}k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, timeout=900)
    except (FileNotFoundError, subprocess.SubprocessError):
        logger.exception("Failed to re-encode %s (codec=%s) to H.264", video_path, codec)
        return video_path

    return output_path if output_path.exists() else video_path


def compress_video(video_path: Path, work_dir: Path) -> Optional[Path]:
    if not ENABLE_VIDEO_COMPRESSION:
        return None

    duration = get_video_duration_seconds(video_path)
    if not duration:
        return None

    target_bits_per_second = int((VIDEO_COMPRESSION_TARGET_BYTES * 8 * 0.92) / duration)
    audio_kbps = min(VIDEO_COMPRESSION_AUDIO_KBPS, max(48, target_bits_per_second // 1000 // 5))
    video_kbps = max((target_bits_per_second // 1000) - audio_kbps, VIDEO_COMPRESSION_MIN_VIDEO_KBPS)

    for height in VIDEO_COMPRESSION_HEIGHTS:
        output_path = work_dir / f"{video_path.stem}.compressed-{height}p.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"scale=-2:{height}:force_original_aspect_ratio=decrease",
            "-c:v",
            "libx264",
            "-preset",
            VIDEO_COMPRESSION_PRESET,
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, timeout=900)
        except FileNotFoundError:
            logger.exception("ffmpeg is not installed")
            return None
        except subprocess.SubprocessError:
            logger.exception("Failed to compress video to %sp", height)
            continue

        if output_path.exists() and output_path.stat().st_size <= VIDEO_COMPRESSION_TARGET_BYTES:
            return output_path

    candidates = sorted(
        work_dir.glob(f"{video_path.stem}.compressed-*.mp4"),
        key=lambda item: item.stat().st_size,
    )
    return candidates[0] if candidates else None


def prepare_video_for_upload(video_path: Path, work_dir: Path) -> Tuple[Path, bool]:
    if video_path.stat().st_size <= MAX_FILE_SIZE_BYTES:
        return video_path, False

    compressed_path = compress_video(video_path, work_dir)
    if compressed_path and compressed_path.stat().st_size < video_path.stat().st_size:
        return compressed_path, True

    return video_path, False


def add_compression_note_if_needed(caption: str, compressed: bool) -> str:
    if not compressed:
        return caption

    note = "Видео было сжато, чтобы Telegram принял файл."
    return f"{caption}\n\n{escape(note)}"


class NoMediaInPostError(Exception):
    """Raised when the linked Instagram post yields no downloadable media at
    all - neither a video track nor a photo - distinct from a genuine
    download failure so the user gets an accurate message instead of being
    told to add cookies."""


VIDEO_FILE_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
TELEGRAM_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DOWNLOADABLE_PHOTO_EXTENSIONS = TELEGRAM_PHOTO_EXTENSIONS | {".webp", ".heic"}
# Instagram's CDN serves signed media URLs to anyone, but it still wants a
# plausible browser request - an unadorned urllib call gets a 403.
INSTAGRAM_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.instagram.com/",
}


@dataclass
class MediaItem:
    """One downloaded file from a post: a Reel or feed video, or a photo
    from a single-photo post or a carousel."""

    path: Path
    kind: str  # "photo" or "video"

    @property
    def is_video(self) -> bool:
        return self.kind == "video"


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


INSTAGRAM_SESSION = InstagramSession(COOKIES_FILE)


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

    if use_cookies and COOKIES_FILE:
        source_cookiefile = Path(COOKIES_FILE)
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
                if item.is_file() and item.suffix.lower() in VIDEO_FILE_EXTENSIONS
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
    if suffix not in DOWNLOADABLE_PHOTO_EXTENSIONS:
        suffix = ".jpg"
    photo_path = download_dir / f"photo-{index:02d}{suffix}"

    headers = dict(INSTAGRAM_IMAGE_HEADERS)
    headers.update(entry.get("http_headers") or {})

    try:
        request = urllib.request.Request(photo_url, headers=headers)
        with (
            urllib.request.urlopen(request, timeout=PHOTO_DOWNLOAD_TIMEOUT_SECONDS) as response,
            photo_path.open("wb") as photo_file,
        ):
            shutil.copyfileobj(response, photo_file)
    except (OSError, ValueError):
        logger.exception("Failed to download photo %s of the post", index)
        return None

    if not photo_path.exists() or photo_path.stat().st_size == 0:
        return None

    return photo_path


def prepare_photo_for_upload(photo_path: Path, work_dir: Path) -> Path:
    """Telegram only takes JPEG/PNG under its photo size limit, while
    Instagram sometimes serves WebP and, for newer posts, large originals -
    so re-encode anything that wouldn't be accepted as a photo."""
    too_large = photo_path.stat().st_size > PHOTO_MAX_FILE_SIZE_BYTES
    if photo_path.suffix.lower() in TELEGRAM_PHOTO_EXTENSIONS and not too_large:
        return photo_path

    output_path = work_dir / f"{photo_path.stem}.telegram.jpg"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(photo_path),
        "-vf",
        f"scale='min({PHOTO_MAX_DIMENSION},iw)':-1",
        "-q:v",
        "3",
        str(output_path),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, timeout=120)
    except (FileNotFoundError, subprocess.SubprocessError):
        logger.exception("Failed to re-encode %s for Telegram", photo_path)
        return photo_path

    return output_path if output_path.exists() else photo_path


def download_post(url: str, download_dir: Path) -> Tuple[list[MediaItem], str]:
    """Download every piece of media in an Instagram post - a Reel, a single
    video or photo, or a carousel mixing both - in the order the post shows
    them."""
    # Canonicalize to instagram.com: yt-dlp's Instagram extractor only
    # recognizes that domain, not aliases like instagr.am.
    url = normalize_post_url(url)

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

    items: list[MediaItem] = []
    for index, entry in enumerate(entries):
        if index in video_paths:
            items.append(MediaItem(ensure_h264_video(video_paths[index], download_dir), "video"))
            continue

        if index in video_indices:
            logger.warning("yt-dlp downloaded no file for video %s of %s", index + 1, url)
            continue

        photo_path = download_photo(entry, index, download_dir)
        if photo_path is None:
            continue

        items.append(MediaItem(prepare_photo_for_upload(photo_path, download_dir), "photo"))

    if not items:
        raise NoMediaInPostError("Failed to download any media from this post.")

    caption = build_post_caption(info, url, post_label(items))
    if len(items) == 1 and items[0].is_video:
        # Only meaningful for a lone video: in a carousel a silent clip next
        # to photos is normal, not a symptom of a stripped audio track.
        caption = add_audio_warning_if_needed(caption, items[0].path)

    return items, caption


def prepare_items_for_upload(items: list[MediaItem], work_dir: Path) -> Tuple[list[MediaItem], bool]:
    prepared_items: list[MediaItem] = []
    compressed_any = False

    for item in items:
        if not item.is_video:
            prepared_items.append(item)
            continue

        video_path, compressed = prepare_video_for_upload(item.path, work_dir)
        compressed_any = compressed_any or compressed
        prepared_items.append(MediaItem(video_path, item.kind))

    return prepared_items, compressed_any


class MediaTooLargeError(RuntimeError):
    """Raised when a downloaded file still exceeds Telegram's limit after
    compression, so the user is told about the size rather than getting the
    generic download-failed message."""


def ensure_items_fit_telegram(items: list[MediaItem]) -> None:
    for item in items:
        limit = MAX_FILE_SIZE_BYTES if item.is_video else PHOTO_MAX_FILE_SIZE_BYTES
        if item.path.stat().st_size > limit:
            raise MediaTooLargeError(
                f"{item.kind} file is larger than {limit // (1024 * 1024)} MB"
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    # The button under an inline carousel opens t.me/<bot>?start=<payload>,
    # which arrives here as /start <payload>.
    if context.args:
        url = post_url_from_start_payload(context.args[0])
        if url:
            await deliver_post(message, url, context)
            return

    await message.reply_text(
        "Пришли ссылку на Instagram — Reel, пост с фото или карусель, "
        "а я отправлю всё содержимое сюда."
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    await message.reply_text(f"Chat ID: <code>{message.chat_id}</code>", parse_mode=ParseMode.HTML)


def file_reference_from_message(message) -> dict[str, str]:
    if message.video is not None:
        return {"type": "video", "file_id": message.video.file_id}

    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}

    if message.document is not None:
        return {"type": "document", "file_id": message.document.file_id}

    raise RuntimeError("Telegram did not return a file_id for the uploaded media")


async def upload_item_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    storage_chat_id: int | str,
    item: MediaItem,
    caption: Optional[str],
) -> dict[str, str]:
    try:
        with item.path.open("rb") as media_file:
            if item.is_video:
                sent_message = await context.bot.send_video(
                    chat_id=storage_chat_id,
                    video=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    **video_send_hints(item.path),
                    **UPLOAD_TIMEOUTS,
                )
            else:
                sent_message = await context.bot.send_photo(
                    chat_id=storage_chat_id,
                    photo=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    **UPLOAD_TIMEOUTS,
                )

        return file_reference_from_message(sent_message)
    except BadRequest:
        logger.exception("Telegram refused the storage %s upload, sending as document", item.kind)
        with item.path.open("rb") as media_file:
            sent_message = await context.bot.send_document(
                chat_id=storage_chat_id,
                document=media_file,
                caption=caption,
                parse_mode=ParseMode.HTML,
                **UPLOAD_TIMEOUTS,
            )

        return file_reference_from_message(sent_message)


async def upload_items_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    items: list[MediaItem],
    caption: str,
) -> list[dict[str, str]]:
    """Park every file of the post in the storage chat and keep the file_ids,
    so later requests for the same post - inline or direct - can be answered
    without downloading or uploading the bytes again."""
    storage_chat_id = parse_storage_chat_id()
    if len(items) == 1:
        return [await upload_item_to_storage(context, storage_chat_id, items[0], caption)]

    uploaded: list[dict[str, str]] = []
    for chunk_index, chunk in enumerate(chunked(items, MEDIA_GROUP_LIMIT)):
        sent_messages = None
        with ExitStack() as stack:
            media_group = [
                build_input_media(
                    item.kind,
                    stack.enter_context(item.path.open("rb")),
                    caption if chunk_index == 0 and position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ]
            try:
                sent_messages = await context.bot.send_media_group(
                    chat_id=storage_chat_id,
                    media=media_group,
                    **UPLOAD_TIMEOUTS,
                )
            except BadRequest:
                logger.exception("Telegram refused the storage album, uploading its items one by one")

        if sent_messages is None:
            for item in chunk:
                uploaded.append(
                    await upload_item_to_storage(
                        context,
                        storage_chat_id,
                        item,
                        caption if not uploaded else None,
                    )
                )
        else:
            uploaded.extend(file_reference_from_message(sent_message) for sent_message in sent_messages)

    return uploaded


async def alert_if_cookies_rejected(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell the owner in the storage chat that the session cookies stopped
    working. Downloads carry on without them meanwhile; this is so it gets
    noticed before someone sends a post that needs a login, not after."""
    if not INSTAGRAM_SESSION.take_alert() or not STORAGE_CHAT_ID:
        return

    try:
        await context.bot.send_message(chat_id=parse_storage_chat_id(), text=COOKIE_ALERT_TEXT)
    except TelegramError:
        logger.exception("Failed to send the Instagram cookie alert to the storage chat")


async def download_post_in_thread(
    url: str,
    download_dir: Path,
    context: ContextTypes.DEFAULT_TYPE,
) -> Tuple[list[MediaItem], str]:
    """download_post off the event loop. The download only notes that the
    cookies were turned down - it runs in a worker thread and cannot talk to
    Telegram - so the alert goes out from here, and in `finally`: the owner
    should hear about the cookies even when the post then fails for some
    other reason."""
    try:
        return await asyncio.to_thread(download_post, url, download_dir)
    finally:
        await alert_if_cookies_rejected(context)


async def prepare_inline_post(url: str, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    cached_result = get_cached_inline_result(url)
    if cached_result:
        return cached_result

    temp_dir = Path(tempfile.mkdtemp(prefix="ig_inline_"))
    try:
        items, caption = await download_post_in_thread(url, temp_dir, context)
        items, compressed = await asyncio.to_thread(prepare_items_for_upload, items, temp_dir)
        caption = add_compression_note_if_needed(caption, compressed)
        ensure_items_fit_telegram(items)

        cached_result = {
            "caption": caption,
            "title": title_from_caption(caption),
            "items": await upload_items_to_storage(context, items, caption),
        }
        save_cached_inline_result(url, cached_result)
        return cached_result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def send_rich_message(message, rich_message: dict[str, Any]) -> None:
    """sendRichMessage, which python-telegram-bot has no method for yet, sent
    the way Message.reply_* sends everything else: quoting the message it
    answers outside a private chat, and into the same forum topic. The library
    JSON-encodes a dict parameter on its own, so rich_message goes in as is."""
    api_kwargs: dict[str, Any] = {"chat_id": message.chat_id, "rich_message": rich_message}
    if message.chat.type != "private":
        api_kwargs["reply_parameters"] = {"message_id": message.message_id}
    if message.is_topic_message and message.message_thread_id:
        api_kwargs["message_thread_id"] = message.message_thread_id

    await message.get_bot().do_api_request("sendRichMessage", api_kwargs=api_kwargs, **UPLOAD_TIMEOUTS)


async def send_prepared_result(message, cached_result: dict[str, Any], url: str) -> None:
    """Send an already-uploaded (storage-chat) post by file_id, without
    re-downloading or re-uploading the bytes.

    A carousel goes out as a single slideshow message - one message however
    long the post, against one album per ten files. If Telegram turns the
    slideshow down it falls back to albums, as does a carousel that cannot be
    a slideshow at all: one holding a file Telegram only took as a document."""
    items = cached_result.get("items") or []
    if not items:
        raise RuntimeError("Prepared result has no media")

    slideshow = carousel_slideshow_message(url, cached_result) if len(items) > 1 else None
    if slideshow is not None:
        try:
            await send_rich_message(message, slideshow)
            return
        except BadRequest:
            logger.exception("Telegram refused the carousel slideshow for %s, sending albums instead", url)

    caption = cached_result.get("caption", "")
    if len(items) == 1:
        item = items[0]
        if item.get("type") == "document":
            await message.reply_document(
                document=item["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                **UPLOAD_TIMEOUTS,
            )
        elif item.get("type") == "photo":
            await message.reply_photo(
                photo=item["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                **UPLOAD_TIMEOUTS,
            )
        else:
            await message.reply_video(
                video=item["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                **UPLOAD_TIMEOUTS,
            )
        return

    for chunk_index, chunk in enumerate(chunked(items, MEDIA_GROUP_LIMIT)):
        await message.reply_media_group(
            media=[
                build_input_media(
                    item.get("type", "video"),
                    item["file_id"],
                    caption if chunk_index == 0 and position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ],
            **UPLOAD_TIMEOUTS,
        )


async def send_local_media_items(message, items: list[MediaItem], caption: str) -> None:
    """Send freshly downloaded files straight from disk - the path taken when
    no storage chat is configured, so there are no file_ids to reuse."""
    if len(items) == 1:
        item = items[0]
        try:
            with item.path.open("rb") as media_file:
                if item.is_video:
                    await message.reply_video(
                        video=media_file,
                        filename=item.path.name,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        **video_send_hints(item.path),
                        **UPLOAD_TIMEOUTS,
                    )
                else:
                    await message.reply_photo(
                        photo=media_file,
                        filename=item.path.name,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        **UPLOAD_TIMEOUTS,
                    )
        except BadRequest:
            logger.exception("Telegram refused the %s format, sending as document", item.kind)
            with item.path.open("rb") as media_file:
                await message.reply_document(
                    document=media_file,
                    filename=item.path.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    **UPLOAD_TIMEOUTS,
                )
        return

    for chunk_index, chunk in enumerate(chunked(items, MEDIA_GROUP_LIMIT)):
        with ExitStack() as stack:
            media_group = [
                build_input_media(
                    item.kind,
                    stack.enter_context(item.path.open("rb")),
                    caption if chunk_index == 0 and position == 0 else None,
                )
                for position, item in enumerate(chunk)
            ]
            await message.reply_media_group(media=media_group, **UPLOAD_TIMEOUTS)


def get_or_create_prepare_task(url: str, context: ContextTypes.DEFAULT_TYPE) -> asyncio.Task:
    """Reuse an in-flight prepare_inline_post() task for the same post so
    concurrent requests - inline queries and direct messages alike - don't
    trigger duplicate downloads and uploads for the same URL."""
    cache_key = normalize_post_url(url)
    inline_tasks = context.application.bot_data.setdefault("inline_tasks", {})
    task = inline_tasks.get(cache_key)
    if task is None or task.done():
        task = context.application.create_task(prepare_inline_post(url, context))
        inline_tasks[cache_key] = task

        def forget_task(done_task: asyncio.Task, key: str = cache_key) -> None:
            if inline_tasks.get(key) is done_task:
                inline_tasks.pop(key, None)

        task.add_done_callback(forget_task)

    return task


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return

    url = find_instagram_url(inline_query.query)
    if not url:
        await inline_query.answer(
            [
                build_inline_article(
                    "help",
                    "Пришли ссылку на Instagram",
                    "Напиши: @bot_username https://www.instagram.com/reel/...",
                    "Пришли ссылку на Reel или пост после имени бота.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    cached_result = get_cached_inline_result(url)
    if cached_result:
        await inline_query.answer(build_inline_results(url, cached_result, await get_bot_username(context)), cache_time=0, is_personal=True)
        return

    if not STORAGE_CHAT_ID:
        await inline_query.answer(
            [
                build_inline_article(
                    "setup-required",
                    "Нужно настроить STORAGE_CHAT_ID",
                    "Inline mode требует storage-чат для кэша файлов",
                    "Inline mode еще не настроен: добавь STORAGE_CHAT_ID в .env и перезапусти бота.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    task = get_or_create_prepare_task(url, context)

    # A zero-cost check, not a wait: if this task was already started by a
    # concurrent request for the same URL and happened to finish in the
    # meantime, we can answer with the real video right away. Otherwise -
    # no artificial delay - answer immediately with a self-updating
    # placeholder; handle_chosen_inline_result() swaps it for the real
    # video via editMessageMedia once the same task completes.
    if task.done():
        try:
            cached_result = task.result()
        except NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await inline_query.answer(
                [
                    build_inline_article(
                        inline_result_id(url),
                        "В посте нет медиа",
                        "Не нашлось ни видео, ни фото",
                        "В этой публикации нет ни видео, ни фото, которые я могу скачать.",
                    )
                ],
                cache_time=0,
                is_personal=True,
            )
            return
        except Exception:
            logger.exception("Failed to prepare inline result for %s", url)
            await inline_query.answer(
                [
                    build_inline_article(
                        inline_result_id(url),
                        "Не получилось подготовить публикацию",
                        "Попробуй еще раз или отправь ссылку боту в личку",
                        "Не получилось подготовить файлы для inline-отправки.",
                    )
                ],
                cache_time=0,
                is_personal=True,
            )
            return

        await inline_query.answer(build_inline_results(url, cached_result, await get_bot_username(context)), cache_time=0, is_personal=True)
        return

    placeholder_photo_file_id = await get_placeholder_photo_file_id(context)
    if placeholder_photo_file_id:
        await inline_query.answer(
            [build_inline_placeholder_result(url, placeholder_photo_file_id)],
            cache_time=0,
            is_personal=True,
        )
    else:
        await inline_query.answer(
            [
                build_inline_article(
                    inline_result_id(url),
                    "Готовлю видео...",
                    "Через несколько секунд повтори inline-запрос",
                    "Видео готовится. Повтори inline-запрос через несколько секунд.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Once a user picks the placeholder result built by
    build_inline_placeholder_result(), swap it for the real media in place
    as soon as it's ready, using the same deduplicated prepare task the
    inline query itself started. Requires inline feedback collection to be
    enabled for the bot via @BotFather (/setinlinefeedback)."""
    chosen = update.chosen_inline_result
    if chosen is None or chosen.inline_message_id is None:
        return

    url = find_instagram_url(chosen.query)
    if not url:
        return

    # Telegram reports an inline_message_id only for a result with a button.
    # That is the placeholder, which still needs its media swapped in - and
    # also, since they carry the carousel button, the per-file results of a
    # cached carousel, which are final already. Swapping one of those would
    # re-set the same file and strip the button the moment it was sent.
    if chosen.result_id != inline_result_id(url):
        return

    cached_result = get_cached_inline_result(url)
    if cached_result is None:
        task = get_or_create_prepare_task(url, context)
        try:
            cached_result = await task
        except Exception:
            logger.exception("Failed to prepare inline result for %s after chosen_inline_result", url)
            try:
                await context.bot.edit_message_caption(
                    inline_message_id=chosen.inline_message_id,
                    caption="Не получилось подготовить публикацию. Попробуй еще раз.",
                    reply_markup=None,
                )
            except TelegramError:
                pass
            return

    items = cached_result.get("items") or []
    if not items:
        return

    # A placeholder holds one medium, so a carousel resolves to its first
    # file here, with the button to the whole album underneath.
    item = items[0]
    caption = add_carousel_note_if_needed(cached_result.get("caption", ""), 0, len(items))
    media = build_input_media(item.get("type", "video"), item["file_id"], caption)
    reply_markup = carousel_keyboard(url, await get_bot_username(context)) if len(items) > 1 else None
    try:
        await context.bot.edit_message_media(
            inline_message_id=chosen.inline_message_id,
            media=media,
            reply_markup=reply_markup,
        )
    except TelegramError:
        logger.exception("Failed to swap placeholder for the prepared media (inline_message_id=%s)", chosen.inline_message_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    if not await is_message_addressed_to_bot(update, context):
        return

    url = find_instagram_url(message.text)
    if not url:
        await message.reply_text(
            "Не вижу ссылку на Instagram. Пришли ссылку на Reel или пост в формате: "
            "instagram.com / reel / CODE или instagram.com / p / CODE",
            disable_web_page_preview=True,
        )
        return

    await deliver_post(message, url, context)


async def deliver_post(message, url: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the post behind `url` to the chat `message` came from - as an
    album when it is a carousel - from the file_id cache when it is there and
    downloading it otherwise. Shared by links sent to the bot and by the
    /start deep link behind the inline carousel button."""
    status_message = await message.reply_text("Скачиваю публикацию...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    # Fast path: this post was already downloaded and uploaded to the
    # storage chat before (via inline mode or an earlier message), so we
    # can just resend the existing file_ids instead of downloading again.
    cached_result = get_cached_inline_result(url)
    if cached_result:
        try:
            await send_prepared_result(message, cached_result, url)
        except TelegramError:
            logger.exception("Failed to resend cached media for %s, falling back to a fresh download", url)
        else:
            await status_message.delete()
            return

    if STORAGE_CHAT_ID:
        # Prepare (download + upload to storage) via the same deduplicated
        # task inline queries use, so concurrent requests for the same URL
        # - from any chat - share one download/encode instead of each
        # running their own.
        task = get_or_create_prepare_task(url, context)
        try:
            cached_result = await task
        except NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status_message.edit_text(
                "В этой публикации нет ни видео, ни фото, которые я могу скачать."
            )
            return
        except MediaTooLargeError:
            logger.exception("Media too large for %s", url)
            await status_message.edit_text(
                f"Публикация скачалась, но файл больше {MAX_FILE_SIZE_MB} МБ. Telegram может не принять такой файл."
            )
            return
        except Exception:
            logger.exception("Failed to prepare %s", url)
            await status_message.edit_text(
                "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
                "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
            )
            return

        try:
            await send_prepared_result(message, cached_result, url)
        except TelegramError:
            logger.exception("Failed to deliver prepared media for %s", url)
            await status_message.edit_text(
                "Файлы подготовлены, но Telegram не смог их отправить в этот чат. Попробуй отправить ссылку еще раз."
            )
            return

        await status_message.delete()
        return

    # Legacy path for setups without STORAGE_CHAT_ID: download and send
    # straight to this chat, without the shared cache/dedup above.
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_post_"))
    try:
        try:
            items, caption = await download_post_in_thread(url, temp_dir, context)
        except NoMediaInPostError:
            logger.info("No downloadable media in post %s", url)
            await status_message.edit_text(
                "В этой публикации нет ни видео, ни фото, которые я могу скачать."
            )
            return
        except Exception:
            logger.exception("Failed to download %s", url)
            await status_message.edit_text(
                "Не получилось скачать публикацию. Возможно, она закрытая или удалена. "
                "Если ссылка открывается в Instagram, попробуй ещё раз чуть позже."
            )
            return

        oversized_video = any(
            item.is_video and item.path.stat().st_size > MAX_FILE_SIZE_BYTES for item in items
        )
        if oversized_video and ENABLE_VIDEO_COMPRESSION:
            await status_message.edit_text("Видео большое, сжимаю перед отправкой...")

        items, compressed = await asyncio.to_thread(prepare_items_for_upload, items, temp_dir)
        caption = add_compression_note_if_needed(caption, compressed)

        try:
            ensure_items_fit_telegram(items)
        except MediaTooLargeError:
            await status_message.edit_text(
                f"Публикация скачалась, но файл больше {MAX_FILE_SIZE_MB} МБ. Telegram может не принять такой файл."
            )
            return

        try:
            await send_local_media_items(message, items, caption)
        except TimedOut:
            logger.exception("Telegram timed out while uploading the post")
            await status_message.edit_text(
                "Файлы скачаны, но Telegram слишком долго отвечал при отправке. Проверь чат: иногда они приходят позже. "
                "Если не пришли, попробуй еще раз или увеличь UPLOAD_TIMEOUT_SECONDS."
            )
            return
        except NetworkError:
            logger.exception("Network error while uploading the post")
            await status_message.edit_text(
                "Файлы скачаны, но при отправке в Telegram был сетевой сбой. Попробуй отправить ссылку еще раз."
            )
            return
        except TelegramError:
            logger.exception("Telegram failed to upload the post")
            await status_message.edit_text(
                "Файлы скачаны, но Telegram не смог их отправить. Попробуй другую публикацию или отправь ссылку еще раз."
            )
            return
        await status_message.delete()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env or environment variables")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(UPLOAD_TIMEOUT_SECONDS)
        .write_timeout(UPLOAD_TIMEOUT_SECONDS)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(handle_chosen_inline_result))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
