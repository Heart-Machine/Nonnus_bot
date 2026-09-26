"""The file_id cache of posts already uploaded to the storage chat."""

import logging
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from nonnus import config, links


logger = logging.getLogger(__name__)


# Bumped to 5: captions now start with "Пост" or "Рилс", and entries cached
# before that would keep the old caption for as long as they stay cached.
# Bumped to 4: a cache entry now holds a list of media items instead of one
# file_id, so entries written by older versions can't be reused.
INLINE_CACHE_VERSION = "5"


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

    def get(self, url: str, version: str, max_age_seconds: Optional[int] = None) -> Optional[dict[str, Any]]:
        if max_age_seconds is None:
            row = self._connect().execute(
                "SELECT result FROM posts WHERE url = ? AND version = ?", (url, version)
            ).fetchone()
        else:
            row = self._connect().execute(
                "SELECT result FROM posts WHERE url = ? AND version = ? AND saved_at >= datetime('now', ?)",
                (url, version, f"-{max_age_seconds} seconds"),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, url: str, version: str, result: dict[str, Any]) -> None:
        self._connect().execute(
            "INSERT INTO posts (url, version, result) VALUES (?, ?, ?)"
            " ON CONFLICT (url) DO UPDATE SET"
            " version = excluded.version, result = excluded.result, saved_at = CURRENT_TIMESTAMP",
            (url, version, json.dumps(result, ensure_ascii=False)),
        )

    def delete(self, url: str) -> None:
        self._connect().execute("DELETE FROM posts WHERE url = ?", (url,))

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


POST_CACHE = PostCache(config.INLINE_CACHE_DB, legacy_json=config.INLINE_CACHE_FILE)


# How long a highlight or someone's current stories are served from the cache.
# A post and a single story stay what they were; these change - a story is
# added, one expires, the owner edits a highlight - so after this they are
# downloaded again.
CHANGING_STORIES_MAX_AGE_SECONDS = 60 * 60


def max_age_of(url: str) -> Optional[int]:
    if links.story_kind(url) in (links.STORIES, links.HIGHLIGHT):
        return CHANGING_STORIES_MAX_AGE_SECONDS
    return None


def get_cached_inline_result(url: str) -> Optional[dict[str, Any]]:
    """The cached post, or None - also when the cache cannot be read, since a
    post that is not in the cache is simply downloaded again."""
    try:
        cached_result = POST_CACHE.get(links.normalize_post_url(url), INLINE_CACHE_VERSION, max_age_of(url))
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
        POST_CACHE.put(links.normalize_post_url(url), INLINE_CACHE_VERSION, result)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to save %s to the post cache", url)


def forget_cached_inline_result(url: str) -> None:
    """Drop a post whose file_ids Telegram no longer accepts, so the next
    request prepares it again instead of failing on them forever."""
    try:
        POST_CACHE.delete(links.normalize_post_url(url))
    except (sqlite3.Error, OSError):
        logger.exception("Failed to drop %s from the post cache", url)
