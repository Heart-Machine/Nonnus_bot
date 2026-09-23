"""Make bot.py importable and pin the settings the tests rely on.

python-dotenv leaves variables that are already in the environment alone, so
whatever is set here wins over a developer's own .env file and the suite
behaves the same locally and in CI.
"""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ["COOKIES_FILE"] = ""
os.environ["STORAGE_CHAT_ID"] = ""
os.environ["MAX_FILE_SIZE_MB"] = "50"
os.environ["PHOTO_MAX_FILE_SIZE_MB"] = "10"
os.environ["ENABLE_VIDEO_COMPRESSION"] = "true"


@pytest.fixture(autouse=True)
def post_cache(monkeypatch, tmp_path):
    """A cache database of its own for every test, so that no test reads or
    writes the one next to bot.py, and none sees what another one cached."""
    import bot

    cache = bot.PostCache(tmp_path / "inline_cache.sqlite3")
    monkeypatch.setattr(bot, "POST_CACHE", cache)
    yield cache
    cache.close()
