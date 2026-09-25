"""Links from the app's share sheet and from the mobile site.

A share link - /share/<id>, /share/reel/<id>, /share/p/<id> - carries no
shortcode, so it has to be resolved into the post it stands for before the
bot can do anything with it. How Instagram answers one was checked against
the live site: a desktop browser gets the web app's shell, the same for a
made-up link, while a phone gets a redirect to the post. These tests replace
the network: redirect_target runs against a local HTTP server, the chain
logic against canned redirects.
"""
import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest

from nonnus import cache, handlers, inline, instagram, links, preparation

SHARE_URL = "https://www.instagram.com/share/reel/_69O6RoGd/"
POST_URL = "https://www.instagram.com/reel/DB0YWyzPdcX/?igsh=ZGUzMzM3NWJiOQ%3D%3D"


@pytest.fixture(autouse=True)
def forget_resolved_links(monkeypatch):
    monkeypatch.setattr(preparation, "RESOLVED_SHARE_LINKS", preparation.OrderedDict())


# --- recognising the links ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://www.instagram.com/share/reel/_69O6RoGd/",
        "https://www.instagram.com/share/p/BAxYz09/",
        "https://www.instagram.com/share/_69O6RoGd",
        "https://instagram.com/share/reel/_69O6RoGd?igsh=abc",
        "https://m.instagram.com/share/reel/_69O6RoGd/",
    ],
)
def test_share_links_are_found_and_known_for_what_they_are(text):
    url = links.find_instagram_url(f"смотри {text} !")

    assert url is not None
    assert links.is_share_link(url)


def test_a_share_link_is_not_taken_for_a_post_by_someone_called_share():
    # It used to be: the id went to yt-dlp as a shortcode and the post came
    # back "private or deleted".
    url = links.find_instagram_url(SHARE_URL)

    assert links.is_share_link(url)
    assert not links.is_share_link(links.find_instagram_url("https://www.instagram.com/someone/p/ABC123/"))


@pytest.mark.parametrize(
    "text, normalized",
    [
        ("https://m.instagram.com/p/Ddo52uECrve/", "https://www.instagram.com/p/Ddo52uECrve/"),
        ("https://m.instagram.com/reel/ABC123/?igsh=x", "https://www.instagram.com/reel/ABC123/"),
    ],
)
def test_mobile_site_links_are_posts_as_they_are(text, normalized):
    # m.instagram.com redirects to the same path on www, so the shortcode in
    # it is real and nothing needs resolving.
    url = links.find_instagram_url(text)

    assert not links.is_share_link(url)
    assert links.normalize_post_url(url) == normalized


# --- following the redirects ----------------------------------------------


def follow(monkeypatch, redirects):
    """resolve_share_link over canned redirects: url -> target, None for a
    page, or an exception to raise. Returns the result and the urls asked."""
    asked = []

    def redirect_target(url):
        asked.append(url)
        outcome = redirects.get(url)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(instagram, "redirect_target", redirect_target)
    return instagram.resolve_share_link("https://instagram.com/share/reel/_69O6RoGd"), asked


def test_the_redirects_are_followed_to_the_post(monkeypatch):
    # The shape the live site has: bare domain to www, www to the post.
    result, asked = follow(
        monkeypatch,
        {
            "https://instagram.com/share/reel/_69O6RoGd": "https://www.instagram.com/share/reel/_69O6RoGd",
            "https://www.instagram.com/share/reel/_69O6RoGd": POST_URL,
        },
    )

    assert result == POST_URL
    assert len(asked) == 2


def test_a_page_instead_of_a_redirect_resolves_to_nothing(monkeypatch):
    result, _ = follow(monkeypatch, {})

    assert result is None


def test_a_redirect_away_from_instagram_is_not_followed(monkeypatch):
    result, asked = follow(
        monkeypatch,
        {"https://instagram.com/share/reel/_69O6RoGd": "https://evil.example/reel/ABC123/"},
    )

    assert result is None
    assert asked == ["https://instagram.com/share/reel/_69O6RoGd"]


def test_redirects_going_round_in_circles_are_given_up_on(monkeypatch):
    loop = {
        "https://instagram.com/share/reel/_69O6RoGd": "https://www.instagram.com/share/reel/_69O6RoGd",
        "https://www.instagram.com/share/reel/_69O6RoGd": "https://instagram.com/share/reel/_69O6RoGd",
    }
    result, asked = follow(monkeypatch, loop)

    assert result is None
    assert len(asked) == instagram.SHARE_LINK_MAX_REDIRECTS


def test_a_network_error_resolves_to_nothing(monkeypatch):
    result, _ = follow(monkeypatch, {"https://instagram.com/share/reel/_69O6RoGd": TimeoutError("timed out")})

    assert result is None


class RedirectServer:
    """Answers each request with a canned response and records the request."""

    def __init__(self, response):
        self.response = response
        self.request = b""
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}/share/reel/_69O6RoGd/"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        connection, _ = self._socket.accept()
        with connection:
            self.request = connection.recv(65536)
            connection.sendall(self.response)
        self._socket.close()


