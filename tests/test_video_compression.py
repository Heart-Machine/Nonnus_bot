"""Compressing a video to fit Telegram, and probing it without stopping the bot.

In the arithmetic tests ffmpeg is a model: it "encodes" by writing a file
whose size is the bitrate it was given times the duration, scaled by
`overshoot`. That is enough to check the steps - what bitrate each try gets
and when trying stops. The scale filter is checked against the real ffmpeg,
where one is installed.
"""
import asyncio
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from nonnus import config, delivery, media

MB = 1024 * 1024


@pytest.fixture
def encoder(monkeypatch, tmp_path):
    """ffmpeg as a model. Set `duration` and `overshoot`; read `runs` for
    the (video kbps, scale filter) of every encode."""
    state = SimpleNamespace(duration=600.0, overshoot=1.0, runs=[], commands=[])
    monkeypatch.setattr(config, "ENABLE_VIDEO_COMPRESSION", True)
    monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 50)
    monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 50 * MB)
    monkeypatch.setattr(config, "VIDEO_COMPRESSION_TARGET_MB", 49)
    monkeypatch.setattr(config, "VIDEO_COMPRESSION_TARGET_BYTES", 49 * MB)
    monkeypatch.setattr(config, "VIDEO_COMPRESSION_HEIGHTS", [1280, 854, 640])
    monkeypatch.setattr(config, "VIDEO_COMPRESSION_AUDIO_KBPS", 96)
    monkeypatch.setattr(config, "VIDEO_COMPRESSION_MIN_VIDEO_KBPS", 250)
    monkeypatch.setattr(media, "get_video_duration_seconds", lambda path: state.duration)

    def run(command, **kwargs):
        video_kbps = int(command[command.index("-b:v") + 1].rstrip("k"))
        audio_kbps = int(command[command.index("-b:a") + 1].rstrip("k"))
        state.runs.append((video_kbps, command[command.index("-vf") + 1]))
        state.commands.append(command)
        overshoot = state.overshoot[len(state.runs) - 1] if isinstance(state.overshoot, list) else state.overshoot
        size = int((video_kbps + audio_kbps) * 1000 / 8 * state.duration * overshoot)
        with open(command[-1], "wb") as output:
            output.truncate(size)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(media.subprocess, "run", run)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    state.source = source
    state.work_dir = tmp_path
    return state


def compress(encoder):
    return media.compress_video(encoder.source, encoder.work_dir)


def test_a_video_that_fits_on_the_first_try_is_encoded_once(encoder):
    result = compress(encoder)

    assert len(encoder.runs) == 1
    assert result.stat().st_size <= config.VIDEO_COMPRESSION_TARGET_BYTES
    # 49 MB over 600 s with 8% to spare is 630 kbps, less 96 kbps of audio.
    assert encoder.runs[0][0] == 534


def test_an_overshoot_lowers_the_bitrate_for_the_next_try_not_just_the_frame(encoder):
    # It used to encode every step at the same bitrate, so a smaller frame
    # came out no smaller - measured on the real ffmpeg: 574, 636, 692 KB.
    encoder.overshoot = 1.15

    result = compress(encoder)

    first, second = encoder.runs[0][0], encoder.runs[1][0]
    assert second < first * 0.9
    assert encoder.runs[1][1] == media.compression_scale_filter(854)
    assert result.stat().st_size <= config.VIDEO_COMPRESSION_TARGET_BYTES


def test_a_video_too_long_to_fit_even_at_the_floor_is_not_encoded_at_all(encoder):
    # 30 minutes: 298 kbps at the floor is about 64 MB. It used to take three
    # full encodes to find that out.
    encoder.duration = 30 * 60

    assert compress(encoder) is None
    assert encoder.runs == []


def test_a_video_just_short_of_the_target_bitrate_is_still_tried_at_the_floor(encoder):
    # 22 minutes: the target works out below the floor, but at the floor it
    # comes to about 47 MB - under Telegram's 50 MB, which is what counts.
    encoder.duration = 22 * 60

    result = compress(encoder)

    assert [kbps for kbps, _ in encoder.runs] == [config.VIDEO_COMPRESSION_MIN_VIDEO_KBPS]
    assert result is not None


def test_trying_stops_once_the_bitrate_is_at_the_floor(encoder):
    # A smaller frame at the same bitrate would come out the same size.
    encoder.duration = 22 * 60
    encoder.overshoot = 1.2

    compress(encoder)

    assert len(encoder.runs) == 1


def test_the_smallest_try_comes_back_when_none_fits(encoder):
    # The later tries overshoot by more than the bitrate came down - x264
    # can miss by more on a smaller frame - so the first file, not the last,
    # is the smallest one.
    encoder.overshoot = [1.2, 1.6, 2.2]

    result = compress(encoder)

    sizes = {path.name: path.stat().st_size for path in encoder.work_dir.glob("clip.compressed-*.mp4")}
    assert len(sizes) == 3
    assert result.name == min(sizes, key=sizes.get) == "clip.compressed-1280p.mp4"


def test_every_try_is_encoded_as_yuv420p(encoder):
    encoder.overshoot = 1.15

    compress(encoder)

    assert all(command[command.index("-pix_fmt") + 1] == "yuv420p" for command in encoder.commands)


# --- the scale filter, on the real ffmpeg --------------------------------


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg is not installed"
)


@needs_ffmpeg
@pytest.mark.parametrize(
    "source, bound, expected",
    [
        ("1080x1920", 1280, "720x1280"),  # a vertical Reel, as before
        ("1920x1080", 1280, "1280x720"),  # was scaled up to 2276x1280
        ("640x360", 1280, "640x360"),  # was scaled up to 2276x1280 too
        ("1080x1350", 854, "684x854"),
        ("721x1281", 1280, "720x1280"),  # odd sides come out even
    ],
)
def test_the_frame_fits_the_bound_on_its_long_side_and_is_never_enlarged(tmp_path, source, bound, expected):
    source_path = tmp_path / "source.mkv"
    output_path = tmp_path / "scaled.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={source}:rate=5", "-t", "0.4",
         "-c:v", "ffv1", str(source_path)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source_path), "-vf", media.compression_scale_filter(bound),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(output_path)],
        check=True,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=s=x:p=0", str(output_path)],
        check=True, capture_output=True, text=True,
    )

    assert probe.stdout.strip() == expected


# --- probing a video without stopping the bot -------------------------------


def test_video_hints_are_read_off_the_event_loop(monkeypatch, tmp_path):
    threads = []

    def video_send_hints(path):
        threads.append(threading.get_ident())
        return {"width": 720, "height": 1280, "duration": 12}

    sent = {}

    async def send_video(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(video=SimpleNamespace(file_id="v1"))

    monkeypatch.setattr(media, "video_send_hints", video_send_hints)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    context = SimpleNamespace(bot=SimpleNamespace(send_video=send_video))

    async def upload():
        loop_thread = threading.get_ident()
        await delivery.upload_item_to_storage(context, -100, media.MediaItem(path, "video"), "caption")
        return loop_thread

    loop_thread = asyncio.run(upload())

    assert threads and threads[0] != loop_thread
    assert (sent["width"], sent["height"], sent["duration"]) == (720, 1280, 12)


def test_a_photo_is_not_probed(monkeypatch):
    monkeypatch.setattr(media, "video_send_hints", lambda path: pytest.fail("a photo has nothing to probe"))

    assert asyncio.run(delivery.video_hints(media.MediaItem(None, "photo"))) == {}
