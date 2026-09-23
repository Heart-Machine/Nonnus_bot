"""The file_id cache in SQLite: what a lookup returns, how the posts cached in
the old JSON file carry over, and what the bot does when the database cannot
be used.

Every test gets its own database in a temporary directory, through the
autouse fixture in conftest.py.
"""
import json
import sqlite3
from contextlib import closing

import pytest

import bot

POST_URL = "https://www.instagram.com/p/ABC123/"
ITEMS = [{"type": "photo", "file_id": "f"}]


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return dict(connection.execute("SELECT url, version FROM posts").fetchall())


def by_hand(path, statement, parameters=()):
    """Change the database the way someone editing it on the server would:
    through a connection of their own."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(statement, parameters)


# --- lookups ------------------------------------------------------------


def test_round_trip():
    assert bot.get_cached_inline_result(POST_URL) is None

    bot.save_cached_inline_result(POST_URL, {"caption": "c", "title": "t", "items": ITEMS})
    cached = bot.get_cached_inline_result(POST_URL)

    assert cached == {"caption": "c", "title": "t", "items": ITEMS, "version": bot.INLINE_CACHE_VERSION}


def test_keyed_by_the_normalized_url():
    bot.save_cached_inline_result("https://instagr.am/p/ABC123", {"items": ITEMS})

    assert bot.get_cached_inline_result("https://www.instagram.com/someone/p/ABC123/?igsh=xyz") is not None


def test_saving_again_replaces_the_entry():
    bot.save_cached_inline_result(POST_URL, {"items": ITEMS})
    bot.save_cached_inline_result(POST_URL, {"items": [{"type": "video", "file_id": "v"}]})

    assert bot.get_cached_inline_result(POST_URL)["items"] == [{"type": "video", "file_id": "v"}]
    assert list(rows(bot.POST_CACHE.path)) == [POST_URL]


def test_an_entry_from_an_older_version_is_not_returned():
    bot.POST_CACHE.put(POST_URL, "3", {"items": ITEMS})

    assert bot.get_cached_inline_result(POST_URL) is None


def test_an_entry_without_items_is_not_returned():
    bot.POST_CACHE.put(POST_URL, bot.INLINE_CACHE_VERSION, {"items": []})

    assert bot.get_cached_inline_result(POST_URL) is None


def test_entries_outlive_the_connection():
    bot.save_cached_inline_result(POST_URL, {"items": ITEMS})

    bot.POST_CACHE.close()

    assert bot.get_cached_inline_result(POST_URL)["items"] == ITEMS


def test_an_entry_deleted_by_hand_is_gone_at_once():
    # The reason for autocommit: a connection that kept a read transaction
    # open would go on seeing its old snapshot of the file.
    bot.save_cached_inline_result(POST_URL, {"items": ITEMS})
    assert bot.get_cached_inline_result(POST_URL) is not None

    by_hand(bot.POST_CACHE.path, "DELETE FROM posts WHERE url = ?", (POST_URL,))

    assert bot.get_cached_inline_result(POST_URL) is None


# --- when the database cannot be used ----------------------------------


def test_an_unusable_database_reads_as_a_miss_and_saving_does_not_raise(monkeypatch, tmp_path):
    # A directory where the file should be: SQLite cannot open it.
    monkeypatch.setattr(bot, "POST_CACHE", bot.PostCache(tmp_path))

    bot.save_cached_inline_result(POST_URL, {"items": ITEMS})

    assert bot.get_cached_inline_result(POST_URL) is None


def test_a_row_mangled_by_hand_reads_as_a_miss():
    bot.save_cached_inline_result(POST_URL, {"items": ITEMS})
    by_hand(bot.POST_CACHE.path, "UPDATE posts SET result = 'not json'")

    assert bot.get_cached_inline_result(POST_URL) is None


# --- carrying the JSON cache over ---------------------------------------


@pytest.fixture
def legacy_cache(tmp_path):
    """A PostCache whose database does not exist yet, next to an old JSON
    cache file; write that file, then use the cache."""
    cache = bot.PostCache(tmp_path / "carried.sqlite3", legacy_json=tmp_path / "inline_cache.json")
    yield cache
    cache.close()


def write_legacy(cache, content):
    cache.legacy_json.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def test_the_json_cache_is_carried_over(legacy_cache):
    write_legacy(
        legacy_cache,
        {
            POST_URL: {"version": bot.INLINE_CACHE_VERSION, "caption": "c", "items": ITEMS},
            "https://www.instagram.com/p/OLD/": {"version": "3", "file_id": "old"},
        },
    )

    assert legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION) == {"caption": "c", "items": ITEMS}
    # Carried over as it was: an old entry keeps its version and so still
    # misses, just as it did in the file.
    assert rows(legacy_cache.path) == {POST_URL: bot.INLINE_CACHE_VERSION, "https://www.instagram.com/p/OLD/": "3"}


def test_the_json_cache_is_read_only_when_the_database_is_created(legacy_cache):
    write_legacy(legacy_cache, {POST_URL: {"version": bot.INLINE_CACHE_VERSION, "items": ITEMS}})
    legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION)
    by_hand(legacy_cache.path, "DELETE FROM posts")
    legacy_cache.close()

    assert legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION) is None


@pytest.mark.parametrize("content", ["not json at all", "", "[1, 2]", json.dumps({POST_URL: "not a post"})])
def test_a_broken_json_cache_leaves_an_empty_database(legacy_cache, content):
    write_legacy(legacy_cache, content)

    assert legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION) is None
    assert rows(legacy_cache.path) == {}


def test_no_json_cache_at_all(legacy_cache):
    assert legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION) is None


def test_a_start_that_fails_mid_import_leaves_nothing_behind(legacy_cache, monkeypatch):
    write_legacy(legacy_cache, {POST_URL: {"version": bot.INLINE_CACHE_VERSION, "items": ITEMS}})

    def crash(self, connection):
        connection.execute("INSERT INTO posts (url, version, result) VALUES ('half', '5', '{}')")
        raise RuntimeError("killed mid-import")

    with monkeypatch.context() as patched:
        patched.setattr(bot.PostCache, "_import_legacy_json", crash)
        with pytest.raises(RuntimeError):
            legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION)

    # No table, so the next start creates it and imports the file again.
    assert legacy_cache.get(POST_URL, bot.INLINE_CACHE_VERSION) == {"items": ITEMS}
    assert list(rows(legacy_cache.path)) == [POST_URL]
