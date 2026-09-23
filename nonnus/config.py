"""Settings read from the environment (and .env next to bot.py)."""

import os
from pathlib import Path

from dotenv import load_dotenv


# The project root - where bot.py, .env and assets/ are - one level above
# this package.
BASE_DIR = Path(__file__).resolve().parent.parent

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


# Updates are handled side by side, so one user's post downloading no longer
# holds up everyone else. What still takes turns is the heavy part - fetching
# a post with yt-dlp and compressing its videos with ffmpeg: without a cap a
# burst of links would start that many of each at once, more than a small
# server has CPU and memory for, and a quick way to get the cookies' account
# rate-limited. A request past the cap waits for a slot; cached posts, inline
# answers and other messages go on meanwhile.
MAX_PARALLEL_DOWNLOADS = max(1, int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3")))


INLINE_CACHE_DB = Path(os.getenv("INLINE_CACHE_DB", str(BASE_DIR / ".inline_cache.sqlite3"))).expanduser()


if not INLINE_CACHE_DB.is_absolute():
    INLINE_CACHE_DB = BASE_DIR / INLINE_CACHE_DB


# The JSON file the cache was kept in before INLINE_CACHE_DB. It is read once,
# when the database is created, so the posts cached in it carry over.
INLINE_CACHE_FILE = Path(os.getenv("INLINE_CACHE_FILE", str(BASE_DIR / ".inline_cache.json"))).expanduser()


if not INLINE_CACHE_FILE.is_absolute():
    INLINE_CACHE_FILE = BASE_DIR / INLINE_CACHE_FILE
