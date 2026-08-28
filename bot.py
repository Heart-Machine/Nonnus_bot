import asyncio
import base64
import hashlib
from html import escape
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputMediaDocument,
    InputMediaVideo,
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
from yt_dlp.utils import ExtractorError


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
INLINE_CACHE_VERSION = "3"
INLINE_CACHE_FILE = Path(os.getenv("INLINE_CACHE_FILE", str(BASE_DIR / ".inline_cache.json"))).expanduser()
if not INLINE_CACHE_FILE.is_absolute():
    INLINE_CACHE_FILE = BASE_DIR / INLINE_CACHE_FILE

INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv)/[A-Za-z0-9_\-]+/?"
    r"(?:\?[^\s.,!?;:()\[\]{}<>'\"]+)?",
    re.IGNORECASE,
)

# A small placeholder photo (240x426 JPEG: a download icon - arrow into a
# tray - plus "Готовлю видео..." caption text, composed once with ffmpeg's
# drawbox/drawtext filters) shown as the inline result while a Reel is being
# downloaded, so the query doesn't have to be retyped once it's ready. Note:
# InlineQueryResultCachedPhoto's title/description fields are NOT rendered
# by any major Telegram client for photo-type inline results (confirmed via
# python-telegram-bot#2115 and telegramdesktop/tdesktop#7310) - the only
# text/graphics that actually show up are whatever is baked into the image
# itself, which is why this is a drawn icon rather than relying on API
# metadata fields. See get_placeholder_photo_file_id() and
# handle_chosen_inline_result().
INLINE_PLACEHOLDER_IMAGE_B64 = (
    "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"
    "qPVe8aUt+hPJhY11MXVaHHUXuS8MUiZuamazM6HXZ9vXij0jS7aN0adNSBMXWfqbpnhkz01iLxM2aqzwSyczl8tc86JlSsng"
    "XxMzp0ZGqeh+voPifcsy6G360ET15sGuzJFZaTieSSe21lK2jeNuDlKZm/Evi6+jqMGC4i6r3xjXvfm6duVk5jj53ivGTvUa"
    "11+Jajw6cfF7bM8zp4unTUp080lmWSaVbySSrVJItXWpa3dSlP8Atd31c8wMFsh3Rh5cNi8ZksHNdfGNaaKeLKKqsprVhUyu"
    "KNqkvVqyfxv1a+Z61t+PFuODMyY2KSCtSehDj2mUlCarVlQIjXMtEq5HZKndSlJ1X1dBTgTF1YYd22vzLL5K2jxU+UoXqa/u"
    "ctovGQ8pKkNwr9iFOjIj6PZZm4mM/kc7tmjicVhvypeQhRWr355a2YaFl37VZHOaZKKs/wByF9YmS6tUNqnRiPwMNSBmt11e"
    "Pb+48TEilkaypai6i7jW3atThhig5qWjgWlM0a5UO/C3EzPorVjCZHPYGzHZVV23DUtWEqZ5VX554YXX+Jdes6I0oV18HEta"
    "Uf8AFugrQGGrNU3TUkxtXHZnEpysVLj8HMi3JTswxyK4lQqkRHKmSLi6UpUjVPk+h53dzQZG7j3s4yL8qxyHigxMNiaJDRKd"
    "1LZVn2pnlkW/HJL1qduoroLia3l2qD0pYU0OG0q3zUW/Eyvy63Bp4XkO3Ar2va5rvx+R85OxTtWly0qX5fA6UMmt4iSxwulD"
    "MpXNkZlvxqZ1aP1a6MaYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/2Q=="
)
INLINE_PLACEHOLDER_IMAGE_BYTES = base64.b64decode(INLINE_PLACEHOLDER_IMAGE_B64)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def find_instagram_url(text: str) -> Optional[str]:
    match = INSTAGRAM_URL_RE.search(text or "")
    return match.group(0) if match else None


def normalize_reel_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = parsed_url.path.rstrip("/") + "/"
    return f"https://www.instagram.com{path}"


def inline_result_id(url: str) -> str:
    return hashlib.sha256(f"{INLINE_CACHE_VERSION}:{normalize_reel_url(url)}".encode("utf-8")).hexdigest()[:32]


def load_inline_cache() -> dict[str, dict[str, str]]:
    if not INLINE_CACHE_FILE.exists():
        return {}

    try:
        content = INLINE_CACHE_FILE.read_text(encoding="utf-8")
        if not content.strip():
            return {}
        return json.loads(content)
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read inline cache")
        return {}


def save_inline_cache(cache: dict[str, dict[str, str]]) -> None:
    INLINE_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_cached_inline_result(url: str) -> Optional[dict[str, str]]:
    cached_result = load_inline_cache().get(normalize_reel_url(url))
    if cached_result and cached_result.get("version") == INLINE_CACHE_VERSION:
        return cached_result

    return None


