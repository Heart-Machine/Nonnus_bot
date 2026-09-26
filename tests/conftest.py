"""Make the nonnus package importable and pin the settings the tests rely on.

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
os.environ["ADMIN_USER_IDS"] = ""
os.environ["DAILY_DOWNLOAD_LIMIT"] = "5"
os.environ["DAILY_LIMIT_TIMEZONE"] = "Europe/Moscow"


@pytest.fixture(autouse=True)
def post_cache(monkeypatch, tmp_path):
    """A cache database of its own for every test, so that no test reads or
    writes the one in the project root, and none sees what another one
    cached."""
    from nonnus import cache

    fresh = cache.PostCache(tmp_path / "inline_cache.sqlite3")
    monkeypatch.setattr(cache, "POST_CACHE", fresh)
    yield fresh
    fresh.close()


@pytest.fixture(autouse=True)
def account_ids(monkeypatch, tmp_path):
    """The Instagram account ids kept by username, in the test's own cache
    database: an id one test learns is not known to the next."""
    from nonnus import cache

    fresh = cache.AccountIds(tmp_path / "inline_cache.sqlite3")
    monkeypatch.setattr(cache, "ACCOUNT_IDS", fresh)
    return fresh


@pytest.fixture(autouse=True)
def user_store(monkeypatch, tmp_path):
    """A users database of its own for every test, for the same reasons."""
    from nonnus import users

    fresh = users.UserStore(tmp_path / "users.sqlite3")
    monkeypatch.setattr(users, "USER_STORE", fresh)
    yield fresh
    fresh.close()
