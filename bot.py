import asyncio
from html import escape
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_TIMEOUT_SECONDS", "180"))

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
    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