def test_redirect_target_asks_as_a_phone_with_head_and_does_not_follow():
    server = RedirectServer(b"HTTP/1.1 302 Found\r\nLocation: /reel/DB0YWyzPdcX/\r\nContent-Length: 0\r\n\r\n")

    target = instagram.redirect_target(server.url)

    # A relative Location comes back absolute; nothing was fetched from it.
    assert target == server.url.split("/share/")[0] + "/reel/DB0YWyzPdcX/"
    request = server.request.decode("latin-1")
    assert request.startswith("HEAD /share/reel/_69O6RoGd/ ")
    assert f"User-Agent: {instagram.SHARE_LINK_USER_AGENT}\r\n" in request


def test_the_agent_is_a_phone():
    # A desktop browser gets the web app's shell, with no redirect to follow.
    assert instagram.SHARE_LINK_USER_AGENT.startswith("Mozilla/5.0 (iPhone;")
    assert "Mobile/" in instagram.SHARE_LINK_USER_AGENT


def test_redirect_target_of_a_page_is_nothing():
    server = RedirectServer(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    assert instagram.redirect_target(server.url) is None


# --- resolving once -------------------------------------------------------


def count_resolutions(monkeypatch, result=POST_URL):
    calls = []

    def resolve_share_link(url):
        calls.append(url)
        return result

    monkeypatch.setattr(instagram, "resolve_share_link", resolve_share_link)
    return calls


def test_an_ordinary_link_is_used_as_it_is(monkeypatch):
    calls = count_resolutions(monkeypatch)

    assert asyncio.run(preparation.resolve_link("https://www.instagram.com/p/ABC123/")) == (
        "https://www.instagram.com/p/ABC123/"
    )
    assert calls == []


def test_a_share_link_is_resolved_once_for_every_keystroke(monkeypatch):
    calls = count_resolutions(monkeypatch)

    async def run():
        return [await preparation.resolve_link(SHARE_URL) for _ in range(3)]

    assert asyncio.run(run()) == [POST_URL] * 3
    assert len(calls) == 1


def test_a_failed_resolution_is_tried_again_next_time(monkeypatch):
    calls = count_resolutions(monkeypatch, result=None)

    async def run():
        return [await preparation.resolve_link(SHARE_URL) for _ in range(2)]

    assert asyncio.run(run()) == [None, None]
    assert len(calls) == 2


def test_the_memory_of_resolved_links_is_bounded(monkeypatch):
    count_resolutions(monkeypatch)
    monkeypatch.setattr(preparation, "RESOLVED_SHARE_LINKS_LIMIT", 2)

    async def run():
        for share_id in ["a1", "b2", "c3"]:
            await preparation.resolve_link(f"https://www.instagram.com/share/{share_id}/")

    asyncio.run(run())

    assert list(preparation.RESOLVED_SHARE_LINKS) == ["/share/b2", "/share/c3"]


# --- the handlers -----------------------------------------------------------


class Message:
    chat_id = 1
    from_user = SimpleNamespace(id=1, username="someone", first_name="Someone")

    def __init__(self, text):
        self.text = text
        self.chat = SimpleNamespace(type="private")
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def send_to_bot(monkeypatch, text):
    delivered = []

    async def deliver_post(message, url, context):
        delivered.append(url)

    monkeypatch.setattr(handlers, "deliver_post", deliver_post)
    message = Message(text)
    asyncio.run(handlers.handle_message(SimpleNamespace(message=message), SimpleNamespace()))
    return delivered, message.replies


def test_a_share_link_sent_to_the_bot_delivers_the_post_it_stands_for(monkeypatch):
    count_resolutions(monkeypatch)

    delivered, replies = send_to_bot(monkeypatch, SHARE_URL)

    assert delivered == [POST_URL]
    assert replies == []


def test_a_share_link_that_would_not_resolve_is_said_so(monkeypatch):
    count_resolutions(monkeypatch, result=None)

    delivered, replies = send_to_bot(monkeypatch, SHARE_URL)

    assert delivered == []
    assert replies == [handlers.SHARE_LINK_UNRESOLVED_TEXT]


class InlineQuery:
    def __init__(self, query):
        self.query = query
        self.from_user = SimpleNamespace(id=1, username="someone", first_name="Someone")
        self.answers = []

    async def answer(self, results, **kwargs):
        self.answers.append(results)


def ask_inline(query):
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"bot_username": "nonnus_bot"}))
    asyncio.run(inline.handle_inline_query(SimpleNamespace(inline_query=query), context))
    return query.answers[-1]


def test_an_inline_share_link_is_answered_from_the_cache_of_the_post(monkeypatch):
    count_resolutions(monkeypatch)
    cache.save_cached_inline_result(POST_URL, {"caption": "c", "title": "t", "items": [{"type": "photo", "file_id": "f"}]})

    results = ask_inline(InlineQuery(SHARE_URL))

    assert [result.id for result in results] == [inline.inline_result_id(POST_URL)]


def test_an_inline_share_link_that_would_not_resolve_is_said_so(monkeypatch):
    count_resolutions(monkeypatch, result=None)

    results = ask_inline(InlineQuery(SHARE_URL))

    assert [result.id for result in results] == ["share-unresolved"]
