import asyncio
import hashlib
from html import escape
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineQueryResultArticle, InlineQueryResultCachedDocument, InlineQueryResultCachedVideo, InputTextMessageContent, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler, MessageHandler, filters
from yt_dlp import YoutubeDL


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_TIMEOUT_SECONDS", "180"))
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID", "").strip()
INLINE_PREPARE_WAIT_SECONDS = int(os.getenv("INLINE_PREPARE_WAIT_SECONDS", "8"))
INLINE_CACHE_FILE = Path(os.getenv("INLINE_CACHE_FILE", str(BASE_DIR / ".inline_cache.json"))).expanduser()
if not INLINE_CACHE_FILE.is_absolute():
    INLINE_CACHE_FILE = BASE_DIR / INLINE_CACHE_FILE

INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[A-Za-z0-9_\-]+/?(?:\?[^\s]+)?",
    re.IGNORECASE,
)


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
    return hashlib.sha256(normalize_reel_url(url).encode("utf-8")).hexdigest()[:32]


def load_inline_cache() -> dict[str, dict[str, str]]:
    if not INLINE_CACHE_FILE.exists():
        return {}

    try:
        return json.loads(INLINE_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read inline cache")
        return {}


def save_inline_cache(cache: dict[str, dict[str, str]]) -> None:
    INLINE_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_cached_inline_result(url: str) -> Optional[dict[str, str]]:
    return load_inline_cache().get(normalize_reel_url(url))


def save_cached_inline_result(url: str, cached_result: dict[str, str]) -> None:
    cache = load_inline_cache()
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


def download_video(url: str, download_dir: Path) -> Tuple[Path, str]:
    output_template = str(download_dir / "%(id)s.%(ext)s")
    cookiefile = None
    if COOKIES_FILE:
        source_cookiefile = Path(COOKIES_FILE)
        cookiefile = download_dir / source_cookiefile.name
        shutil.copyfile(source_cookiefile, cookiefile)

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    if cookiefile:
        ydl_opts["cookiefile"] = str(cookiefile)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    video_path = Path(filename)
    if not video_path.exists():
        candidates = sorted(download_dir.glob("*"), key=lambda item: item.stat().st_size, reverse=True)
        if not candidates:
            raise FileNotFoundError("yt-dlp did not create a video file")
        video_path = candidates[0]

    return video_path, build_reel_caption(info, url)


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
        file_size = video_path.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            raise RuntimeError(f"Video file is larger than {MAX_FILE_SIZE_MB} MB")

        cached_result = await upload_video_to_storage(context, video_path, caption)
        save_cached_inline_result(url, cached_result)
        return cached_result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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

    try:
        cached_result = await asyncio.wait_for(asyncio.shield(task), timeout=INLINE_PREPARE_WAIT_SECONDS)
    except TimeoutError:
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    if not await is_message_addressed_to_bot(update, context):
        return

    url = find_instagram_url(message.text)
    if not url:
        await message.reply_text("Не вижу ссылку на Instagram Reel. Пришли ссылку вида https://www.instagram.com/reel/...")
        return

    status_message = await message.reply_text("Скачиваю видео...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    temp_dir = Path(tempfile.mkdtemp(prefix="ig_reel_"))
    try:
        try:
            video_path, caption = await asyncio.to_thread(download_video, url, temp_dir)
        except Exception:
            logger.exception("Failed to download %s", url)
            await status_message.edit_text(
                "Не получилось скачать видео. Если Reel приватный или Instagram просит вход, добавь cookies-файл в настройках."
            )
            return

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
