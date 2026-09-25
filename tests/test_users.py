"""Roles and the daily limit on new downloads: what counts, what does not,
when the day turns, and what someone past the limit is told - in a private
chat, in inline mode and on a placeholder already sent.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from nonnus import cache, config, delivery, handlers, inline, preparation, users

POST_URL = "https://www.instagram.com/p/ABC123/"
OTHER_URL = "https://www.instagram.com/p/XYZ789/"
MOSCOW = ZoneInfo("Europe/Moscow")
USER = SimpleNamespace(id=42, username="Someone", first_name="Some")
CACHED = {"caption": "caption", "title": "title", "items": [{"type": "photo", "file_id": "f1"}]}


def take(times, user=USER):
    for _ in range(times):
        users.take_download(user)


# --- the limit ------------------------------------------------------------


def test_a_regular_user_gets_the_daily_limit_and_no_more():
    take(5)

    with pytest.raises(users.DailyLimitReached) as refused:
        users.take_download(USER)

    assert refused.value.limit == 5
    assert users.downloads_today(USER.id) == 5


@pytest.mark.parametrize("role", [users.PREMIUM, users.ADMIN])
def test_premium_users_and_admins_have_no_limit(role):
    users.USER_STORE.set_role(USER.id, role)

    take(12)

    assert users.downloads_today(USER.id) == 12


def test_an_admin_from_the_settings_has_no_limit_whatever_the_database_says(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({USER.id}))
    users.USER_STORE.set_role(USER.id, users.REGULAR)

    take(12)

    assert users.role_of(USER.id) == users.ADMIN


def test_a_limit_of_zero_turns_it_off(monkeypatch):
    monkeypatch.setattr(config, "DAILY_DOWNLOAD_LIMIT", 0)

    take(12)


def test_a_download_given_back_frees_its_place():
    take(4)
    download = users.take_download(USER)
    users.give_back_download(download)

    users.take_download(USER)
    with pytest.raises(users.DailyLimitReached):
        users.take_download(USER)


def test_a_download_is_given_back_to_the_day_it_was_counted_for(monkeypatch):
    monkeypatch.setattr(users, "today", lambda: "2026-09-25")
    take(4)
    download = users.take_download(USER)

    monkeypatch.setattr(users, "today", lambda: "2026-09-26")
    take(1)
    users.give_back_download(download)

    assert users.USER_STORE.downloads(USER.id, "2026-09-25") == 4
    assert users.USER_STORE.downloads(USER.id, "2026-09-26") == 1


def test_the_day_turns_at_midnight_in_the_configured_zone(monkeypatch):
    # 23:59 in Moscow is 20:59 UTC: the same UTC day as 00:01 Moscow's,
    # which is 21:01 UTC - the count must still start over.
    monkeypatch.setattr(users, "now", lambda: datetime(2026, 9, 25, 23, 59, tzinfo=MOSCOW))
    take(5)

    monkeypatch.setattr(users, "now", lambda: datetime(2026, 9, 26, 0, 1, tzinfo=MOSCOW))
    take(5)


def test_the_real_clock_counts_days_in_the_configured_zone(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT_TIMEZONE", ZoneInfo("Pacific/Kiritimati"))
    kiritimati = users.now()

    assert kiritimati.utcoffset() == timedelta(hours=14)
    assert users.today() == kiritimati.date().isoformat()


def test_the_wait_is_until_the_next_midnight(monkeypatch):
    monkeypatch.setattr(users, "now", lambda: datetime(2026, 9, 25, 21, 40, tzinfo=MOSCOW))
    take(5)

    with pytest.raises(users.DailyLimitReached) as refused:
        users.take_download(USER)

    assert refused.value.resets_in == timedelta(hours=2, minutes=20)


def test_nobody_to_count_for_is_not_counted():
    # A channel post has no sender.
    assert users.take_download(None) is None


def test_a_database_that_fails_lets_the_download_through(monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(users.USER_STORE, "take_download", broken)

    assert users.take_download(USER) is None


# --- the texts ------------------------------------------------------------


@pytest.mark.parametrize(
    "number, expected",
    [(1, "новую публикацию"), (3, "новые публикации"), (5, "новых публикаций"), (11, "новых публикаций"),
     (12, "новых публикаций"), (21, "новую публикацию"), (22, "новые публикации")],
)
def test_the_word_agrees_with_the_number(number, expected):
    assert users.plural(number, "новую публикацию", "новые публикации", "новых публикаций") == expected


@pytest.mark.parametrize(
    "wait, expected",
    [(timedelta(hours=2, minutes=20), "2 ч 20 мин"), (timedelta(hours=3), "3 ч"),
     (timedelta(minutes=45), "45 мин"), (timedelta(seconds=30), "1 мин"), (timedelta(minutes=59, seconds=1), "1 ч")],
)
def test_the_wait_is_said_in_hours_and_minutes(wait, expected):
    assert users.describe_wait(wait) == expected


def test_the_limit_text_says_what_is_free_and_when_it_resets():
    text = users.limit_reached_text(users.DailyLimitReached(5, timedelta(hours=2, minutes=20)))

    assert "5 новых публикаций" in text
    assert "уже скачивал" in text
    assert "через 2 ч 20 мин" in text


# --- who is who -------------------------------------------------------------


def test_a_username_is_found_whatever_its_case_and_by_whoever_had_it_last():
    # Within the same second, too: the database keeps time to the second.
    users.remember(SimpleNamespace(id=1, username="Taken", first_name="First"))
    users.remember(SimpleNamespace(id=2, username="taken", first_name="Second"))

    assert users.USER_STORE.find_by_username("TAKEN").user_id == 2
    assert users.USER_STORE.get(1).username is None


def test_remembering_keeps_the_role():
    users.USER_STORE.set_role(USER.id, users.PREMIUM)
    users.remember(USER)

    assert users.role_of(USER.id) == users.PREMIUM


def test_a_role_typed_in_by_hand_must_be_one_the_bot_knows():
    with pytest.raises(sqlite3.IntegrityError):
        users.USER_STORE.set_role(USER.id, "superuser")


def test_start_tells_a_regular_user_about_the_limit_and_nobody_else():
    assert "5 новых публикаций" in users.limit_note(USER)

    users.USER_STORE.set_role(USER.id, users.PREMIUM)
    assert users.limit_note(USER) == ""


def test_start_says_it_and_remembers_who_asked():
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(text)

    message = SimpleNamespace(from_user=USER, reply_text=reply_text)
    asyncio.run(handlers.start(SimpleNamespace(message=message), SimpleNamespace(args=[])))

    [reply] = replies
    assert reply.endswith(users.limit_note(USER))
    assert users.USER_STORE.find_by_username("someone").user_id == USER.id


# --- what counts ----------------------------------------------------------


def new_context(loop):
    return SimpleNamespace(application=SimpleNamespace(bot_data={}, create_task=loop.create_task))


@pytest.fixture
def preparations(monkeypatch):
    """prepare_inline_post, replaced: records the posts it is asked for and
    fails the ones listed in `failing`, after a moment."""
    started = []
    failing = set()

    async def prepare_inline_post(url, context, tracker=None):
        started.append(url)
        await asyncio.sleep(0.01)
        if url in failing:
            raise RuntimeError("private post")
        return CACHED

    monkeypatch.setattr(preparation, "prepare_inline_post", prepare_inline_post)
    return SimpleNamespace(started=started, failing=failing)


def test_joining_a_download_under_way_costs_nothing(preparations):
    other = SimpleNamespace(id=7, username=None, first_name="Other")

    async def run():
        context = new_context(asyncio.get_running_loop())
        first = preparation.get_or_create_prepare_task(POST_URL, context, user=USER)
        second = preparation.get_or_create_prepare_task(POST_URL, context, user=other)
        await asyncio.gather(first, second)

    asyncio.run(run())

    assert users.downloads_today(USER.id) == 1
    assert users.downloads_today(other.id) == 0


def test_a_preparation_that_fails_gives_the_download_back(preparations):
    preparations.failing.add(POST_URL)

    async def run():
        context = new_context(asyncio.get_running_loop())
        task = preparation.get_or_create_prepare_task(POST_URL, context, user=USER)
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

    assert users.downloads_today(USER.id) == 0


def test_past_the_limit_no_download_starts(preparations):
    take(5)

    async def run():
        context = new_context(asyncio.get_running_loop())
        with pytest.raises(users.DailyLimitReached):
            preparation.get_or_create_prepare_task(POST_URL, context, user=USER)
        return context

    context = asyncio.run(run())

    assert preparations.started == []
    assert context.application.bot_data.get("inline_tasks", {}) == {}


# --- what the user is told --------------------------------------------------


class Status:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class Message:
    chat_id = 1
    from_user = USER

    def __init__(self):
        self.status = Status()

    async def reply_text(self, text, **kwargs):
        return self.status


def deliver(url):
    message = Message()

    async def run():
        async def no_action(**kwargs):
            pass

        context = new_context(asyncio.get_running_loop())
        context.bot = SimpleNamespace(send_chat_action=no_action)
        await handlers.deliver_post(message, url, context)

    asyncio.run(run())
    return message.status


@pytest.fixture
def sent(monkeypatch):
    posts = []

    async def send_prepared_result(message, cached_result, url):
        posts.append(url)

    monkeypatch.setattr(delivery, "send_prepared_result", send_prepared_result)
    return posts


def test_past_the_limit_a_link_is_answered_with_the_limit(monkeypatch, preparations, sent):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    take(5)

    status = deliver(POST_URL)

    assert status.edits and status.edits[-1].startswith("На сегодня всё")
    assert preparations.started == [] and sent == []


def test_past_the_limit_a_cached_post_still_comes(monkeypatch, preparations, sent):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    cache.save_cached_inline_result(POST_URL, dict(CACHED))
    take(5)

    status = deliver(POST_URL)

    assert sent == [POST_URL]
    assert status.deleted


def test_without_a_storage_chat_the_limit_holds_too(monkeypatch, sent):
    downloads = []

    async def download_post_in_thread(url, temp_dir, context):
        downloads.append(url)
        raise RuntimeError("private post")

    monkeypatch.setattr(preparation, "download_post_in_thread", download_post_in_thread)
    take(4)

    # A download that fails is given back, so the fifth place is still free.
    deliver(POST_URL)
    deliver(OTHER_URL)
    take(1)
    status = deliver(POST_URL)

    assert downloads == [POST_URL, OTHER_URL]
    assert status.edits[-1].startswith("На сегодня всё")


def test_past_the_limit_an_inline_query_is_answered_with_the_limit(monkeypatch, preparations):
    monkeypatch.setattr(config, "STORAGE_CHAT_ID", "-100")
    take(5)
    answers = []

    async def answer(results, **kwargs):
        answers.append(results)

    async def run():
        query = SimpleNamespace(query=POST_URL, answer=answer, from_user=USER)
        await inline.handle_inline_query(SimpleNamespace(inline_query=query), new_context(asyncio.get_running_loop()))

    asyncio.run(run())

    [[result]] = answers
    assert result.title == "На сегодня всё"
    assert result.input_message_content.message_text.startswith("На сегодня всё")
    assert preparations.started == []


def test_a_placeholder_that_needs_a_new_download_past_the_limit_says_so(monkeypatch, preparations):
    take(5)
    captions = []

    async def edit_message_caption(**kwargs):
        captions.append(kwargs["caption"])

    async def run():
        context = new_context(asyncio.get_running_loop())
        context.bot = SimpleNamespace(edit_message_caption=edit_message_caption)
        chosen = SimpleNamespace(
            result_id=inline.inline_result_id(POST_URL), inline_message_id="inline-1", query=POST_URL, from_user=USER
        )
        await inline.handle_chosen_inline_result(SimpleNamespace(chosen_inline_result=chosen), context)

    asyncio.run(run())

    assert captions and captions[-1].startswith("На сегодня всё")
    assert preparations.started == []
