"""The bot token stays out of the log.

It travels in the URL of every Bot API request, and httpx logged every
request at INFO - a line with the token for every poll of getUpdates. These
tests run the logging exactly as main() sets it up, into a buffer, and one of
them makes a real httpx request to see that the library's own line is gone.
"""
import io
import logging

import httpx
import pytest

from nonnus import app, config

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
OTHER_TOKEN = "987654321:BBHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"


@pytest.fixture
def log(monkeypatch):
    """Logging as main() sets it up, writing into a buffer; the root logger
    and the chatty loggers are put back afterwards."""
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, {name: logging.getLogger(name).level for name in ("httpx", "httpcore")})

    app.configure_logging()
    buffer = io.StringIO()
    root.handlers[0].setStream(buffer)
    yield buffer

    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)


def test_httpx_request_lines_are_gone(log):
    # A real request through httpx, answered locally: httpx logs it the way it
    # logged every getUpdates on the server.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        client.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates")

    assert log.getvalue() == ""


def test_a_token_in_a_message_is_redacted(log):
    logging.getLogger("nonnus.test").error("Request to https://api.telegram.org/bot%s/getMe failed", TOKEN)

    line = log.getvalue()
    assert TOKEN not in line
    assert "https://api.telegram.org/bot<BOT_TOKEN>/getMe" in line


def test_a_token_in_a_traceback_is_redacted(log):
    try:
        raise ConnectionError(f"could not reach https://api.telegram.org/bot{TOKEN}/getUpdates")
    except ConnectionError:
        logging.getLogger("nonnus.test").exception("Polling failed")

    assert TOKEN not in log.getvalue()
    assert "ConnectionError" in log.getvalue()


def test_any_bot_token_is_redacted_not_only_the_configured_one(log):
    logging.getLogger("nonnus.test").warning("old token %s", OTHER_TOKEN)

    assert OTHER_TOKEN not in log.getvalue()


def test_the_configured_token_is_redacted_whatever_its_shape(log, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "odd-shaped-secret")
    logging.getLogger("nonnus.test").warning("token odd-shaped-secret in a message")

    assert "odd-shaped-secret" not in log.getvalue()


def test_ordinary_lines_are_left_alone(log):
    logging.getLogger("nonnus.test").info("Canary: https://www.instagram.com/p/Ddo52uECrve/ downloaded fine")

    assert "Canary: https://www.instagram.com/p/Ddo52uECrve/ downloaded fine" in log.getvalue()


def test_httpx_warnings_still_get_through(log):
    logging.getLogger("httpx").warning("something httpx wants said")

    assert "something httpx wants said" in log.getvalue()


def test_main_sets_the_logging_up_first(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)

    class Application:
        def run_polling(self, **kwargs):
            calls.append("polling")

    monkeypatch.setattr(app, "build_application", lambda token: Application())

    app.main()

    assert calls == ["logging", "polling"]


def test_importing_the_package_leaves_logging_alone():
    # Nothing is set up on import any more - the tests, the image build check
    # and anyone else importing it keep their own logging.
    assert not any(isinstance(handler.formatter, app.RedactingFormatter) for handler in logging.getLogger().handlers)