def save_cached_inline_result(url: str, cached_result: dict[str, str]) -> None:
    cache = load_inline_cache()
    cached_result["version"] = INLINE_CACHE_VERSION
    cache[normalize_reel_url(url)] = cached_result
    save_inline_cache(cache)


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

    return "Instagram Reel"


def build_inline_result(url: str, cached_result: dict[str, str]) -> InlineQueryResultCachedVideo | InlineQueryResultCachedDocument:
    result_id = inline_result_id(url)
    title = cached_result.get("title") or "Instagram Reel"
    caption = cached_result.get("caption") or escape(normalize_reel_url(url))

    if cached_result.get("type") == "document":
        return InlineQueryResultCachedDocument(
            id=result_id,
            title=title,
            document_file_id=cached_result["file_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    return InlineQueryResultCachedVideo(
        id=result_id,
        video_file_id=cached_result["file_id"],
        title=title,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


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
            [[InlineKeyboardButton("Открыть в Instagram", url=normalize_reel_url(url))]]
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


def build_reel_caption(info: dict[str, Any], fallback_url: str) -> str:
    reel_url = info.get("webpage_url") or fallback_url
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
                normalize_instagram_username(info.get("author_id")),
                normalize_instagram_username(info.get("uploader_id")),
            )
            if username
        ),
        None,
    )

    if author:
        return f'<a href="{escape(str(reel_url), quote=True)}">{escape(author)}</a>'

    return escape(str(reel_url))


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

    warning = "Звук недоступен: Instagram не отдал аудиодорожку для этого Reel."
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


class NoVideoInPostError(Exception):
    """Raised when the linked Instagram post has no video track at all
    (a photo post, or a carousel made only of photos) - distinct from a
    genuine download failure so the user gets an accurate message instead
    of being told to add cookies."""


