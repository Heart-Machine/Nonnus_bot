"""The downloaded files: what a post item is, and getting videos and photos
into a shape and size Telegram accepts - with ffmpeg where it takes that."""

import logging
from html import escape
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from nonnus import config


logger = logging.getLogger(__name__)


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
        config.VIDEO_COMPRESSION_PRESET,
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{config.VIDEO_COMPRESSION_AUDIO_KBPS}k",
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


def compression_scale_filter(size: int) -> str:
    """Fit the frame in a size x size box - so the number bounds the long
    side, 1280 being 720x1280 for a vertical Reel and 1280x720 for a
    landscape video alike - and never enlarge it: the box shrinks to the
    frame where the frame is smaller. Sides come out even, which libx264
    needs for yuv420p."""
    return (
        f"scale=w='min({size},iw)':h='min({size},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def compress_video(video_path: Path, work_dir: Path) -> Optional[Path]:
    """Re-encode a video to fit VIDEO_COMPRESSION_TARGET_BYTES.

    What decides the size is the bitrate times the duration; the resolution
    only decides how good that bitrate looks. So the bitrate is worked out
    from the target, kept at or above VIDEO_COMPRESSION_MIN_VIDEO_KBPS, and a
    video that would not fit Telegram's limit even at that floor is not
    encoded at all: three full encodes of a long video, fifteen minutes
    apiece at most, would only end in the same "too large". Each later step
    both lowers the resolution and cuts the bitrate by as much as the last
    try overshot, down to the floor."""
    if not config.ENABLE_VIDEO_COMPRESSION:
        return None

    duration = get_video_duration_seconds(video_path)
    if not duration:
        return None

    floor_kbps = config.VIDEO_COMPRESSION_MIN_VIDEO_KBPS
    target_kbps = int((config.VIDEO_COMPRESSION_TARGET_BYTES * 8 * 0.92) / duration) // 1000
    audio_kbps = min(config.VIDEO_COMPRESSION_AUDIO_KBPS, max(48, target_kbps // 5))
    video_kbps = max(target_kbps - audio_kbps, floor_kbps)

    size_at_floor = (floor_kbps + audio_kbps) * 1000 / 8 * duration
    if size_at_floor > config.MAX_FILE_SIZE_BYTES:
        logger.warning(
            "%s: %.0f s would be about %.0f MB even at the %d kbps floor, over the %d MB limit; not compressing",
            video_path.name,
            duration,
            size_at_floor / (1024 * 1024),
            floor_kbps,
            config.MAX_FILE_SIZE_MB,
        )
        return None

    smallest: Optional[Path] = None
    for size in config.VIDEO_COMPRESSION_HEIGHTS:
        output_path = work_dir / f"{video_path.stem}.compressed-{size}p.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            compression_scale_filter(size),
            "-c:v",
            "libx264",
            "-preset",
            config.VIDEO_COMPRESSION_PRESET,
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            # A 10-bit or 4:4:4 source would otherwise come out in a profile
            # Telegram on iOS cannot play, as in ensure_h264_video.
            "-pix_fmt",
            "yuv420p",
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
            logger.exception("Failed to compress video to %sp", size)
            continue

        if not output_path.exists():
            continue

        output_size = output_path.stat().st_size
        if output_size <= config.VIDEO_COMPRESSION_TARGET_BYTES:
            return output_path

        if smallest is None or output_size < smallest.stat().st_size:
            smallest = output_path

        # Overshot: aim the next try lower by the same proportion, with a
        # little to spare, but not below the floor - and once at the floor,
        # a smaller frame at the same bitrate would come out the same size.
        next_kbps = max(int(video_kbps * config.VIDEO_COMPRESSION_TARGET_BYTES / output_size * 0.95), floor_kbps)
        if next_kbps >= video_kbps:
            logger.warning("%s: still %d bytes at %sp and already at the bitrate floor", video_path.name, output_size, size)
            break
        video_kbps = next_kbps

    return smallest


def prepare_video_for_upload(video_path: Path, work_dir: Path) -> Tuple[Path, bool]:
    if video_path.stat().st_size <= config.MAX_FILE_SIZE_BYTES:
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


VIDEO_FILE_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


TELEGRAM_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}


DOWNLOADABLE_PHOTO_EXTENSIONS = TELEGRAM_PHOTO_EXTENSIONS | {".webp", ".heic"}


@dataclass
class MediaItem:
    """One downloaded file from a post: a Reel or feed video, or a photo
    from a single-photo post or a carousel."""

    path: Path
    kind: str  # "photo" or "video"

    @property
    def is_video(self) -> bool:
        return self.kind == "video"


def prepare_photo_for_upload(photo_path: Path, work_dir: Path) -> Path:
    """Telegram only takes JPEG/PNG under its photo size limit, while
    Instagram sometimes serves WebP and, for newer posts, large originals -
    so re-encode anything that wouldn't be accepted as a photo."""
    too_large = photo_path.stat().st_size > config.PHOTO_MAX_FILE_SIZE_BYTES
    if photo_path.suffix.lower() in TELEGRAM_PHOTO_EXTENSIONS and not too_large:
        return photo_path

    output_path = work_dir / f"{photo_path.stem}.telegram.jpg"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(photo_path),
        "-vf",
        f"scale='min({config.PHOTO_MAX_DIMENSION},iw)':-1",
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
    generic download-failed message.

    It carries which kind of file it was and that kind's limit, since the two
    differ - 50 MB for a video, 10 MB for a photo - and the message the user
    gets should name the one that was actually crossed."""

    def __init__(self, kind: str, limit_mb: int) -> None:
        super().__init__(f"{kind} file is larger than {limit_mb} MB")
        self.kind = kind
        self.limit_mb = limit_mb


def ensure_items_fit_telegram(items: list[MediaItem]) -> None:
    for item in items:
        limit_mb = config.MAX_FILE_SIZE_MB if item.is_video else config.PHOTO_MAX_FILE_SIZE_MB
        limit = config.MAX_FILE_SIZE_BYTES if item.is_video else config.PHOTO_MAX_FILE_SIZE_BYTES
        if item.path.stat().st_size > limit:
            raise MediaTooLargeError(item.kind, limit_mb)
