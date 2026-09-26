"""Who uses the bot: their role, and the daily limit on new downloads.

A user is an admin, premium or regular. Admins and premium users download
without a limit, and admins also hand out the roles. A regular user may have
config.DAILY_DOWNLOAD_LIMIT new posts downloaded a day; a post already in the
cache costs nothing, since the limit is there to spare the Instagram account
that every download goes through.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from nonnus import config, links


logger = logging.getLogger(__name__)


ADMIN = "admin"
PREMIUM = "premium"
REGULAR = "regular"

ROLES = (ADMIN, PREMIUM, REGULAR)


@dataclass(frozen=True)
class UserRecord:
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    role: str


class UserStore:
    """The users database. SQLite, like the post cache and for the same
    reasons: one row by its key per lookup, a transaction per write, and WAL,
    so it can be looked at and edited by hand while the bot runs.

    Only what the bot needs is kept: the id, the username to find someone by,
    the first name to tell people without one apart, the role, and the number
    of new downloads per day."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: Optional[sqlite3.Connection] = None

    def remember(self, user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        connection = self._connect()
        if username:
            # A username belongs to one account at a time. Whoever turns up
            # with it now has it; anyone the bot saw with it before gave it up.
            connection.execute(
                "UPDATE users SET username = NULL WHERE username = ? COLLATE NOCASE AND user_id != ?",
                (username, user_id),
            )
        connection.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET"
            " username = excluded.username, first_name = excluded.first_name, last_seen = CURRENT_TIMESTAMP",
            (user_id, username, first_name),
        )

    def get(self, user_id: int) -> Optional[UserRecord]:
        row = self._connect().execute(
            "SELECT user_id, username, first_name, role FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return UserRecord(*row) if row else None

    def find_by_username(self, username: str) -> Optional[UserRecord]:
        row = self._connect().execute(
            "SELECT user_id, username, first_name, role FROM users WHERE username = ? COLLATE NOCASE LIMIT 1",
            (username,),
        ).fetchone()
        return UserRecord(*row) if row else None

    def set_role(self, user_id: int, role: str) -> None:
        # Someone who has not used the bot yet can be given a role in advance,
        # by id.
        self._connect().execute(
            "INSERT INTO users (user_id, role) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET role = excluded.role",
            (user_id, role),
        )

    def with_roles(self, roles: tuple[str, ...]) -> list[UserRecord]:
        placeholders = ", ".join("?" for _ in roles)
        rows = self._connect().execute(
            f"SELECT user_id, username, first_name, role FROM users WHERE role IN ({placeholders}) ORDER BY user_id",
            roles,
        ).fetchall()
        return [UserRecord(*row) for row in rows]

    def count(self) -> int:
        return self._connect().execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def take_download(self, user_id: int, day: str, limit: Optional[int]) -> bool:
        """Count one new download for the day, unless that would go past
        `limit` (None for no limit). The check and the count are one
        statement, so requests side by side cannot both slip under it."""
        cursor = self._connect().execute(
            "INSERT INTO downloads (user_id, day, count) VALUES (?, ?, 1)"
            " ON CONFLICT (user_id, day) DO UPDATE SET count = count + 1"
            + ("" if limit is None else " WHERE count < ?"),
            (user_id, day) if limit is None else (user_id, day, limit),
        )
        return cursor.rowcount > 0

    def give_back_download(self, user_id: int, day: str) -> None:
        self._connect().execute(
            "UPDATE downloads SET count = count - 1 WHERE user_id = ? AND day = ? AND count > 0", (user_id, day)
        )

    def downloads(self, user_id: int, day: str) -> int:
        row = self._connect().execute(
            "SELECT count FROM downloads WHERE user_id = ? AND day = ?", (user_id, day)
        ).fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # The CHECK keeps a role typed in by hand to the three the bot
                # knows: anything else would make the user a regular one without
                # a word.
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    " user_id INTEGER PRIMARY KEY,"
                    " username TEXT,"
                    " first_name TEXT,"
                    f" role TEXT NOT NULL DEFAULT '{REGULAR}' CHECK (role IN ('{ADMIN}', '{PREMIUM}', '{REGULAR}')),"
                    " first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                    " last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.execute("CREATE INDEX IF NOT EXISTS users_by_username ON users (username COLLATE NOCASE)")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS downloads ("
                    " user_id INTEGER NOT NULL,"
                    " day TEXT NOT NULL,"
                    " count INTEGER NOT NULL,"
                    " PRIMARY KEY (user_id, day))"
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except BaseException:
            connection.close()
            raise

        self._connection = connection
        return connection


USER_STORE = UserStore(config.USERS_DB)


def remember(user: Any) -> None:
    """Keep the username and name of someone using the bot up to date, so an
    admin can find them by @username. A failure is logged and nothing more:
    nobody should be kept from a post over it."""
    if user is None:
        return
    try:
        USER_STORE.remember(user.id, user.username, user.first_name)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to remember user %s", user.id)


def role_of(user_id: int) -> str:
    if user_id in config.ADMIN_USER_IDS:
        return ADMIN
    try:
        record = USER_STORE.get(user_id)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to read the role of user %s", user_id)
        return REGULAR
    return record.role if record else REGULAR


def is_admin(user: Any) -> bool:
    return user is not None and role_of(user.id) == ADMIN


STORIES_FOR_PREMIUM_TEXT = (
    "Все сторис аккаунта разом скачивают только премиум-пользователи. "
    "Пришли ссылку на конкретную сторис или на хайлайт - их может скачать любой."
)


def refusal_for(user: Any, url: str) -> Optional[str]:
    """Why `user` may not have `url`, or None when they may.

    All of someone's current stories at once - /stories/<username>/ - are
    for premium users and admins; a single story and a highlight are for
    everyone. It holds for the cache too: this is what the role gives, not
    what the download costs. Nobody to ask about - a channel post has no
    sender - is refused like a regular user."""
    if links.story_kind(url) != links.STORIES:
        return None
    if user is not None and role_of(user.id) != REGULAR:
        return None
    return STORIES_FOR_PREMIUM_TEXT


def daily_limit_of(user_id: int) -> Optional[int]:
    """The day's limit on new downloads for this user, None for none."""
    if role_of(user_id) != REGULAR or config.DAILY_DOWNLOAD_LIMIT <= 0:
        return None
    return config.DAILY_DOWNLOAD_LIMIT


def now() -> datetime:
    return datetime.now(config.DAILY_LIMIT_TIMEZONE)


def today() -> str:
    return now().date().isoformat()


def until_tomorrow() -> timedelta:
    current = now()
    midnight = datetime.combine(current.date() + timedelta(days=1), time(), tzinfo=current.tzinfo)
    return midnight - current


class DailyLimitReached(Exception):
    def __init__(self, limit: int, resets_in: timedelta) -> None:
        super().__init__(f"Daily limit of {limit} new downloads reached")
        self.limit = limit
        self.resets_in = resets_in


@dataclass(frozen=True)
class Download:
    """One new download counted for a user, to give back if it fails."""

    user_id: int
    day: str


def take_download(user: Any) -> Optional[Download]:
    """Count a new download for `user`, or raise DailyLimitReached.

    Everyone's downloads are counted, only a regular user's are limited.
    Nobody to count them for - a channel post has no sender - and a database
    that cannot be read let the download through: the limit protects the
    account, it is not worth failing a request over."""
    if user is None:
        return None

    day = today()
    limit = daily_limit_of(user.id)
    try:
        taken = USER_STORE.take_download(user.id, day, limit)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to count a download for user %s", user.id)
        return None

    if not taken:
        raise DailyLimitReached(limit, until_tomorrow())
    return Download(user.id, day)


def give_back_download(download: Optional[Download]) -> None:
    """Uncount a download that did not come through: a post that could not be
    downloaded does not use up the limit. It goes back to the day it was
    counted for, which is not today when the day turned meanwhile."""
    if download is None:
        return
    try:
        USER_STORE.give_back_download(download.user_id, download.day)
    except (sqlite3.Error, OSError):
        logger.exception("Failed to give a download back to user %s", download.user_id)


def downloads_today(user_id: int) -> int:
    try:
        return USER_STORE.downloads(user_id, today())
    except (sqlite3.Error, OSError):
        logger.exception("Failed to read the downloads of user %s", user_id)
        return 0


def plural(number: int, one: str, few: str, many: str) -> str:
    """The Russian form of a word for a number: 1 публикацию, 2 публикации,
    5 публикаций - and 11 to 14 take the last one, whatever they end in."""
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def describe_wait(wait: timedelta) -> str:
    minutes = max(1, -(-int(wait.total_seconds()) // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def limit_reached_text(error: DailyLimitReached) -> str:
    posts = plural(error.limit, "новую публикацию", "новые публикации", "новых публикаций")
    return (
        f"На сегодня всё: в день можно скачать {error.limit} {posts}. "
        "Публикации, которые бот уже скачивал, приходят без ограничений. "
        f"Новые снова можно будет скачать через {describe_wait(error.resets_in)}."
    )


def limit_note(user: Any) -> str:
    """A line on the limit for /start, empty for someone it does not apply to."""
    if user is None:
        return ""
    limit = daily_limit_of(user.id)
    if limit is None:
        return ""
    posts = plural(limit, "новую публикацию", "новые публикации", "новых публикаций")
    return f"В день можно скачать {limit} {posts}; те, что бот уже скачивал, — без ограничений."