def download_video(url: str, download_dir: Path) -> Tuple[Path, str]:
    # Canonicalize to instagram.com: yt-dlp's Instagram extractor only
    # recognizes that domain, not aliases like instagr.am.
    url = normalize_reel_url(url)
    output_template = str(download_dir / "%(id)s.%(ext)s")
    cookiefile = None
    if COOKIES_FILE:
        source_cookiefile = Path(COOKIES_FILE)
        cookiefile = download_dir / source_cookiefile.name
        shutil.copyfile(source_cookiefile, cookiefile)

    ydl_opts = {
        "outtmpl": output_template,
        "format": (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "best[vcodec^=avc1][ext=mp4]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    if cookiefile:
        ydl_opts["cookiefile"] = str(cookiefile)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
    except ExtractorError as error:
        if "no video formats" in str(error).lower():
            raise NoVideoInPostError(
                "This Instagram post has no video track (photo post or a photo-only carousel)."
            ) from error
        raise

    video_path = Path(filename)
    requested_downloads = info.get("requested_downloads") or []
    for requested_download in requested_downloads:
        filepath = requested_download.get("filepath") or requested_download.get("_filename")
        if filepath and Path(filepath).exists():
            video_path = Path(filepath)
            break

    if not video_path.exists():
        candidates = sorted(download_dir.glob("*"), key=lambda item: item.stat().st_size, reverse=True)
        if not candidates:
            raise FileNotFoundError("yt-dlp did not create a video file")
        video_path = candidates[0]

    video_path = ensure_h264_video(video_path, download_dir)

    caption = build_reel_caption(info, url)
    return video_path, add_audio_warning_if_needed(caption, video_path)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришли ссылку на Instagram Reel, а я отправлю видео файлом."
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    await message.reply_text(f"Chat ID: <code>{message.chat_id}</code>", parse_mode=ParseMode.HTML)


async def upload_video_to_storage(context: ContextTypes.DEFAULT_TYPE, video_path: Path, caption: str) -> dict[str, str]:
    storage_chat_id = parse_storage_chat_id()

    try:
        with video_path.open("rb") as video_file:
            sent_message = await context.bot.send_video(
                chat_id=storage_chat_id,
                video=video_file,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                **video_send_hints(video_path),
                read_timeout=UPLOAD_TIMEOUT_SECONDS,
                write_timeout=UPLOAD_TIMEOUT_SECONDS,
                connect_timeout=30,
                pool_timeout=30,
            )
        if sent_message.video is None:
            raise RuntimeError("Telegram did not return a video file_id")

        return {
            "type": "video",
            "file_id": sent_message.video.file_id,
            "caption": caption,
            "title": title_from_caption(caption),
        }
    except BadRequest:
        logger.exception("Telegram refused storage video upload, sending as document")
        with video_path.open("rb") as video_file:
            sent_message = await context.bot.send_document(
                chat_id=storage_chat_id,
                document=video_file,
                caption=caption,
                parse_mode=ParseMode.HTML,
                read_timeout=UPLOAD_TIMEOUT_SECONDS,
                write_timeout=UPLOAD_TIMEOUT_SECONDS,
                connect_timeout=30,
                pool_timeout=30,
            )
        if sent_message.document is None:
            raise RuntimeError("Telegram did not return a document file_id")

        return {
            "type": "document",
            "file_id": sent_message.document.file_id,
            "caption": caption,
            "title": title_from_caption(caption),
        }


async def prepare_inline_video(url: str, context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    cached_result = get_cached_inline_result(url)
    if cached_result:
        return cached_result

    temp_dir = Path(tempfile.mkdtemp(prefix="ig_inline_"))
    try:
        video_path, caption = await asyncio.to_thread(download_video, url, temp_dir)
        video_path, compressed = await asyncio.to_thread(prepare_video_for_upload, video_path, temp_dir)
        caption = add_compression_note_if_needed(caption, compressed)
        file_size = video_path.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            raise RuntimeError(f"Video file is larger than {MAX_FILE_SIZE_MB} MB")

        cached_result = await upload_video_to_storage(context, video_path, caption)
        save_cached_inline_result(url, cached_result)
        return cached_result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def send_prepared_result(message, cached_result: dict[str, str]) -> None:
    """Send an already-uploaded (storage-chat) video/document by file_id,
    without re-downloading or re-uploading the bytes."""
    caption = cached_result.get("caption", "")
    file_id = cached_result["file_id"]
    if cached_result.get("type") == "document":
        await message.reply_document(
            document=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            read_timeout=UPLOAD_TIMEOUT_SECONDS,
            write_timeout=UPLOAD_TIMEOUT_SECONDS,
            connect_timeout=30,
            pool_timeout=30,
        )
    else:
        await message.reply_video(
            video=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
            read_timeout=UPLOAD_TIMEOUT_SECONDS,
            write_timeout=UPLOAD_TIMEOUT_SECONDS,
            connect_timeout=30,
            pool_timeout=30,
        )


def get_or_create_prepare_task(url: str, context: ContextTypes.DEFAULT_TYPE) -> asyncio.Task:
    """Reuse an in-flight prepare_inline_video() task for the same Reel so
    concurrent requests - inline queries and direct messages alike - don't
    trigger duplicate downloads and uploads for the same URL."""
    cache_key = normalize_reel_url(url)
    inline_tasks = context.application.bot_data.setdefault("inline_tasks", {})
    task = inline_tasks.get(cache_key)
    if task is None or task.done():
        task = context.application.create_task(prepare_inline_video(url, context))
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
                    "Пришли ссылку на Instagram Reel",
                    "Напиши: @bot_username https://www.instagram.com/reel/...",
                    "Пришли ссылку на Instagram Reel после имени бота.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    cached_result = get_cached_inline_result(url)
    if cached_result:
        await inline_query.answer([build_inline_result(url, cached_result)], cache_time=0, is_personal=True)
        return

    if not STORAGE_CHAT_ID:
        await inline_query.answer(
            [
                build_inline_article(
                    "setup-required",
                    "Нужно настроить STORAGE_CHAT_ID",
                    "Inline mode требует storage-чат для кэша видео",
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
        except NoVideoInPostError:
            logger.info("No video track in post %s", url)
            await inline_query.answer(
                [
                    build_inline_article(
                        inline_result_id(url),
                        "В посте нет видео",
                        "Это фото или карусель без видео",
                        "В этой публикации нет видео — это фото или карусель без видео. Пришли ссылку на Reel или пост с видео.",
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
                        "Не получилось подготовить видео",
                        "Попробуй еще раз или отправь ссылку боту в личку",
                        "Не получилось подготовить видео для inline-отправки.",
                    )
                ],
                cache_time=0,
                is_personal=True,
            )
            return

        await inline_query.answer([build_inline_result(url, cached_result)], cache_time=0, is_personal=True)
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
    build_inline_placeholder_result(), swap it for the real video in place
    as soon as it's ready, using the same deduplicated prepare task the
    inline query itself started. Requires inline feedback collection to be
    enabled for the bot via @BotFather (/setinlinefeedback)."""
    chosen = update.chosen_inline_result
    if chosen is None or chosen.inline_message_id is None:
        return

    url = find_instagram_url(chosen.query)
    if not url:
        return

    # Already-cached results are answered as the final video directly by
    # handle_inline_query and carry no reply_markup, so they never reach
    # here with an inline_message_id - only unfinished placeholders do.
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
                    caption="Не получилось подготовить видео. Попробуй еще раз.",
                    reply_markup=None,
                )
            except TelegramError:
                pass
            return

    caption = cached_result.get("caption", "")
    media = (
        InputMediaDocument(media=cached_result["file_id"], caption=caption, parse_mode=ParseMode.HTML)
        if cached_result.get("type") == "document"
        else InputMediaVideo(media=cached_result["file_id"], caption=caption, parse_mode=ParseMode.HTML)
    )
    try:
        await context.bot.edit_message_media(
            inline_message_id=chosen.inline_message_id,
            media=media,
            reply_markup=None,
        )
    except TelegramError:
        logger.exception("Failed to swap placeholder for the prepared video (inline_message_id=%s)", chosen.inline_message_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    if not await is_message_addressed_to_bot(update, context):
        return

    url = find_instagram_url(message.text)
    if not url:
        await message.reply_text(
            "Не вижу ссылку на Instagram Reel. Пришли ссылку на ролик в формате: instagram.com / reel / CODE",
            disable_web_page_preview=True,
        )
        return

    status_message = await message.reply_text("Скачиваю видео...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    # Fast path: this Reel was already downloaded and uploaded to the
    # storage chat before (via inline mode or an earlier message), so we
    # can just resend the existing file_id instead of downloading again.
    cached_result = get_cached_inline_result(url)
    if cached_result:
        try:
            await send_prepared_result(message, cached_result)
        except TelegramError:
            logger.exception("Failed to resend cached video for %s, falling back to a fresh download", url)
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
        except RuntimeError:
            logger.exception("Video too large for %s", url)
            await status_message.edit_text(
                f"Видео скачалось, но файл больше {MAX_FILE_SIZE_MB} МБ. Telegram может не принять такой файл."
            )
            return
        except Exception:
            logger.exception("Failed to prepare %s", url)
            await status_message.edit_text(
                "Не получилось скачать видео. Если Reel приватный или Instagram просит вход, добавь cookies-файл в настройках."
            )
            return

        try:
            await send_prepared_result(message, cached_result)
        except TelegramError:
            logger.exception("Failed to deliver prepared video for %s", url)
            await status_message.edit_text(
                "Видео подготовлено, но Telegram не смог его отправить в этот чат. Попробуй отправить ссылку еще раз."
            )
            return

        await status_message.delete()
        return

    # Legacy path for setups without STORAGE_CHAT_ID: download and send
    # straight to this chat, without the shared cache/dedup above.
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_reel_"))
    try:
        try:
            video_path, caption = await asyncio.to_thread(download_video, url, temp_dir)
        except NoVideoInPostError:
            logger.info("No video track in post %s", url)
            await status_message.edit_text(
                "В этой публикации нет видео — это фото или карусель без видео. Пришли ссылку на Reel или пост с видео."
            )
            return
        except Exception:
            logger.exception("Failed to download %s", url)
            await status_message.edit_text(
                "Не получилось скачать видео. Если Reel приватный или Instagram просит вход, добавь cookies-файл в настройках."
            )
            return

        file_size = video_path.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            if ENABLE_VIDEO_COMPRESSION:
                await status_message.edit_text("Видео большое, сжимаю перед отправкой...")
                video_path, compressed = await asyncio.to_thread(prepare_video_for_upload, video_path, temp_dir)
                caption = add_compression_note_if_needed(caption, compressed)
                file_size = video_path.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            await status_message.edit_text(
                f"Видео скачалось, но файл больше {MAX_FILE_SIZE_MB} МБ. Telegram может не принять такой файл."
            )
            return

        try:
            with video_path.open("rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    filename=video_path.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    **video_send_hints(video_path),
                    read_timeout=UPLOAD_TIMEOUT_SECONDS,
                    write_timeout=UPLOAD_TIMEOUT_SECONDS,
                    connect_timeout=30,
                    pool_timeout=30,
                )
        except BadRequest:
            logger.exception("Telegram refused video format, sending as document")
            with video_path.open("rb") as video_file:
                await message.reply_document(
                    document=video_file,
                    filename=video_path.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    read_timeout=UPLOAD_TIMEOUT_SECONDS,
                    write_timeout=UPLOAD_TIMEOUT_SECONDS,
                    connect_timeout=30,
                    pool_timeout=30,
                )
        except TimedOut:
            logger.exception("Telegram timed out while uploading video")
            await status_message.edit_text(
                "Видео скачано, но Telegram слишком долго отвечал при отправке. Проверь чат: иногда файл приходит позже. "
                "Если не пришел, попробуй еще раз или увеличь UPLOAD_TIMEOUT_SECONDS."
            )
            return
        except NetworkError:
            logger.exception("Network error while uploading video")
            await status_message.edit_text(
                "Видео скачано, но при отправке в Telegram был сетевой сбой. Попробуй отправить ссылку еще раз."
            )
            return
        except TelegramError:
            logger.exception("Telegram failed to upload video")
            await status_message.edit_text(
                "Видео скачано, но Telegram не смог его отправить. Попробуй другой Reel или отправь ссылку еще раз."
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
